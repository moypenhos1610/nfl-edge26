"""¿Se puede arreglar el hoyo de los receptores abiertos?

Hipótesis: el problema es que tratamos a todos los WR igual. Un receptor de
slot y uno abierto enfrentan defensores distintos, y las defensas son buenas
contra uno y malas contra el otro. Si separamos SLOT vs ABIERTO, la señal
debería aparecer.

Se prueban tres arreglos, todos con datos gratuitos:
  1. Separar la matriz defensiva en "vs slot" y "vs abierto" (ngs_position)
  2. Añadir separación promedio del receptor (Next Gen Stats)
  3. Añadir participación en prácticas (Full / Limited / DNP)
"""
from __future__ import annotations

import sys
import time

import numpy as np
import polars as pl
import nflreadpy as nfl

sys.path.insert(0, "/home/claude/nfl-edge")
from pipeline import defense, matchup, players        # noqa: E402

SEASON = 2025
FMT = "half_ppr"


# ---------------------------------------------------------------- 1) slot/wide
def positions_split(rosters: pl.DataFrame) -> pl.DataFrame:
    """Igual que defense._positions pero separando WR de slot y abierto."""
    return (rosters
            .filter(pl.col("gsis_id").is_not_null())
            .select(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32),
                    pl.col("gsis_id").alias("pid"),
                    pl.col("position").alias("p"), pl.col("ngs_position").alias("ngs"))
            .with_columns(
                pl.when(pl.col("p").is_in(["RB", "FB", "HB"])).then(pl.lit("RB"))
                .when((pl.col("p") == "WR") & (pl.col("ngs") == "SLOT_WR")).then(pl.lit("SWR"))
                .when(pl.col("p") == "WR").then(pl.lit("WR"))
                .when(pl.col("p") == "TE").then(pl.lit("TE"))
                .when(pl.col("p") == "QB").then(pl.lit("QB"))
                .otherwise(pl.lit("OTHER")).alias("pos"))
            .drop(["p", "ngs"])
            .unique(subset=["season", "week", "pid"], keep="first"))


