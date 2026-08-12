"""Backtest completo de la temporada 2025, semana por semana.

Regla de oro: para predecir la semana W sólo se usan datos ANTERIORES a W.
Nada de mirar el futuro. Cada semana se reconstruye la matriz defensiva, los
perfiles de jugador y los ratings de equipo desde cero con la información que
realmente existía ese martes.

Mide dos cosas distintas:
  A) Partidos: ¿acertamos el ganador? ¿mejor o peor que el mercado de Vegas?
  B) Fantasy: ¿un matchup verde realmente hace que el jugador supere su propio
     promedio? (controlado por calidad: comparamos contra su línea base, no
     contra otros jugadores)
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np
import polars as pl
import nflreadpy as nfl

sys.path.insert(0, "/home/claude/nfl-edge")
from pipeline import config as C          # noqa: E402
from pipeline import defense, games, matchup, players  # noqa: E402

SEASON = 2025
FMT = "half_ppr"


def defense_matrix_upto(ev: pl.DataFrame, season: int, week: int):
    """Réplica de defense.build() pero limitada a lo ocurrido antes de `week`."""
    prior_ev = ev.filter((pl.col("season") < season)
                         | ((pl.col("season") == season) & (pl.col("week") < week)))
    if prior_ev.height == 0:
        return pl.DataFrame(), 0
    gl = defense._game_level(prior_ev)
    cur = gl.filter(pl.col("season") == season)
    pri = gl.filter(pl.col("season") < season)
    games_played = int(cur["week"].n_unique()) if cur.height else 0

    parts = []
    for name, wb in C.WINDOWS.items():
        m = defense.build_matrix(cur, wb) if cur.height else pl.DataFrame()
        if m.height:
            parts.append((C.WINDOW_BLEND[name], m))
    if parts:
        total_w = sum(w for w, _ in parts)
        acc = parts[0][1].select("team")
        cols = [c for c in parts[0][1].columns if c != "team"]
        exprs = []
        for c in cols:
            terms = []
            for i, (w, m) in enumerate(parts):
                if c in m.columns:
                    acc = acc.join(m.select(["team", pl.col(c).alias(f"{c}__{i}")]),
                                   on="team", how="left")
                    terms.append(pl.col(f"{c}__{i}").fill_null(0.0) * (w / total_w))
            if terms:
                exprs.append(sum(terms[1:], terms[0]).alias(c))
        current = acc.with_columns(exprs).select(["team"] + cols)
    else:
        current = pl.DataFrame()

    prior = defense.build_matrix(pri) if pri.height else pl.DataFrame()
    blended = defense.blend_with_prior(current, prior, games_played)
    return defense.rank_and_z(blended), games_played, gl


def main() -> None:
    t0 = time.time()
    print("Cargando datos…")
    pbp = nfl.load_pbp([2023, 2024, SEASON])
    ros = nfl.load_rosters_weekly([2023, 2024, SEASON])
    snaps = nfl.load_snap_counts([2024, SEASON])
    ffopp = nfl.load_ff_opportunity([2024, SEASON])
    sched = nfl.load_schedules(list(range(2010, SEASON + 1)))
    ev = defense.build_play_events(pbp, ros)
    pg = players.player_game_usage(ev)
    print(f"  pbp {pbp.shape} · jugadas etiquetadas {ev.height}  ({time.time()-t0:.0f}s)")

    weeks = sorted(sched.filter((pl.col("season") == SEASON)
                                & (pl.col("game_type") == "REG")
                                & pl.col("home_score").is_not_null())["week"].unique().to_list())

    game_rows: list[dict] = []
    fant_rows: list[dict] = []

    for wk in weeks:
        tw = time.time()
        # ---------------------------------------------------- A) partidos
        preds, meta = games.predict_week(sched, pl.DataFrame(), SEASON, wk)
        # EPA ajustada sólo con lo anterior a la semana
        prior_ev = ev.filter((pl.col("season") < SEASON)
                             | ((pl.col("season") == SEASON) & (pl.col("week") < wk)))
        gl_prior = defense._game_level(prior_ev) if prior_ev.height else pl.DataFrame()
        preds, meta = games.predict_week(sched, gl_prior, SEASON, wk)

        actual = {r["game_id"]: (r["home_score"], r["away_score"])
                  for r in sched.filter((pl.col("season") == SEASON) & (pl.col("week") == wk))
                  .iter_rows(named=True) if r["home_score"] is not None}
        for p in preds:
            a = actual.get(p["game_id"])
            if not a or a[0] == a[1]:
                continue
            margin = a[0] - a[1]
            home_won = margin > 0
            game_rows.append({
                "week": wk, "game_id": p["game_id"],
                "p_home": p["p_home"], "p_mkt": p["p_market_home"],
                "model_margin": p["model_margin"], "vegas_spread": p["vegas_spread"],
                "home_won": home_won, "margin": margin,
                "model_ok": (p["p_home"] > 0.5) == home_won,
                "mkt_ok": (None if p["p_market_home"] is None
                           else (p["p_market_home"] > 0.5) == home_won),
                "model_ats": (None if p["vegas_spread"] is None
                              else (p["model_margin"] > p["vegas_spread"]) ==
                                   (margin > p["vegas_spread"])),
            })

        # ---------------------------------------------------- B) fantasy
        matrix, gp, _gl = defense_matrix_upto(ev, SEASON, wk)
        if matrix.height == 0:
            print(f"  semana {wk}: sin matriz defensiva, se omite fantasy")
            continue
        prof = players.build_profiles(prior_ev, ros, snaps, ffopp, SEASON)
        if prof.height == 0:
            continue
        week_games = sched.filter((pl.col("season") == SEASON)
                                  & (pl.col("week") == wk)).to_dicts()
        scored = matchup.compute(prof, matrix, week_games, {}, {},
                                 defense.METRIC_LABELS, fmt=FMT)
        if scored.height == 0:
            continue

        # puntos REALES de esa semana
        wkpg = pg.filter((pl.col("season") == SEASON) & (pl.col("week") == wk))
        real = {r["pid"]: players.fantasy_points(r, FMT) for r in wkpg.to_dicts()}
        # línea base del jugador ANTES de la semana (su propio promedio)
        base = {r["pid"]: (r.get(f"fppg_{FMT}") or 0.0) for r in prof.to_dicts()}
        gcount = {r["pid"]: (r.get("games") or 0) for r in prof.to_dicts()}

        for s in scored.to_dicts():
            pid = s["pid"]
            if pid not in real:          # no jugó esa semana
                continue
            if gcount.get(pid, 0) < 3:   # sin línea base confiable
                continue
            fp = real[pid]
            bl = base.get(pid, 0.0)
            fant_rows.append({
                "week": wk, "pid": pid, "name": s["name"], "pos": s["pos"],
                "light": s["light"], "matchup_score": s["matchup_score"],
                "fp": fp, "baseline": bl, "residual": fp - bl,
            })
        print(f"  semana {wk:>2}: {len(preds)} partidos · "
              f"{scored.height} jugadores  ({time.time()-tw:.0f}s)")

    # ============================================================ resultados
    G = pl.DataFrame(game_rows)
    F = pl.DataFrame(fant_rows)
    out: dict = {"season": SEASON}

    print("\n" + "=" * 68)
    print(f"A) PARTIDOS — temporada {SEASON} completa")
    print("=" * 68)
    n = G.height
    macc = 100 * G["model_ok"].sum() / n
    gm = G.filter(pl.col("mkt_ok").is_not_null())
    vacc = 100 * gm["mkt_ok"].sum() / gm.height
    brier = float(((G["p_home"] - G["home_won"].cast(pl.Float64)) ** 2).mean())
    brier_mkt = float(gm.select(
        ((pl.col("p_mkt") - pl.col("home_won").cast(pl.Float64)) ** 2)
        .alias("b")).to_series().mean())
    ga = G.filter(pl.col("model_ats").is_not_null())
    ats = 100 * ga["model_ats"].sum() / ga.height

    print(f"  Partidos evaluados      {n}")
    print(f"  Acierto del MODELO      {macc:.1f}%")
    print(f"  Acierto de VEGAS        {vacc:.1f}%")
    print(f"  Diferencia              {macc - vacc:+.1f} puntos porcentuales")
    print(f"  Brier modelo / Vegas    {brier:.4f} / {brier_mkt:.4f}  (más bajo = mejor)")
    print(f"  Contra el spread (ATS)  {ats:.1f}%   (52.4% es el punto de equilibrio)")

    # ¿qué pasa cuando el modelo y Vegas NO coinciden?
    dis = gm.filter((pl.col("p_home") > 0.5) != (pl.col("p_mkt") > 0.5))
    if dis.height:
        dm = 100 * dis["model_ok"].sum() / dis.height
        print(f"\n  Cuando el modelo DISCREPA de Vegas ({dis.height} partidos, "
              f"{100*dis.height/gm.height:.0f}% del total):")
        print(f"    gana el modelo {dm:.1f}% de las veces · gana Vegas {100-dm:.1f}%")
        out["disagreement"] = {"n": int(dis.height), "model_wins_pct": round(dm, 1)}
    agree = gm.filter((pl.col("p_home") > 0.5) == (pl.col("p_mkt") > 0.5))
    if agree.height:
        print(f"  Cuando COINCIDEN ({agree.height} partidos): "
              f"aciertan {100*agree['model_ok'].sum()/agree.height:.1f}%")

    print("\n  Semana por semana:")
    print("  sem  n   modelo   vegas")
    wk_rows = []
    for wk in sorted(G["week"].unique().to_list()):
        w = G.filter(pl.col("week") == wk)
        wm = w.filter(pl.col("mkt_ok").is_not_null())
        a = 100 * w["model_ok"].sum() / w.height
        v = 100 * wm["mkt_ok"].sum() / wm.height if wm.height else float("nan")
        flag = "  <<" if a > v else ("  --" if a == v else "")
        print(f"  {wk:>3} {w.height:>3}   {a:5.1f}%  {v:5.1f}%{flag}")
        wk_rows.append({"week": int(wk), "n": int(w.height),
                        "model": round(a, 1), "vegas": round(v, 1)})
    out["games"] = {"n": int(n), "model_acc": round(macc, 1), "vegas_acc": round(vacc, 1),
                    "brier": round(brier, 4), "brier_vegas": round(brier_mkt, 4),
                    "ats": round(ats, 1), "by_week": wk_rows}

    print("\n" + "=" * 68)
    print("B) FANTASY — ¿el semáforo realmente sirve?")
    print("=" * 68)
    print("  Se compara a cada jugador CONTRA SÍ MISMO: sus puntos reales de esa")
    print("  semana menos su propio promedio previo. Si el semáforo funciona, los")
    print("  verdes deben superar su promedio y los rojos quedarse cortos.\n")
    print("  luz         n     puntos reales   su promedio   diferencia")
    lights = []
    for lg in ("verde", "amarillo", "rojo"):
        d = F.filter(pl.col("light") == lg)
        if d.height == 0:
            continue
        print(f"  {lg:<10}{d.height:>5}     {d['fp'].mean():>8.2f}      "
              f"{d['baseline'].mean():>8.2f}     {d['residual'].mean():>+8.2f}")
        lights.append({"light": lg, "n": int(d.height),
                       "fp": round(float(d["fp"].mean()), 2),
                       "baseline": round(float(d["baseline"].mean()), 2),
                       "residual": round(float(d["residual"].mean()), 2)})
    spread_vr = (F.filter(pl.col("light") == "verde")["residual"].mean()
                 - F.filter(pl.col("light") == "rojo")["residual"].mean())
    print(f"\n  Brecha verde - rojo: {spread_vr:+.2f} puntos fantasy por partido")

    # significancia: ¿podría ser suerte?
    gv = F.filter(pl.col("light") == "verde")["residual"].to_numpy()
    rv = F.filter(pl.col("light") == "rojo")["residual"].to_numpy()
    se = float(np.sqrt(gv.var(ddof=1)/len(gv) + rv.var(ddof=1)/len(rv)))
    tstat = float((gv.mean() - rv.mean()) / se) if se > 0 else 0.0
    print(f"  Estadístico t = {tstat:.2f}  "
          f"({'significativo, no es azar' if abs(tstat) > 2 else 'NO significativo: cabe el azar'})")

    print("\n  Por posición (diferencia verde - rojo):")
    pos_rows = []
    for pos in ("QB", "RB", "WR", "TE"):
        d = F.filter(pl.col("pos") == pos)
        v = d.filter(pl.col("light") == "verde")["residual"]
        r = d.filter(pl.col("light") == "rojo")["residual"]
        if len(v) < 25 or len(r) < 25:
            continue
        print(f"    {pos}: {float(v.mean()) - float(r.mean()):+.2f} pts   "
              f"(n verde={len(v)}, n rojo={len(r)})")
        pos_rows.append({"pos": pos, "gap": round(float(v.mean()) - float(r.mean()), 2),
                         "n_green": len(v), "n_red": len(r)})

    # correlación score -> rendimiento sobre su base
    sc = F["matchup_score"].to_numpy()
    rs = F["residual"].to_numpy()
    rank_c = float(np.corrcoef(sc.argsort().argsort(), rs.argsort().argsort())[0, 1])
    print(f"\n  Correlación (Spearman) score 0-100 vs rendimiento sobre su base: {rank_c:+.3f}")

    out["fantasy"] = {"n": int(F.height), "by_light": lights,
                      "green_minus_red": round(float(spread_vr), 2),
                      "t_stat": round(tstat, 2), "by_pos": pos_rows,
                      "spearman": round(rank_c, 3)}

    with open("/home/claude/nfl-edge/tests/backtest_2025_resultados.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nTiempo total: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
