"""Ingesta de datos. Todas las fuentes son gratuitas y sin llave de API.

Diseño clave: cada fuente externa degrada con elegancia. Si Sleeper o ESPN no
responden (red restringida, caída temporal), el pipeline sigue corriendo y
marca esa señal como no disponible en vez de romperse.
"""
from __future__ import annotations

import json
import time
from typing import Any

import polars as pl

from . import config as C

try:
    import nflreadpy as nfl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Falta nflreadpy. Corre: pip install -r requirements.txt") from exc

import urllib.error
import urllib.request

UA = {"User-Agent": "nfl-edge-2026/1.0 (fantasy decision tool)"}
EMPTY = pl.DataFrame()


# --------------------------------------------------------------------- util
def _http_json(url: str, timeout: int = 20, retries: int = 2) -> Any | None:
    """GET JSON con reintentos. Devuelve None si la fuente no está disponible."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return None


def _safe(fn, *args, label: str = "") -> pl.DataFrame:
    """Llama a un loader de nflverse; devuelve DataFrame vacío si falla."""
    try:
        df = fn(*args)
        if df is None:
            return EMPTY
        if not isinstance(df, pl.DataFrame):
            df = pl.from_pandas(df)
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"  [aviso] {label or fn.__name__} no disponible: {str(exc)[:110]}")
        return EMPTY


def _safe_by_season(fn, seasons: list[int], label: str = "") -> pl.DataFrame:
    """Carga temporada por temporada y concatena lo que sí exista.

    Indispensable en pretemporada: pedir 2026 antes del kickoff hace fallar la
    llamada completa y nos dejaría sin los datos de 2025 y 2024 que sí están.
    """
    frames: list[pl.DataFrame] = []
    missing: list[int] = []
    for s in seasons:
        df = _safe(fn, [s], label=f"{label} {s}")
        if df.height:
            frames.append(df)
        else:
            missing.append(s)
    if missing:
        print(f"  [info] {label}: sin datos aún para {missing} (normal antes del kickoff)")
    if not frames:
        return EMPTY
    if len(frames) == 1:
        return frames[0]
    return pl.concat(frames, how="diagonal_relaxed")


# ------------------------------------------------------------------ nflverse
def load_nflverse(seasons: list[int], season: int | None = None) -> dict[str, pl.DataFrame]:
    """Carga todos los datasets de nflverse que necesita el modelo."""
    seasons = sorted({s for s in seasons if s >= 1999})
    season = season or C.SEASON
    print(f"[nflverse] temporadas {seasons}")

    out: dict[str, pl.DataFrame] = {}
    out["pbp"] = _safe_by_season(nfl.load_pbp, seasons, label="pbp")
    out["weekly"] = _safe_by_season(nfl.load_player_stats, seasons, label="player_stats")
    out["snaps"] = _safe_by_season(nfl.load_snap_counts, seasons, label="snap_counts")
    out["rosters"] = _safe_by_season(nfl.load_rosters_weekly, seasons, label="rosters_weekly")
    out["injuries"] = _safe_by_season(nfl.load_injuries, seasons, label="injuries")
    out["ffopp"] = _safe_by_season(nfl.load_ff_opportunity, seasons, label="ff_opportunity")
    out["players"] = _safe(nfl.load_players, label="players")
    # Roster de temporada del año objetivo: en pretemporada `rosters_weekly`
    # todavía no existe, pero `rosters` ya trae las altas de agencia libre y el
    # draft. Sin esto asignaríamos a cada jugador su equipo del año pasado.
    out["rosters_current"] = _safe(nfl.load_rosters, [season], label="rosters_current")
    # Separación promedio del receptor (Next Gen Stats). El backtest mostró que
    # el semáforo funciona en receptores que separan y se invierte en los que no.
    out["ngs_rec"] = _safe_by_season(
        lambda ss: nfl.load_nextgen_stats(ss, "receiving"), seasons, label="ngs_receiving")
    out["teams"] = _safe(nfl.load_teams, label="teams")

    # El calendario incluye spread_line / total_line / moneyline: nuestra fuente
    # de mercado, gratis y ya dentro de nflverse. Cargamos historia larga porque
    # el Elo y el backtest del modelo de partidos la necesitan (pesa muy poco).
    sched_seasons = sorted(set(seasons) | set(C.CALIBRATION_SEASONS) | {season})
    out["schedules"] = _safe_by_season(nfl.load_schedules, sched_seasons, label="schedules")

    for k, v in out.items():
        print(f"  {k:<11} {v.shape if v.height else 'VACÍO'}")
    return out


# -------------------------------------------------------------------- Sleeper
def load_sleeper_trending(hours: int = 24, limit: int = 200) -> dict[str, dict[str, int]]:
    """Altas y bajas en tendencia (24h) de todas las ligas de Sleeper.

    Señal de MERCADO, no de rendimiento. La usamos sobre todo al revés: el valor
    está en los jugadores cuyo uso sube y que TODAVÍA no están en tendencia.
    """
    base = "https://api.sleeper.app/v1/players/nfl/trending"
    res: dict[str, dict[str, int]] = {"add": {}, "drop": {}}
    ok = False
    for kind in ("add", "drop"):
        data = _http_json(f"{base}/{kind}?lookback_hours={hours}&limit={limit}")
        if isinstance(data, list):
            ok = True
            for row in data:
                pid = str(row.get("player_id", ""))
                if pid:
                    res[kind][pid] = int(row.get("count", 0))
    print(f"[sleeper] trending: {'OK' if ok else 'NO DISPONIBLE'} "
          f"(adds={len(res['add'])}, drops={len(res['drop'])})")
    res["_available"] = ok  # type: ignore[assignment]
    return res


def load_sleeper_players() -> dict[str, dict]:
    """Catálogo de jugadores de Sleeper: mapea sleeper_id <-> gsis_id."""
    data = _http_json("https://api.sleeper.app/v1/players/nfl", timeout=60)
    if not isinstance(data, dict):
        print("[sleeper] catálogo NO DISPONIBLE")
        return {}
    print(f"[sleeper] catálogo OK ({len(data)} jugadores)")
    return data


# ----------------------------------------------------------------------- ESPN
def load_espn_standings(season: int) -> dict[str, dict]:
    """Récords por equipo desde la API pública de ESPN (sin llave)."""
    url = ("https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
           f"?season={season}")
    data = _http_json(url)
    out: dict[str, dict] = {}
    if not isinstance(data, dict):
        print("[espn] standings NO DISPONIBLE (se usará récord derivado del calendario)")
        return out
    for child in data.get("children", []):
        for entry in child.get("standings", {}).get("entries", []):
            abbr = entry.get("team", {}).get("abbreviation")
            if not abbr:
                continue
            stats = {s.get("name"): s.get("value") for s in entry.get("stats", [])}
            out[abbr] = {
                "wins": int(stats.get("wins", 0) or 0),
                "losses": int(stats.get("losses", 0) or 0),
                "ties": int(stats.get("ties", 0) or 0),
                "pct": float(stats.get("winPercent", 0) or 0),
            }
    print(f"[espn] standings OK ({len(out)} equipos)")
    return out


def load_espn_injury_news() -> dict[str, str]:
    """Designaciones de lesión casi en tiempo real (más rápido que el batch)."""
    out: dict[str, str] = {}
    data = _http_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries")
    if not isinstance(data, dict):
        print("[espn] injuries NO DISPONIBLE (se usará el reporte oficial de nflverse)")
        return out
    for team in data.get("injuries", []):
        for item in team.get("injuries", []):
            ath = item.get("athlete", {}) or {}
            name = ath.get("displayName")
            status = item.get("status")
            if name and status:
                out[name] = status
    print(f"[espn] injuries OK ({len(out)} jugadores)")
    return out


# -------------------------------------------------------------------- clima
def load_weather(games: list[dict]) -> dict[str, dict]:
    """Pronóstico por partido vía Open-Meteo (gratis, sin llave).

    Sólo para estadios al aire libre. El viento es de las variables más
    subestimadas del fantasy: >15 mph hunde el juego aéreo profundo.
    """
    out: dict[str, dict] = {}
    if not games:
        return out
    ok = 0
    for g in games:
        home = g.get("home_team")
        gid = g.get("game_id")
        day = g.get("gameday")
        loc = C.STADIUMS.get(home)
        if not (gid and day and loc):
            continue
        lat, lon, dome = loc
        if dome:
            out[gid] = {"dome": True, "wind_mph": 0.0, "precip_mm": 0.0, "temp_f": 70.0}
            continue
        if (g.get("roof") or "") in ("dome", "closed"):
            out[gid] = {"dome": True, "wind_mph": 0.0, "precip_mm": 0.0, "temp_f": 70.0}
            continue
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&daily=wind_speed_10m_max,precipitation_sum,temperature_2m_max"
               f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
               f"&start_date={day}&end_date={day}&timezone=UTC")
        data = _http_json(url, timeout=15, retries=1)
        if isinstance(data, dict) and "daily" in data:
            d = data["daily"]
            try:
                out[gid] = {
                    "dome": False,
                    "wind_mph": float((d.get("wind_speed_10m_max") or [0])[0] or 0),
                    "precip_mm": float((d.get("precipitation_sum") or [0])[0] or 0),
                    "temp_f": float((d.get("temperature_2m_max") or [60])[0] or 60),
                }
                ok += 1
            except (IndexError, TypeError, ValueError):
                pass
        time.sleep(0.12)  # cortesía con la API pública
    print(f"[clima] {ok} partidos al aire libre con pronóstico")
    return out


# ------------------------------------------------- % de apuestas del público
# Action Network publica el "consenso" (book 15): % de boletos y % de dinero
# por lado. No hay llave ni pago. El formato no está documentado, así que el
# parser busca los campos por patrón y reporta lo que vio (para diagnosticar).
_AN_ABBR = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "LVR": "LV", "OAK": "LV",
            "ARZ": "ARI", "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO",
            "SFO": "SF", "TAM": "TB", "SD": "LAC", "STL": "LA"}
_BROWSER_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36"),
               "Accept": "application/json"}


def _http_json_browser(url: str, timeout: int = 20) -> Any | None:
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_BROWSER_UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError):
            time.sleep(1.5)
    return None


def _pct(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:
        return None
    return round(v * 100, 1) if v <= 1.0 and v != 0 else round(v, 1)


def parse_public_game(game: dict) -> dict:
    """Extrae % del público de un juego de Action Network (v1 o v2)."""
    out: dict[str, float | None] = {}
    # ---- v1: odds = [ {book_id, spread_home_public, ml_home_money, ...}, ... ]
    odds = game.get("odds")
    if isinstance(odds, list):
        cands = [o for o in odds if isinstance(o, dict)
                 and any(k.endswith("_public") and o.get(k) is not None for k in o)]
        cands.sort(key=lambda o: 0 if o.get("book_id") == 15 else 1)
        if cands:
            o = cands[0]
            for k_out, k_in in (("spread_home", "spread_home_public"),
                                ("spread_away", "spread_away_public"),
                                ("ml_home", "ml_home_public"), ("ml_away", "ml_away_public"),
                                ("over", "total_over_public"), ("under", "total_under_public"),
                                ("spread_home_money", "spread_home_money"),
                                ("ml_home_money", "ml_home_money"),
                                ("over_money", "total_over_money")):
                if o.get(k_in) is not None:
                    out[k_out] = _pct(o[k_in])
            if o.get("num_bets") is not None:
                out["num_bets"] = o.get("num_bets")
    # ---- v2: markets -> {book} -> event -> spread/moneyline/total -> [ {side, bet_info} ]
    if not out:
        def walk(node, market=None):
            if isinstance(node, dict):
                side = node.get("side")
                bi = node.get("bet_info")
                if market and side in ("home", "away", "over", "under") and isinstance(bi, dict):
                    t = (bi.get("tickets") or {}).get("percent")
                    m = (bi.get("money") or {}).get("percent")
                    key = {"spread": "spread_", "moneyline": "ml_", "total": ""}.get(market)
                    if key is not None:
                        if t is not None and f"{key}{side}" not in out:
                            out[f"{key}{side}"] = _pct(t)
                        if m is not None and side in ("home", "over") and f"{key}{side}_money" not in out:
                            out[f"{key}{side}_money"] = _pct(m)
                for k, v in node.items():
                    walk(v, k if k in ("spread", "moneyline", "total") else market)
            elif isinstance(node, list):
                for v in node:
                    walk(v, market)
        walk(game.get("markets") or {})
    # completa el lado contrario si sólo vino uno
    for a, b in (("spread_home", "spread_away"), ("ml_home", "ml_away"), ("over", "under")):
        if out.get(a) is not None and out.get(b) is None:
            out[b] = round(100 - out[a], 1)
        if out.get(b) is not None and out.get(a) is None:
            out[a] = round(100 - out[b], 1)
    return {k: v for k, v in out.items() if v is not None}


def _an_teams(game: dict) -> tuple[str | None, str | None]:
    teams = {t.get("id"): t for t in (game.get("teams") or []) if isinstance(t, dict)}
    def ab(tid):
        t = teams.get(tid) or {}
        a = (t.get("abbr") or t.get("abbreviation") or "").upper()
        return _AN_ABBR.get(a, a) or None
    return ab(game.get("home_team_id")), ab(game.get("away_team_id"))


def load_public_betting(games: list[dict]) -> dict:
    """game_id -> {spread_home, spread_away, ml_home, ml_away, over, under, ...} en %."""
    res: dict = {"_available": False, "_diag": ""}
    days = sorted({str(g.get("gameday"))[:10].replace("-", "") for g in games if g.get("gameday")})
    if not days:
        return res
    by_pair = {(g.get("home_team"), g.get("away_team")): g.get("game_id") for g in games}
    seen_keys: set[str] = set()
    n_raw = 0
    for day in days:
        data = None
        for url in (f"https://api.actionnetwork.com/web/v1/scoreboard/nfl?period=game&bookIds=15,30&date={day}",
                    f"https://api.actionnetwork.com/web/v2/scoreboard/nfl?bookIds=15,30&periods=event&date={day}"):
            data = _http_json_browser(url)
            if isinstance(data, dict) and data.get("games"):
                break
        if not isinstance(data, dict):
            continue
        for gm in data.get("games") or []:
            n_raw += 1
            hab, aab = _an_teams(gm)
            gid = by_pair.get((hab, aab))
            if not gid:
                continue
            if isinstance(gm.get("odds"), list) and gm["odds"]:
                seen_keys |= {k for k in gm["odds"][0] if "public" in k or "money" in k}
            pub = parse_public_game(gm)
            if pub:
                res[gid] = pub
        time.sleep(0.3)
    n = len([k for k in res if not k.startswith("_")])
    res["_available"] = n > 0
    res["_diag"] = f"juegos recibidos {n_raw}, con % del público {n}; campos: {sorted(seen_keys)[:12]}"
    print(f"[público] {'OK' if n else 'NO DISPONIBLE'} — {res['_diag']}")
    return res


# ------------------------------------------------------------------- bundle
def load_all(seasons: list[int], fetch_weather_for: list[dict] | None = None) -> dict:
    """Carga todo. Nunca lanza excepción por una fuente externa caída."""
    bundle: dict = {"nflverse": load_nflverse(seasons)}
    bundle["trending"] = load_sleeper_trending()
    bundle["sleeper_players"] = load_sleeper_players()
    bundle["standings"] = load_espn_standings(C.SEASON)
    bundle["espn_injuries"] = load_espn_injury_news()
    bundle["weather"] = load_weather(fetch_weather_for or [])
    return bundle
