"""Validación: ¿las mejoras realmente mejoran? Temporada 2025 completa.

Compara el modelo anterior contra el nuevo (reporte de lesiones + separación +
slot), con la misma regla walk-forward. Si el número no sube, la mejora no es
mejora y hay que revertirla.
"""
from __future__ import annotations

import sys
import time

import numpy as np
import polars as pl
import nflreadpy as nfl
from scipy import stats

sys.path.insert(0, "/home/claude/nfl-edge")
from pipeline import config as C                       # noqa: E402
from pipeline import defense, matchup, players         # noqa: E402

SEASON, FMT = 2025, "half_ppr"


def matrix_upto(ev, week):
    prior = ev.filter((pl.col("season") < SEASON)
                      | ((pl.col("season") == SEASON) & (pl.col("week") < week)))
    if prior.height == 0:
        return pl.DataFrame(), prior
    gl = defense._game_level(prior)
    cur, pri = gl.filter(pl.col("season") == SEASON), gl.filter(pl.col("season") < SEASON)
    gp = int(cur["week"].n_unique()) if cur.height else 0
    parts = []
    for name, wb in C.WINDOWS.items():
        m = defense.build_matrix(cur, wb) if cur.height else pl.DataFrame()
        if m.height:
            parts.append((C.WINDOW_BLEND[name], m))
    if parts:
        tw = sum(w for w, _ in parts)
        acc = parts[0][1].select("team")
        cols = [c for c in parts[0][1].columns if c != "team"]
        exprs = []
        for c in cols:
            terms = []
            for i, (w, m) in enumerate(parts):
                if c in m.columns:
                    acc = acc.join(m.select(["team", pl.col(c).alias(f"{c}__{i}")]),
                                   on="team", how="left")
                    terms.append(pl.col(f"{c}__{i}").fill_null(0.0) * (w / tw))
            if terms:
                exprs.append(sum(terms[1:], terms[0]).alias(c))
        current = acc.with_columns(exprs).select(["team"] + cols)
    else:
        current = pl.DataFrame()
    pm = defense.build_matrix(pri) if pri.height else pl.DataFrame()
    return defense.rank_and_z(defense.blend_with_prior(current, pm, gp)), prior


def evaluate(ev, ros, snaps, ffopp, sched, pg, ngs, inj_by_week, use_new: bool,
             slot: bool = True, sep: bool = True):
    rows = []
    weeks = sorted(sched.filter((pl.col("season") == SEASON)
                                & pl.col("home_score").is_not_null())["week"]
                   .unique().to_list())
    for wk in weeks:
        mtx, prior = matrix_upto(ev, wk)
        if mtx.height == 0:
            continue
        prof = players.build_profiles(prior, ros, snaps, ffopp, SEASON,
                                      ngs=(ngs if (use_new and sep) else None))
        if prof.height == 0:
            continue
        if not (use_new and slot):
            prof = prof.with_columns(pl.lit(False).alias("is_slot"))
        wg = sched.filter((pl.col("season") == SEASON)
                          & (pl.col("week") == wk)).to_dicts()
        sc = matchup.compute(prof, mtx, wg,
                             inj_by_week.get(wk, {}) if use_new else {},
                             {}, defense.METRIC_LABELS, fmt=FMT)
        if sc.height == 0:
            continue
        real = {r["pid"]: players.fantasy_points(r, FMT)
                for r in pg.filter((pl.col("season") == SEASON)
                                   & (pl.col("week") == wk)).to_dicts()}
        pr = prof.to_dicts()
        base = {r["pid"]: (r.get(f"fppg_{FMT}") or 0.0) for r in pr}
        gc = {r["pid"]: (r.get("games") or 0) for r in pr}
        for s in sc.to_dicts():
            if s["pid"] not in real or gc.get(s["pid"], 0) < 3:
                continue
            rows.append({"pos": s["pos"], "light": s["light"],
                         "score": s["matchup_score"],
                         "residual": real[s["pid"]] - base.get(s["pid"], 0.0)})
    return pl.DataFrame(rows)