def main() -> None:
    t0 = time.time()
    print("Cargando…")
    pbp = nfl.load_pbp([2024, SEASON])
    ros = nfl.load_rosters_weekly([2024, SEASON])
    snaps = nfl.load_snap_counts([2024, SEASON])
    ffopp = nfl.load_ff_opportunity([2024, SEASON])
    sched = nfl.load_schedules([2024, SEASON])
    inj = nfl.load_injuries([SEASON])
    ngs = nfl.load_nextgen_stats([2024, SEASON], "receiving")

    # --- eventos con la etiqueta de slot
    orig = defense._positions
    defense._positions = positions_split
    ev_split = defense.build_play_events(pbp, ros)
    defense._positions = orig
    ev = defense.build_play_events(pbp, ros)

    cov = ev_split.filter(pl.col("rec_pos") == "SWR").height
    print(f"  objetivos etiquetados como slot: {cov} "
          f"({100*cov/max(1,ev_split.filter(pl.col('is_target')).height):.0f}% de los targets)")

    # --- separación promedio por jugador (NGS)
    sep = {}
    if ngs.height and "avg_separation" in ngs.columns:
        s = (ngs.filter(pl.col("season") == SEASON)
             .group_by("player_gsis_id" if "player_gsis_id" in ngs.columns
                       else "player_id")
             .agg(pl.col("avg_separation").mean().alias("sep")))
        sep = {r[s.columns[0]]: r["sep"] for r in s.to_dicts()}
    print(f"  jugadores con dato de separación: {len(sep)}")

    # --- participación en prácticas por semana
    prac: dict[tuple[int, str], str] = {}
    if inj.height:
        for r in inj.iter_rows(named=True):
            if r.get("gsis_id") and r.get("practice_status"):
                prac[(int(r["week"]), r["gsis_id"])] = str(r["practice_status"])
    print(f"  registros de práctica: {len(prac)}")

    pg = players.player_game_usage(ev)
    weeks = sorted(sched.filter((pl.col("season") == SEASON)
                                & pl.col("home_score").is_not_null())["week"]
                   .unique().to_list())

    def evaluate(label: str, events: pl.DataFrame, slot_aware: bool) -> pl.DataFrame:
        rows = []
        for wk in weeks:
            prior = events.filter((pl.col("season") < SEASON)
                                  | ((pl.col("season") == SEASON) & (pl.col("week") < wk)))
            if prior.height == 0:
                continue
            gl = defense._game_level(prior)
            cur = gl.filter(pl.col("season") == SEASON)
            pri = gl.filter(pl.col("season") < SEASON)
            gp = int(cur["week"].n_unique()) if cur.height else 0
            m = defense.build_matrix(cur) if cur.height else pl.DataFrame()
            pm = defense.build_matrix(pri) if pri.height else pl.DataFrame()
            mtx = defense.rank_and_z(defense.blend_with_prior(m, pm, gp))
            if mtx.height == 0:
                continue
            prof = players.build_profiles(
                prior.with_columns(
                    pl.when(pl.col("rec_pos") == "SWR").then(pl.lit("WR"))
                    .otherwise(pl.col("rec_pos")).alias("rec_pos"))
                if slot_aware else prior,
                ros, snaps, ffopp, SEASON)
            if prof.height == 0:
                continue
            wg = sched.filter((pl.col("season") == SEASON)
                              & (pl.col("week") == wk)).to_dicts()
            sc = matchup.compute(prof, mtx, wg, {}, {}, defense.METRIC_LABELS, fmt=FMT)
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
                rows.append({"week": wk, "pid": s["pid"], "pos": s["pos"],
                             "light": s["light"], "score": s["matchup_score"],
                             "residual": real[s["pid"]] - base.get(s["pid"], 0.0)})
        return pl.DataFrame(rows)

    def report(label: str, F: pl.DataFrame, extra_mask=None) -> None:
        if extra_mask is not None:
            F = F.filter(extra_mask)
        g = F.filter(pl.col("light") == "verde")["residual"].to_numpy()
        r = F.filter(pl.col("light") == "rojo")["residual"].to_numpy()
        gap = g.mean() - r.mean()
        se = np.sqrt(g.var(ddof=1)/len(g) + r.var(ddof=1)/len(r))
        wr = F.filter(pl.col("pos") == "WR")
        gw = wr.filter(pl.col("light") == "verde")["residual"].to_numpy()
        rw = wr.filter(pl.col("light") == "rojo")["residual"].to_numpy()
        wgap = (gw.mean() - rw.mean()) if len(gw) > 5 and len(rw) > 5 else float("nan")
        print(f"  {label:<44} global {gap:+.3f} (t={gap/se:4.2f})   WR {wgap:+.3f}")

    print("\nRESULTADOS")
    F_base = evaluate("base", ev, False)
    report("Modelo actual (todos los WR iguales)", F_base)

    F_slot = evaluate("slot", ev_split, True)
    report("1) Defensa separada en slot vs abierto", F_slot)

    # 2) separación: ¿el efecto es mayor en receptores que separan poco?
    if sep:
        F2 = F_base.with_columns(
            pl.col("pid").replace_strict(sep, default=None).alias("sep"))
        lo = F2.filter(pl.col("sep").is_not_null() & (pl.col("sep") <= 2.7))
        hi = F2.filter(pl.col("sep").is_not_null() & (pl.col("sep") > 2.7))
        if lo.height > 100 and hi.height > 100:
            report("2a) Solo receptores con POCA separación", lo)
            report("2b) Solo receptores con MUCHA separación", hi)

    # 3) práctica: ¿los 'limited' rinden por debajo?
    if prac:
        F3 = F_base.with_columns(
            pl.struct(["week", "pid"]).map_elements(
                lambda s: prac.get((s["week"], s["pid"]), "Sin reporte"),
                return_dtype=pl.Utf8).alias("prac"))
        print("\n  Participación en prácticas (rendimiento vs su propio promedio):")
        for st in ("Sin reporte", "Full Participation in Practice",
                   "Limited Participation in Practice",
                   "Did Not Participate In Practice"):
            d = F3.filter(pl.col("prac") == st)
            if d.height >= 30:
                print(f"    {st:<38} n={d.height:>5}  {d['residual'].mean():+.2f}")

    print(f"\nTiempo: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
