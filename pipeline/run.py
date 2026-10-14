"""Orquestador. Corre el pipeline completo y escribe el JSON del dashboard.

    python -m pipeline.run              # semana automática
    python -m pipeline.run --week 5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time

import polars as pl

from . import config as C
from datetime import datetime, timezone

from . import defense, games, ingest, matchup, picks, players


def _clean(o):
    """JSON no acepta NaN/Inf; los convertimos a null."""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else round(o, 5)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def target_week(sched: pl.DataFrame, season: int) -> int:
    """Próxima semana a jugar: la primera con partidos aún sin resultado."""
    s = sched.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    if s.height == 0:
        return 1
    pending = s.filter(pl.col("home_score").is_null())
    if pending.height == 0:
        return int(s["week"].max())
    return int(pending["week"].min())


def injury_map(inj: pl.DataFrame, season: int, week: int,
               espn: dict[str, str], profiles: pl.DataFrame) -> dict[str, dict]:
    """gsis_id -> {status, practice}.

    Incluye la PARTICIPACIÓN EN PRÁCTICAS, no sólo la designación de partido.
    Es el hallazgo más grande del backtest 2025: aparecer en el reporte cuesta
    0.92 puntos fantasy (p=0.0005) aunque el jugador haya practicado completo.
    """
    out: dict[str, dict] = {}
    if inj.height and "gsis_id" in inj.columns:
        d = inj.filter((pl.col("season") == season) & (pl.col("week") == week))
        stale = False
        if d.height == 0:
            # Martes/miércoles: el reporte de ESTA semana aún no sale. Antes se
            # copiaba el de la semana pasada completo (prácticas incluidas), y eso
            # castigaba a jugadores ya sanos. Ahora sólo se arrastra "Out":
            # quien no jugó la semana pasada probablemente siga fuera.
            d = inj.filter((pl.col("season") == season) & (pl.col("week") == week - 1))
            stale = True
        for r in d.iter_rows(named=True):
            pid = r.get("gsis_id")
            if not pid:
                continue
            if stale:
                if str(r.get("report_status") or "") == "Out":
                    out[pid] = {"status": "Out", "practice": ""}
                continue
            e = out.setdefault(pid, {"status": "", "practice": ""})
            if r.get("report_status"):
                e["status"] = str(r["report_status"])
            if r.get("practice_status"):
                e["practice"] = str(r["practice_status"])
    # ESPN por nombre, sólo para rellenar lo que falte
    if espn and profiles.height:
        by_name = {r["name"]: r["pid"] for r in
                   profiles.select(["name", "pid"]).to_dicts() if r.get("name")}
        for name, status in espn.items():
            pid = by_name.get(name)
            if pid and status in ("Out", "Doubtful", "Questionable", "IR"):
                e = out.setdefault(pid, {"status": "", "practice": ""})
                if not e["status"]:
                    e["status"] = status
    return out


def _relevant(p: dict, blk: dict | None = None) -> bool:
    """¿Es un jugador sobre el que alguien tomaría una decisión?

    Filtra suplentes profundos: sin esto, el historial y las alertas se llenan
    de jugadores con 2 snaps cuyo resultado es puro ruido.
    """
    blk = blk or {}
    return ((blk.get("fppg") or 0) >= 4.0
            or (p.get("snap_pct") or 0) >= 40.0
            or (p.get("target_share") or 0) >= 0.10
            or (p.get("carry_share") or 0) >= 0.12)


LEDGER = "ledger.json"


def _load_ledger(season: int) -> dict:
    path = os.path.join(C.DATA_DIR, LEDGER)
    try:
        with open(path, encoding="utf-8") as f:
            led = json.load(f)
        if led.get("season") == season:
            led.setdefault("games", {})
            led.setdefault("players", {})
            return led
    except (OSError, json.JSONDecodeError):
        pass
    return {"season": season, "games": {}, "players": {}}


def _lock_entry(gm: dict, now: datetime, backfill: bool = False) -> dict:
    sp = gm.get("spread_pick") or {}
    ml = gm.get("ml_pick") or {}
    return {
        "week": gm.get("week"), "home": gm.get("home_team"), "away": gm.get("away_team"),
        "kickoff": gm.get("kickoff_utc"), "locked_at": now.isoformat(timespec="minutes"),
        "spread_line": gm.get("vegas_spread"),
        "sp": ({"side": sp.get("side"), "line": sp.get("line"), "tier": sp.get("tier"),
                "dog": sp.get("is_dog")} if sp else None),
        "ml": ({"side": ml.get("side"), "prob": ml.get("prob"), "odds": ml.get("odds")}
               if ml else None),
        "p_home": gm.get("p_home"), "p_mkt": gm.get("p_market_home"),
        "upset": bool(gm.get("upset")), "public": gm.get("public"),
        "backfill": backfill,
    }


def update_ledger(season: int, week: int, preds: list[dict], unified: list[dict],
                  sched: pl.DataFrame, fmt: str) -> dict:
    """Congela cada pick ANTES del kickoff. Lo que se califica es lo que se vio
    antes de que empezara el partido, aunque la corrida llegue tarde."""
    led = _load_ledger(season)
    now = datetime.now(timezone.utc)

    def started(ko: str | None) -> bool:
        if not ko:
            return False
        try:
            return datetime.fromisoformat(ko) <= now
        except ValueError:
            return False

    open_games = set()
    for gm in preds:
        gid = gm.get("game_id")
        if not gid:
            continue
        if not started(gm.get("kickoff_utc")):
            led["games"][gid] = _lock_entry(gm, now)
            open_games.add(gid)

    wk_players = led["players"].setdefault(str(week), {})
    for u in unified:
        if u.get("game_id") not in open_games:
            continue
        blk = u.get(fmt) or {}
        if blk.get("light") and (u.get("games") or 0) >= 3 and _relevant(u, blk):
            wk_players[u["pid"]] = [blk["light"], blk.get("fppg")]
        else:
            wk_players.pop(u["pid"], None)

    # ---- reconstrucción de semanas sin registro (temporada ya empezada)
    have = {e["week"] for e in led["games"].values()}
    forms = picks.ats_forms(sched)
    hist = picks.load_hist()
    for w in range(1, week):
        if w in have:
            continue
        snap = None
        fp = os.path.join(C.DATA_DIR, f"week_{w}.json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    snap = json.load(f)
            except (OSError, json.JSONDecodeError):
                snap = None
        pub_games = {g["game_id"]: g for g in (snap or {}).get("games", [])}
        for g in sched.filter((pl.col("season") == season) & (pl.col("week") == w)
                              & (pl.col("game_type") == "REG")).to_dicts():
            pg = pub_games.get(g["game_id"], {})
            p_home = pg.get("p_home")
            if p_home is None:
                ph, pa = picks.american_to_prob(g.get("home_moneyline")), picks.american_to_prob(g.get("away_moneyline"))
                p_home = ph / (ph + pa) if ph and pa else 0.5
            sp = picks.spread_pick(g, forms.get(g["game_id"]), w, hist)
            gm = {"game_id": g["game_id"], "week": w, "home_team": g["home_team"],
                  "away_team": g["away_team"],
                  "kickoff_utc": (picks.kickoff_utc(g.get("gameday"), g.get("gametime")) or now).isoformat(),
                  "vegas_spread": g.get("spread_line"), "spread_pick": sp,
                  "ml_pick": picks.ml_pick(g, p_home, hist), "p_home": p_home,
                  "p_market_home": pg.get("p_market_home"),
                  "upset": picks.upset_alert(g, sp, p_home), "public": None}
            led["games"][g["game_id"]] = _lock_entry(gm, now, backfill=True)
        if snap and str(w) not in led["players"]:
            sf = snap["meta"].get("default_format", fmt)
            led["players"][str(w)] = {
                p["pid"]: [(p.get(sf) or {}).get("light"), (p.get(sf) or {}).get("fppg")]
                for p in snap.get("players", [])
                if (p.get(sf) or {}).get("light") and (p.get("games") or 0) >= 3
                and _relevant(p, p.get(sf) or {})}

    with open(os.path.join(C.DATA_DIR, LEDGER), "w", encoding="utf-8") as f:
        json.dump(_clean(led), f, ensure_ascii=False, separators=(",", ":"))
    return led


def graded_games(season: int, sched: pl.DataFrame, led: dict) -> dict:
    """Resultado de cada pick ya jugado, para pintar ✓/✗ en semanas pasadas."""
    out: dict[str, dict] = {}
    for r in sched.filter((pl.col("season") == season)
                          & pl.col("home_score").is_not_null()).iter_rows(named=True):
        e = led.get("games", {}).get(r["game_id"])
        if not e:
            continue
        diff = r["home_score"] - r["away_score"]
        g = {"week": e["week"], "home": e["home"], "away": e["away"],
             "hs": r["home_score"], "as": r["away_score"], "line": e.get("spread_line"),
             "bf": e.get("backfill", False), "upset": e.get("upset", False)}
        if e.get("sp") and e.get("spread_line") is not None:
            cm = diff - float(e["spread_line"])
            home_side = e["sp"]["side"] == e["home"]
            g["sp"] = {"side": e["sp"]["side"], "line": e["sp"]["line"], "tier": e["sp"]["tier"],
                       "res": "P" if cm == 0 else ("W" if (cm > 0) == home_side else "L")}
        if e.get("ml"):
            g["ml"] = {"side": e["ml"]["side"], "prob": e["ml"].get("prob"),
                       "res": "P" if diff == 0 else ("W" if (e["ml"]["side"] == e["home"]) == (diff > 0) else "L")}
        out[r["game_id"]] = g
    return out


def build_track_record(season: int, sched: pl.DataFrame, pg: pl.DataFrame,
                       led: dict, fmt: str) -> dict:
    """Califica el registro congelado contra lo que pasó. No es simulación."""
    res = {r["game_id"]: (r["home_score"], r["away_score"])
           for r in sched.filter((pl.col("season") == season)
                                 & pl.col("home_score").is_not_null())
           .iter_rows(named=True)}

    def blank():
        return {"w": 0, "l": 0, "p": 0}

    sp_all, sp_pub = blank(), blank()
    sp_tier: dict[str, dict] = {}
    ml, mk, ups = blank(), blank(), {"n": 0, "cover": 0, "win": 0}
    by_week: dict[int, dict] = {}

    for gid, e in led.get("games", {}).items():
        r = res.get(gid)
        if not r:
            continue
        diff = r[0] - r[1]
        wk = by_week.setdefault(e["week"], {"week": e["week"], "sp": blank(), "ml": blank(),
                                            "strong": blank(), "backfill": e.get("backfill", False)})
        if e.get("sp") and e.get("spread_line") is not None:
            home_side = e["sp"]["side"] == e["home"]
            cm = diff - float(e["spread_line"])
            k = "p" if cm == 0 else ("w" if (cm > 0) == home_side else "l")
            for bucket in (sp_all, wk["sp"], sp_tier.setdefault(e["sp"]["tier"], blank())):
                bucket[k] += 1
            if not e.get("backfill"):
                sp_pub[k] += 1
            if e["sp"]["tier"] in ("fuerte", "media"):
                wk["strong"][k] += 1
            if e.get("upset") and k != "p":
                ups["n"] += 1
                ups["cover"] += int(k == "w")
                ups["win"] += int((diff > 0) == home_side and diff != 0)
        if diff != 0 and e.get("ml"):
            k = "w" if (e["ml"]["side"] == e["home"]) == (diff > 0) else "l"
            ml[k] += 1
            wk["ml"][k] += 1
            if e.get("p_mkt") is not None:
                mk["w" if (e["p_mkt"] > 0.5) == (diff > 0) else "l"] += 1

    def pct(b):
        n = b["w"] + b["l"]
        return round(100 * b["w"] / n, 1) if n else None

    # ---- fantasy por semáforo
    real_pts: dict[tuple[int, str], dict] = {}
    if pg is not None and pg.height:
        for r in pg.filter(pl.col("season") == season).to_dicts():
            real_pts[(int(r["week"]), r["pid"])] = r
    lights: dict[str, dict] = {}
    for w, plist in led.get("players", {}).items():
        for pid, (lg, base) in plist.items():
            rp = real_pts.get((int(w), pid))
            if not lg or rp is None:
                continue
            actual = players.fantasy_points(rp, fmt)
            e = lights.setdefault(lg, {"n": 0, "sum": 0.0})
            e["n"] += 1
            e["sum"] += actual - (base or 0.0)

    graded = ml["w"] + ml["l"]
    out = {
        "available": graded > 0,
        "spread": {**sp_all, "pct": pct(sp_all)},
        "spread_published": {**sp_pub, "pct": pct(sp_pub)},
        "spread_tiers": {t: {**b, "pct": pct(b)} for t, b in sp_tier.items()},
        "moneyline": {**ml, "pct": pct(ml)},
        "market": {**mk, "pct": pct(mk)},
        "upset": {**ups, "cover_pct": round(100 * ups["cover"] / ups["n"], 1) if ups["n"] else None},
        "by_week": [{"week": v["week"], "backfill": v["backfill"],
                     "sp": v["sp"], "sp_pct": pct(v["sp"]),
                     "strong": v["strong"], "ml": v["ml"], "ml_pct": pct(v["ml"])}
                    for _, v in sorted(by_week.items())],
        "fantasy": {k: {"n": v["n"], "vs_promedio": round(v["sum"] / v["n"], 2)}
                    for k, v in lights.items() if v["n"]},
    }
    # compatibilidad con el reporte semanal y la versión anterior de la página
    out["games"] = {"n": graded, "acc": pct(ml), "vegas_acc": pct(mk),
                    "ats_n": sp_all["w"] + sp_all["l"], "ats_acc": pct(sp_all)}
    return out


def run(season: int = C.SEASON, week: int | None = None) -> dict:
    t0 = time.time()
    print(f"\n{'='*62}\nNFL EDGE {season} — corrida {C.stamp()}\n{'='*62}")

    seasons = sorted({season, *C.PRIOR_SEASONS})
    nv = ingest.load_nflverse(seasons, season=season)
    sched = nv["schedules"]
    if sched.height == 0:
        raise SystemExit("No se pudo cargar el calendario; se aborta la corrida.")

    wk = week or target_week(sched, season)
    print(f"\n[semana objetivo] {season} semana {wk}")

    week_games = (sched.filter((pl.col("season") == season) & (pl.col("week") == wk))
                  .to_dicts())
    print(f"[calendario] {len(week_games)} partidos")

    # fuentes externas (degradan con elegancia si no responden)
    trending = ingest.load_sleeper_trending()
    espn_inj = ingest.load_espn_injury_news()
    weather = ingest.load_weather(week_games)
    public = ingest.load_public_betting(week_games)

    # ---------------------------------------------------------- motores
    print("\n[motor] matriz defensiva ajustada por rival…")
    dres = defense.build(nv["pbp"], nv["rosters"], season)
    matrix, ev, gl = dres["matrix"], dres.get("events"), dres.get("game_level")
    print(f"  {matrix.height} defensas · {dres['games_played']} semanas jugadas "
          f"· confianza {dres['confidence']}")

    print("[motor] perfiles de jugador…")
    prof = players.build_profiles(ev, nv["rosters"], nv["snaps"], nv["ffopp"], season,
                                  current_roster=nv.get("rosters_current"),
                                  ngs=nv.get("ngs_rec"))
    print(f"  {prof.height} jugadores de posición fantasy")

    inj = injury_map(nv["injuries"], season, wk, espn_inj, prof)
    print(f"  {len(inj)} jugadores con designación de lesión")

    print("[motor] cruce jugador × defensa (3 formatos)…")
    per_fmt: dict[str, pl.DataFrame] = {}
    for fmt in C.SCORING_FORMATS:
        per_fmt[fmt] = matchup.compute(
            prof, matrix, week_games, inj, weather,
            defense.METRIC_LABELS, fmt=fmt, confidence=dres["confidence"])
        print(f"  {fmt:<9} {per_fmt[fmt].height} jugador-matchups")

    print("[motor] modelo de partidos…")
    preds, gmeta = games.predict_week(sched, gl, season, wk, public=public,
                                      weather=weather)
    print(f"  {len(preds)} predicciones · backtest {gmeta['backtest']}")

    # ------------------------------------------------- unificar por jugador
    base_fmt = C.DEFAULT_FORMAT
    if per_fmt[base_fmt].height == 0:
        raise SystemExit("El cruce no produjo resultados; revisa los datos de entrada.")

    sleeper_ids = {r["pid"]: r.get("sleeper_id")
                   for r in prof.select(["pid", "sleeper_id"]).to_dicts()}
    idx: dict[str, dict] = {}
    for fmt, df in per_fmt.items():
        for r in df.to_dicts():
            e = idx.setdefault(r["pid"], {
                k: r[k] for k in ("pid", "name", "pos", "team", "opp", "is_home",
                                  "game_id", "gameday", "status", "snap_pct",
                                  "target_share", "carry_share", "wopr", "usage_trend",
                                  "xfp_pg", "fp_over_xfp", "games", "opportunities_pg",
                                  "implied_total", "spread_own", "confidence",
                                  "practice", "separation", "sep_factor", "is_slot",
                                  "risk_level", "risk_txt", "risk_pen")
            })
            e["sleeper_id"] = sleeper_ids.get(r["pid"])
            e[fmt] = {
                "matchup_score": r["matchup_score"], "start_score": r["start_score"],
                "quality_score": r["quality_score"], "light": r["light"],
                "fppg": r["fppg"], "channels": r["channels"], "insight": r["insight"],
            }

    unified = list(idx.values())

    # waivers sobre el formato por defecto, enriquecidos con el mercado
    wdf = per_fmt[base_fmt].join(
        prof.select(["pid", "sleeper_id"]), on="pid", how="left")
    waivers = matchup.waiver_ranking(wdf, trending)
    wmap = {r["pid"]: {"waiver_score": r["waiver_score"],
                       "before_the_herd": r["before_the_herd"],
                       "market_trend": r["market_trend"]}
            for r in waivers.to_dicts()}
    for u in unified:
        u.update(wmap.get(u["pid"], {"waiver_score": 0.0, "before_the_herd": False,
                                     "market_trend": 0.0}))

    # ------------------------------------------------------- alertas de cambio
    prev_path = os.path.join(C.DATA_DIR, f"week_{wk-1}.json")
    changes: list[dict] = []
    if os.path.exists(prev_path):
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev = json.load(f)
            pm = {p["pid"]: p for p in prev.get("players", [])}
            for u in unified:
                p = pm.get(u["pid"])
                if not p:
                    continue
                old = (p.get(base_fmt) or {}).get("light")
                new = u[base_fmt]["light"]
                if old and new and old != new and _relevant(u, u[base_fmt]):
                    changes.append({
                        "pid": u["pid"], "name": u["name"], "pos": u["pos"],
                        "team": u["team"], "opp": u["opp"], "from": old, "to": new,
                        "score": u[base_fmt]["matchup_score"],
                        "delta": round(u[base_fmt]["matchup_score"]
                                       - (p.get(base_fmt) or {}).get("matchup_score", 0), 1),
                    })
            changes.sort(key=lambda c: -abs(c["delta"]))
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    print("[motor] historial real de aciertos…")
    pg_all = players.player_game_usage(ev) if ev is not None and ev.height else pl.DataFrame()
    led = update_ledger(season, wk, preds, unified, sched, base_fmt)
    track = build_track_record(season, sched, pg_all, led, base_fmt)
    if track.get("available"):
        print(f"  {track['games']['n']} partidos calificados · "
              f"moneyline {track['moneyline']['pct']}% · Vegas {track['market']['pct']}% · "
              f"spread {track['spread']['w']}-{track['spread']['l']}-{track['spread']['p']}")
    else:
        print("  aún sin semanas anteriores que calificar")

    graded = graded_games(season, sched, led)

    payload = {
        "meta": {
            "season": season, "week": wk,
            "generated_at": C.stamp(),
            "generated_iso": C.now_local().isoformat(),
            "data_confidence": dres["confidence"],
            "weeks_played": dres["games_played"],
            "formats": list(C.SCORING_FORMATS.keys()),
            "default_format": base_fmt,
            "sources": {
                "nflverse": True,
                "sleeper_trending": bool(trending.get("_available")),
                "espn_injuries": bool(espn_inj),
                "weather": any(not v.get("dome") for v in weather.values()),
                "public_betting": bool(public.get("_available")),
                "public_diag": public.get("_diag", ""),
            },
            "picks_hist": picks.load_hist(),
            "season_pattern": _clean(picks.season_pattern(sched, season, wk)),
            "backtest": gmeta["backtest"],
            "track_record": _clean(track),
            "market_rules": {
                "anchor": C.MARKET_ANCHOR,
                "disagree_min_pts": C.DISAGREE_MIN_PTS,
                "disagree_strong_pts": C.DISAGREE_STRONG_PTS,
                "ats_min_edge": C.ATS_MIN_EDGE,
                "consensus_required": C.CONSENSUS_REQUIRED,
            },
            "calibration": _clean(gmeta["calibration"]),
            "runtime_sec": round(time.time() - t0, 1),
        },
        "players": _clean(unified),
        "games": _clean(preds),
        "defense": _clean(matrix.to_dicts()),
        "metric_labels": defense.METRIC_LABELS,
        "changes": _clean(changes[:40]),
        "graded": _clean(graded),
    }

    out = os.path.join(C.DATA_DIR, f"week_{wk}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(C.DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    idxp = os.path.join(C.DATA_DIR, "index.json")
    weeks = sorted({int(fn.split("_")[1].split(".")[0])
                    for fn in os.listdir(C.DATA_DIR) if fn.startswith("week_")})
    with open(idxp, "w", encoding="utf-8") as f:
        json.dump({"season": season, "weeks": weeks, "current": wk,
                   "updated": C.stamp()}, f, ensure_ascii=False)

    size = os.path.getsize(out) / 1024
    print(f"\n[listo] {out} ({size:.0f} KB) en {time.time()-t0:.1f}s")
    print(f"  jugadores {len(unified)} · partidos {len(preds)} · cambios {len(changes)}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="NFL EDGE 2026")
    ap.add_argument("--season", type=int, default=C.SEASON)
    ap.add_argument("--week", type=int, default=None)
    a = ap.parse_args()
    run(a.season, a.week)


if __name__ == "__main__":
    main()