def report(tag: str, F: pl.DataFrame) -> dict:
    g = F.filter(pl.col("light") == "verde")["residual"].to_numpy()
    r = F.filter(pl.col("light") == "rojo")["residual"].to_numpy()
    y = F.filter(pl.col("light") == "amarillo")["residual"].to_numpy()
    gap = float(g.mean() - r.mean())
    t, p = stats.ttest_ind(g, r, equal_var=False)
    rho = float(np.corrcoef(F["score"].to_numpy().argsort().argsort(),
                            F["residual"].to_numpy().argsort().argsort())[0, 1])
    by = {}
    for pos in ("QB", "RB", "WR", "TE"):
        d = F.filter(pl.col("pos") == pos)
        gv = d.filter(pl.col("light") == "verde")["residual"]
        rv = d.filter(pl.col("light") == "rojo")["residual"]
        if len(gv) >= 25 and len(rv) >= 25:
            by[pos] = round(float(gv.mean()) - float(rv.mean()), 2)
    print(f"\n{tag}")
    print(f"  n={F.height}   verde {g.mean():+.2f} · amarillo {y.mean():+.2f} "
          f"· rojo {r.mean():+.2f}")
    print(f"  BRECHA verde-rojo: {gap:+.3f}   t={t:.2f}   p={p:.5f}   rho={rho:+.3f}")
    print("  por posición: " + "  ".join(f"{k} {v:+.2f}" for k, v in by.items()))
    return {"gap": gap, "t": float(t), "p": float(p), "rho": rho, "by_pos": by}


def main() -> None:
    t0 = time.time()
    print("Cargando…")
    pbp = nfl.load_pbp([2024, SEASON])
    ros = nfl.load_rosters_weekly([2024, SEASON])
    snaps = nfl.load_snap_counts([2024, SEASON])
    ffopp = nfl.load_ff_opportunity([2024, SEASON])
    sched = nfl.load_schedules([2024, SEASON])
    ngs = nfl.load_nextgen_stats([2024, SEASON], "receiving")
    inj = nfl.load_injuries([SEASON])
    ev = defense.build_play_events(pbp, ros)
    pg = players.player_game_usage(ev)

    inj_by_week: dict[int, dict] = {}
    for r in inj.iter_rows(named=True):
        pid = r.get("gsis_id")
        if not pid:
            continue
        e = inj_by_week.setdefault(int(r["week"]), {}).setdefault(
            pid, {"status": "", "practice": ""})
        if r.get("report_status"):
            e["status"] = str(r["report_status"])
        if r.get("practice_status"):
            e["practice"] = str(r["practice_status"])
    print(f"  listo ({time.time()-t0:.0f}s)\n")

    print("=" * 66)
    old = evaluate(ev, ros, snaps, ffopp, sched, pg, ngs, inj_by_week, use_new=False)
    a = report("ANTES  (sin reporte de lesiones, sin separación, sin slot)", old)

    b = report("+ slot solamente",
               evaluate(ev, ros, snaps, ffopp, sched, pg, ngs, inj_by_week,
                        use_new=True, slot=True, sep=False))
    c = report("+ separación solamente",
               evaluate(ev, ros, snaps, ffopp, sched, pg, ngs, inj_by_week,
                        use_new=True, slot=False, sep=True))
    e = report("+ slot y separación",
               evaluate(ev, ros, snaps, ffopp, sched, pg, ngs, inj_by_week,
                        use_new=True, slot=True, sep=True))
    best = max([("slot", b), ("separación", c), ("ambas", e), ("ninguna", a)],
               key=lambda kv: kv[1]["gap"])
    print(f"\n>>> MEJOR CONFIGURACIÓN: {best[0]}  (brecha {best[1]['gap']:+.3f})")
    b = best[1]

    print("\n" + "=" * 66)
    d = b["gap"] - a["gap"]
    print(f"CAMBIO EN LA BRECHA: {d:+.3f} puntos "
          f"({100*d/max(abs(a['gap']),1e-9):+.0f}%)")
    for pos in ("QB", "RB", "WR", "TE"):
        if pos in a["by_pos"] and pos in b["by_pos"]:
            print(f"  {pos}: {a['by_pos'][pos]:+.2f} -> {b['by_pos'][pos]:+.2f}  "
                  f"({b['by_pos'][pos]-a['by_pos'][pos]:+.2f})")
    print(f"  rho: {a['rho']:+.3f} -> {b['rho']:+.3f}")
    print("\nVEREDICTO: " + ("MEJORA CONFIRMADA" if d > 0.02 else
                              "SIN MEJORA — revisar" if d > -0.02 else
                              "EMPEORA — hay que revertir"))
    print(f"Tiempo: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
