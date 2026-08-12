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
from . import defense, games, ingest, matchup, players


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
        if d.height == 0:
            d = inj.filter(pl.col("season") == season)
            if d.height:
                d = d.filter(pl.col("week") == d["week"].max())
        for r in d.iter_rows(named=True):
            pid = r.get("gsis_id")
            if not pid:
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


def build_track_record(season: int, sched: pl.DataFrame,
                       pg: pl.DataFrame) -> dict:
    """Historial REAL de aciertos, calificando lo que ya publicamos.

    No es una simulación: lee los JSON de semanas anteriores (las predicciones
    tal como se publicaron ese martes) y las califica contra lo que pasó.
    """
    res = {r["game_id"]: (r["home_score"], r["away_score"])
           for r in sched.filter((pl.col("season") == season)
                                 & pl.col("home_score").is_not_null())
           .iter_rows(named=True)}
    real_pts: dict[tuple[int, str], dict] = {}
    if pg is not None and pg.height:
        for r in pg.filter(pl.col("season") == season).to_dicts():
            real_pts[(int(r["week"]), r["pid"])] = r

    g_tot = g_ok = v_tot = v_ok = ats_tot = ats_ok = 0
    weeks: list[dict] = []
    lights: dict[str, dict] = {}

    for fn in sorted(os.listdir(C.DATA_DIR)):
        if not (fn.startswith("week_") and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(C.DATA_DIR, fn), encoding="utf-8") as f:
                snap = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if snap.get("meta", {}).get("season") != season:
            continue
        wk = snap["meta"]["week"]
        wg_ok = wg_tot = 0
        for gm in snap.get("games", []):
            a = res.get(gm.get("game_id"))
            if not a or a[0] == a[1]:
                continue
            home_won = a[0] > a[1]
            g_tot += 1
            wg_tot += 1
            if (gm.get("p_home") or 0) > 0.5 and home_won or \
               (gm.get("p_home") or 0) <= 0.5 and not home_won:
                g_ok += 1
                wg_ok += 1
            pm = gm.get("p_market_home")
            if pm is not None:
                v_tot += 1
                if (pm > 0.5) == home_won:
                    v_ok += 1
            sp, rm = gm.get("vegas_spread"), gm.get("raw_margin")
            if sp is not None and rm is not None and gm.get("ats_pick"):
                ats_tot += 1
                if (rm > sp) == ((a[0] - a[1]) > sp):
                    ats_ok += 1
        if wg_tot:
            weeks.append({"week": wk, "n": wg_tot,
                          "acc": round(100 * wg_ok / wg_tot, 1)})

        fmt = snap["meta"].get("default_format", C.DEFAULT_FORMAT)
        for p in snap.get("players", []):
            blk = p.get(fmt) or {}
            lg = blk.get("light")
            rp = real_pts.get((wk, p.get("pid")))
            if not lg or rp is None or (p.get("games") or 0) < 3:
                continue
            # sólo jugadores con rol real: calificar suplentes de 2 snaps
            # llena el historial de ruido y no refleja ninguna decisión tuya
            if not _relevant(p, blk):
                continue
            from . import players as _pl
            actual = _pl.fantasy_points(rp, fmt)
            base = blk.get("fppg") or 0.0
            e = lights.setdefault(lg, {"n": 0, "sum": 0.0})
            e["n"] += 1
            e["sum"] += actual - base

    out = {"available": g_tot > 0, "games": {}, "fantasy": {}, "by_week": weeks}
    if g_tot:
        out["games"] = {
            "n": g_tot, "acc": round(100 * g_ok / g_tot, 1),
            "vegas_acc": (round(100 * v_ok / v_tot, 1) if v_tot else None),
            "ats_n": ats_tot,
            "ats_acc": (round(100 * ats_ok / ats_tot, 1) if ats_tot else None),
        }
    if lights:
        out["fantasy"] = {k: {"n": v["n"], "vs_promedio": round(v["sum"] / v["n"], 2)}
                          for k, v in lights.items() if v["n"]}
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
    preds, gmeta = games.predict_week(sched, gl, season, wk)
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
    track = build_track_record(season, sched, pg_all)
    if track.get("available"):
        print(f"  {track['games']['n']} partidos calificados · "
              f"modelo {track['games']['acc']}% · Vegas {track['games']['vegas_acc']}%")
    else:
        print("  aún sin semanas anteriores que calificar")

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
                "weather": len(weather) > 0,
            },
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
