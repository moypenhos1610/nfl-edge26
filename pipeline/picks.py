"""Picks por partido: spread, moneyline y alerta de upset.

Lo que se probó (2006-2025, 5,000+ partidos, dos épocas independientes) y lo
que sobrevivió está documentado en tests/validar_picks.py. Resumen:

  * La única señal contra el spread que se replicó en las dos épocas es la
    REGRESIÓN ATS: los equipos que vienen cubriendo mucho quedan caros en la
    línea siguiente, y los que vienen fallando quedan baratos.
        brecha >= 0.25  ->  55% histórico   (~13% de los partidos)
        brecha >= 0.20  ->  53.5%           (~20% de los partidos)
  * Perros en semanas 1-5: 54% en 2016-2025, se apaga desde la semana 6 (50%).
  * Todo lo demás (Elo vs línea, suerte pitagórica, rebote, descanso, perro
    local, perro divisional) NO se replicó: se queda como contexto, no decide.
  * Moneyline: el mercado está bien calibrado; ningún filtro de "upset directo"
    le ganó. El pick de moneyline es el equipo con más probabilidad.

El punto de equilibrio contra el spread con momio -110 es 52.4%.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import polars as pl

from . import config as C

ET = ZoneInfo("America/New_York")
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "picks_hist.json")

# Umbrales de la brecha de forma ATS (tasa de cobertura encogida, ver _rate)
GAP_STRONG = 0.25
GAP_MEDIUM = 0.20
GAP_LEAN = 0.10
SHRINK_K = 4.0          # (cubiertos + K/2) / (jugados + K): 2 cubiertos y 2 fallados ficticios
EARLY_DOG_WEEKS = 5


# ------------------------------------------------------------------ util
def _rate(c: int, n: int) -> float:
    return (c + SHRINK_K / 2) / (n + SHRINK_K)


def american_to_prob(ml) -> float | None:
    if ml is None:
        return None
    ml = float(ml)
    return (-ml) / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


def american_to_decimal(ml) -> float | None:
    if ml is None:
        return None
    ml = float(ml)
    return 1 + (100.0 / -ml if ml < 0 else ml / 100.0)


def line_txt(team_line: float) -> str:
    """Línea desde la perspectiva de un equipo: -3.5 / +7 / PK."""
    if team_line == 0:
        return "PK"
    v = f"{abs(team_line):.1f}".rstrip("0").rstrip(".")
    return ("+" if team_line > 0 else "-") + v


def kickoff_utc(gameday: str | None, gametime: str | None) -> datetime | None:
    if not gameday:
        return None
    try:
        hh, mm = (gametime or "13:00").split(":")[:2]
        y, mo, d = (int(x) for x in str(gameday)[:10].split("-"))
        return datetime(y, mo, d, int(hh), int(mm), tzinfo=ET).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def load_hist() -> dict:
    try:
        with open(HIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------- forma ATS por equipo
def ats_forms(sched: pl.DataFrame) -> dict[str, dict]:
    """Para CADA partido (jugado o no), la forma ATS de ambos equipos ANTES de él.

    Devuelve game_id -> {"home": {...}, "away": {...}}. Sólo usa partidos de
    temporada regular de la misma temporada, ya terminados y con línea.
    """
    need = {"game_id", "season", "week", "home_team", "away_team", "spread_line",
            "home_score", "away_score"}
    if sched.height == 0 or not need.issubset(set(sched.columns)):
        return {}
    d = sched
    if "game_type" in d.columns:
        d = d.filter(pl.col("game_type") == "REG")
    sort_cols = [c for c in ("season", "week", "gameday", "gametime") if c in d.columns]
    d = d.sort(sort_cols)

    state: dict[tuple[str, int], dict] = {}
    out: dict[str, dict] = {}
    # partidos agrupados por (temporada, semana): la forma se congela al inicio
    # de la semana para que el jueves y el domingo vean lo mismo
    rows = d.to_dicts()
    i = 0
    while i < len(rows):
        s, w = rows[i]["season"], rows[i]["week"]
        j = i
        while j < len(rows) and rows[j]["season"] == s and rows[j]["week"] == w:
            j += 1
        block = rows[i:j]
        for r in block:
            f = {}
            for side, t in (("home", r["home_team"]), ("away", r["away_team"])):
                e = state.get((t, s), {"c": 0, "l": 0, "p": 0, "seq": [], "mg": 0.0,
                                       "w": 0, "gl": 0, "exp_w": 0.0})
                n = e["c"] + e["l"]
                f[side] = {"cov": e["c"], "fail": e["l"], "push": e["p"], "n": n,
                           "rate": _rate(e["c"], n), "seq": e["seq"][-5:],
                           "avg_margin": (e["mg"] / (n + e["p"])) if (n + e["p"]) else 0.0,
                           "wins": e["w"], "games": e["gl"],
                           "wins_over_exp": e["w"] - e["exp_w"]}
            out[r["game_id"]] = f
        for r in block:
            if r["home_score"] is None or r["away_score"] is None or r["spread_line"] is None:
                continue
            res = r["home_score"] - r["away_score"]
            cm = res - r["spread_line"]
            p_home = 1 / (1 + 10 ** (-float(r["spread_line"]) / 13.5))
            for t, m, pw, won in ((r["home_team"], cm, p_home, res > 0),
                                  (r["away_team"], -cm, 1 - p_home, res < 0)):
                e = state.setdefault((t, s), {"c": 0, "l": 0, "p": 0, "seq": [], "mg": 0.0,
                                              "w": 0, "gl": 0, "exp_w": 0.0})
                if m > 0:
                    e["c"] += 1
                    e["seq"].append("C")
                elif m < 0:
                    e["l"] += 1
                    e["seq"].append("F")
                else:
                    e["p"] += 1
                    e["seq"].append("P")
                e["mg"] += m
                e["gl"] += 1
                e["w"] += 1 if won else 0
                e["exp_w"] += pw
        i = j
    return out


# --------------------------------------------------------------- pick spread
def spread_pick(g: dict, form: dict | None, week: int, hist: dict | None = None) -> dict | None:
    """Lado del spread para un partido. Siempre devuelve un lado si hay línea."""
    sp = g.get("spread_line")
    if sp is None:
        return None
    sp = float(sp)
    h, a = g["home_team"], g["away_team"]
    hist = hist or {}
    tiers = hist.get("spread_tiers", {})
    fh = (form or {}).get("home") or {"rate": .5, "cov": 0, "n": 0, "seq": []}
    fa = (form or {}).get("away") or {"rate": .5, "cov": 0, "n": 0, "seq": []}
    gap = fa["rate"] - fh["rate"]          # >0: el visitante viene más "caliente" -> local

    dog = h if sp < 0 else (a if sp > 0 else None)
    reasons: list[dict] = []

    if abs(gap) >= GAP_MEDIUM:
        side = h if gap > 0 else a
        tier = "fuerte" if abs(gap) >= GAP_STRONG else "media"
        hot, cold = (a, h) if gap > 0 else (h, a)
        fhot = fa if gap > 0 else fh
        fcold = fh if gap > 0 else fa
        reasons.append({"k": "regresion",
                        "t": f"{hot} cubrió {fhot['cov']} de {fhot['n']}: el mercado ya lo cobra caro"})
        if fcold["n"]:
            reasons.append({"k": "regresion",
                            "t": f"{cold} cubrió {fcold['cov']} de {fcold['n']}: llega barato"})
    elif week <= EARLY_DOG_WEEKS and dog:
        side, tier = dog, "leve"
        reasons.append({"k": "perro_temprano",
                        "t": "Semanas 1-5: los perros cubren 54% (2016-2025)"})
    else:
        tier = "moneda"
        if abs(gap) >= GAP_LEAN:
            side = h if gap > 0 else a
            reasons.append({"k": "leve", "t": "Ligera inclinación por forma ATS, sin ventaja probada"})
        else:
            side = dog or h
            reasons.append({"k": "leve", "t": "Sin señal: es una moneda al aire"})

    team_line = -sp if side == h else sp
    st = tiers.get(tier, {})
    return {
        "side": side, "line": team_line, "txt": f"{side} {line_txt(team_line)}",
        "tier": tier, "gap": round(gap, 3), "is_dog": side == dog,
        "hist_pct": st.get("pct"), "hist_n": st.get("n"),
        "reasons": reasons,
        "form": {h: {"cov": fh["cov"], "n": fh["n"], "seq": fh.get("seq", [])},
                 a: {"cov": fa["cov"], "n": fa["n"], "seq": fa.get("seq", [])}},
    }


# ------------------------------------------------------------ pick moneyline
def ml_pick(g: dict, p_home: float, hist: dict | None = None) -> dict:
    h, a = g["home_team"], g["away_team"]
    side, p = (h, p_home) if p_home >= 0.5 else (a, 1 - p_home)
    ml = g.get("home_moneyline") if side == h else g.get("away_moneyline")
    buckets = (hist or {}).get("ml_buckets", [])
    hb = next((b for b in buckets if b["lo"] <= p < b["hi"]), None)
    dec = american_to_decimal(ml)
    return {"side": side, "prob": round(p, 4), "odds": ml,
            "pays_per_100": round((dec - 1) * 100) if dec else None,
            "hist_pct": hb["pct"] if hb else None, "hist_n": hb["n"] if hb else None}


# ------------------------------------------------------------- alerta upset
def upset_alert(g: dict, sp_pick: dict | None, p_home: float) -> dict | None:
    """'OJO CON EL UPSET': la señal validada apunta a que el PERRO cubra."""
    if not sp_pick or not sp_pick["is_dog"] or sp_pick["tier"] not in ("fuerte", "media"):
        return None
    dog = sp_pick["side"]
    p_dog = p_home if dog == g["home_team"] else 1 - p_home
    level = "alto" if sp_pick["tier"] == "fuerte" else "medio"
    return {"team": dog, "line": sp_pick["txt"], "level": level,
            "p_win": round(p_dog, 3),
            "outright": p_dog >= 0.35,
            "txt": (f"{dog} puede cubrir {line_txt(sp_pick['line'])}"
                    + (f" y hasta ganar ({round(100*p_dog)}%)" if p_dog >= 0.35 else ""))}


# ----------------------------------------------- lo que ve el modelo (corto)
def model_sees(g: dict, pred: dict, form: dict | None, epa_rank: dict, pub: dict | None,
               weather: dict | None) -> list[dict]:
    """Máximo 4 frases cortas, cada una con un número. Prioridad por utilidad."""
    h, a = g["home_team"], g["away_team"]
    out: list[dict] = []

    # 1) récord que engaña: récord vs calidad real (EPA ajustada por rival)
    for t, rec in ((h, pred.get("home_record")), (a, pred.get("away_record"))):
        r = epa_rank.get(t)
        if not rec or r is None:
            continue
        try:
            w, l = (int(x) for x in rec.split("-")[:2])
        except ValueError:
            continue
        if w + l < 3:
            continue
        wp = w / (w + l)
        if wp >= 0.66 and r >= 20:
            out.append({"ic": "mask", "t": f"{t} va {rec} pero juega como el #{r} de 32 (EPA)"})
        elif wp <= 0.34 and r <= 12:
            out.append({"ic": "mask", "t": f"{t} va {rec} pero juega como el #{r} de 32 (EPA)"})

    # 2) forma ATS extrema
    if form:
        for t, f in ((h, form.get("home")), (a, form.get("away"))):
            if f and f["n"] >= 3 and (f["cov"] / f["n"] >= .75 or f["cov"] / f["n"] <= .25):
                hot = f["cov"] / f["n"] >= .75
                out.append({"ic": "fire" if hot else "ice",
                            "t": f"{t} {f['cov']}-{f['n']-f['cov']} contra el spread"
                                 + (" → caro" if hot else " → barato")})

    # (La "línea justa" del modelo puro NO se muestra: medido en 2025, cuando
    #  discrepa fuerte de Vegas acierta MENOS. Enseñarla invita a apostarla.)

    # 3) descanso
    hr, ar = g.get("home_rest"), g.get("away_rest")
    if hr is not None and ar is not None and abs(hr - ar) >= 3:
        t = h if hr > ar else a
        out.append({"ic": "bed", "t": f"{t} con {abs(int(hr - ar))} días más de descanso"})

    # 5) público cargado
    if pub and pub.get("spread_home") is not None:
        ph = pub["spread_home"]
        if ph >= 70 or ph <= 30:
            t = h if ph >= 70 else a
            out.append({"ic": "crowd", "t": f"{round(max(ph, 100 - ph))}% del público va con {t}"})

    # 6) viento
    if weather and not weather.get("dome") and (weather.get("wind_mph") or 0) >= C.WIND_PENALTY_START:
        out.append({"ic": "wind", "t": f"Viento de {round(weather['wind_mph'])} mph: menos pase profundo"})
    return out[:4]


# --------------------------------------------------------- patrón de temporada
def season_pattern(sched: pl.DataFrame, season: int, week: int) -> dict:
    """¿Hubo más upsets de lo normal? Medido, no percibido."""
    d = sched.filter((pl.col("game_type") == "REG") & pl.col("home_score").is_not_null()
                     & pl.col("spread_line").is_not_null() & (pl.col("spread_line") != 0))
    cur = d.filter((pl.col("season") == season) & (pl.col("week") < week))
    if cur.height == 0:
        return {"available": False}

    def stats(df: pl.DataFrame) -> dict:
        n = fav = exp = cov = ncov = big_n = big_cov = big_up = 0
        for r in df.iter_rows(named=True):
            res = r["home_score"] - r["away_score"]
            if res == 0:
                continue
            sp = float(r["spread_line"])
            fav_home = sp > 0
            n += 1
            fav += int((res > 0) == fav_home)
            ph, pa = american_to_prob(r.get("home_moneyline")), american_to_prob(r.get("away_moneyline"))
            if ph and pa:
                pf = ph / (ph + pa)
                exp += pf if fav_home else 1 - pf
            else:
                exp += 1 / (1 + 10 ** (-abs(sp) / 13.5))
            cm = res - sp
            if cm != 0:
                ncov += 1
                dog_cov = (cm < 0) if fav_home else (cm > 0)
                cov += int(dog_cov)
                if abs(sp) >= 7:
                    big_n += 1
                    big_cov += int(dog_cov)
                    big_up += int((res > 0) != fav_home)
        return {"n": n, "fav_win": round(100 * fav / n, 1) if n else None,
                "fav_expected": round(100 * exp / n, 1) if n else None,
                "upsets": n - fav, "upsets_expected": round(n - exp, 1),
                "dog_cover": round(100 * cov / ncov, 1) if ncov else None,
                "big_dog_n": big_n, "big_dog_cover": round(100 * big_cov / big_n, 1) if big_n else None,
                "big_dog_upsets": big_up}

    by_week = []
    for w in sorted(cur["week"].unique().to_list()):
        s = stats(cur.filter(pl.col("week") == w))
        by_week.append({"week": w, "upsets": s["upsets"], "expected": s["upsets_expected"],
                        "dog_cover": s["dog_cover"]})
    hist = d.filter((pl.col("season") < season) & (pl.col("season") >= season - 10))
    return {"available": True, "season": stats(cur), "by_week": by_week,
            "hist_early": stats(hist.filter(pl.col("week") <= 5)),
            "hist_late": stats(hist.filter(pl.col("week") >= 6)),
            "hist_seasons": f"{season-10}-{season-1}"}
