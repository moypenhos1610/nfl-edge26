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
               espn: dict[str, str], profiles: pl.DataFrame) -> dict[str, str]:
    """gsis_id -> estatus. Combina el reporte oficial con la señal rápida de ESPN."""
    out: dict[str, str] = {}
    if inj.height and "gsis_id" in inj.columns:
        d = inj.filter((pl.col("season") == season) & (pl.col("week") == week))
        if d.height == 0:
            d = inj.filter(pl.col("season") == season)
            if d.height:
                d = d.filter(pl.col("week") == d["week"].max())
        for r in d.iter_rows(named=True):
            if r.get("gsis_id") and r.get("report_status"):
                out[r["gsis_id"]] = str(r["report_status"])
    # ESPN por nombre, sólo para rellenar lo que falte
    if espn and profiles.height:
        by_name = {r["name"]: r["pid"] for r in
                   profiles.select(["name", "pid"]).to_dicts() if r.get("name")}
        for name, status in espn.items():
            pid = by_name.get(name)
            if pid and pid not in out and status in ("Out", "Doubtful", "Questionable", "IR"):
                out[pid] = status
    return out


def run(season: int = C.SEASON, week: int | None = None) -> dict:
    t0 = time.time()
    print(f"\n{'='*62}\nNFL EDGE {season} — corrida {C.stamp()}\n{'='*62}")

    seasons = sorted({season, *C.PRIOR_SEASONS})
    nv = ingest.load_nflverse(seasons)
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
                                  current_roster=nv.get("rosters_current"))
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
                                  "implied_total", "spread_own", "confidence")
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
                if old and new and old != new:
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
