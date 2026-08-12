"""SIMULACIÓN FINAL — modelo nuevo contra la temporada 2025, semanas 1 a 18.

Usa exactamente el mismo código que corre en producción cada martes:
anclaje al mercado, reporte de lesiones separado del semáforo, y las reglas de
discrepancia. Validación walk-forward: para la semana W sólo datos anteriores.

Reporta todo: fantasy por semáforo, por posición, por riesgo de lesión, por
decil de score; y partidos con acierto, Brier, ATS y postura frente a Vegas.
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np
import polars as pl
import nflreadpy as nfl
from scipy import stats

sys.path.insert(0, "/home/claude/nfl-edge")
from pipeline import config as C                       # noqa: E402
from pipeline import defense, games, matchup, players  # noqa: E402

SEASON, FMT = 2025, "half_ppr"


def matrix_upto(ev: pl.DataFrame, week: int):
    prior = ev.filter((pl.col("season") < SEASON)
                      | ((pl.col("season") == SEASON) & (pl.col("week") < week)))
    if prior.height == 0:
        return pl.DataFrame(), prior, pl.DataFrame()
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
    return defense.rank_and_z(defense.blend_with_prior(current, pm, gp)), prior, gl


def main() -> None:
    t0 = time.time()
    print("Cargando datos…")
    pbp = nfl.load_pbp([2024, SEASON])
    ros = nfl.load_rosters_weekly([2024, SEASON])
    snaps = nfl.load_snap_counts([2024, SEASON])
    ffopp = nfl.load_ff_opportunity([2024, SEASON])
    sched = nfl.load_schedules(list(range(2010, SEASON + 1)))
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
    print(f"  {ev.height} jugadas etiquetadas ({time.time()-t0:.0f}s)\n")

    weeks = sorted(sched.filter((pl.col("season") == SEASON)
                                & (pl.col("game_type") == "REG")
                                & pl.col("home_score").is_not_null())["week"]
                   .unique().to_list())
    F, G = [], []

    for wk in weeks:
        tw = time.time()
        mtx, prior, _ = matrix_upto(ev, wk)
        if mtx.height == 0:
            continue
        gl_prior = defense._game_level(prior) if prior.height else pl.DataFrame()

        # ---- partidos (con el anclaje al mercado que corre en producción)
        preds, _meta = games.predict_week(sched, gl_prior, SEASON, wk)
        actual = {r["game_id"]: (r["home_score"], r["away_score"])
                  for r in sched.filter((pl.col("season") == SEASON)
                                        & (pl.col("week") == wk)).iter_rows(named=True)
                  if r["home_score"] is not None}
        for p in preds:
            a = actual.get(p["game_id"])
            if not a or a[0] == a[1]:
                continue
            m = a[0] - a[1]
            hw = m > 0
            G.append({
                "week": wk, "p_home": p["p_home"], "p_mkt": p["p_market_home"],
                "margin": p["model_margin"], "raw": p["raw_margin"],
                "spread": p["vegas_spread"], "stance": p["stance"],
                "ats_pick": p["ats_pick"], "consensus": p["consensus"],
                "home_won": hw, "real_margin": m,
                "ok": (p["p_home"] > 0.5) == hw,
                "mkt_ok": (None if p["p_market_home"] is None
                           else (p["p_market_home"] > 0.5) == hw),
                "ats_ok": (None if p["vegas_spread"] is None else
                           (p["raw_margin"] > p["vegas_spread"]) == (m > p["vegas_spread"])),
            })

        # ---- fantasy
        prof = players.build_profiles(prior, ros, snaps, ffopp, SEASON, ngs=ngs)
        if prof.height == 0:
            continue
        wg = sched.filter((pl.col("season") == SEASON) & (pl.col("week") == wk)).to_dicts()
        sc = matchup.compute(prof, mtx, wg, inj_by_week.get(wk, {}), {},
                             defense.METRIC_LABELS, fmt=FMT)
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
            F.append({"week": wk, "pos": s["pos"], "light": s["light"],
                      "score": s["matchup_score"], "start": s["start_score"],
                      "risk": s["risk_level"], "fp": real[s["pid"]],
                      "base": base.get(s["pid"], 0.0),
                      "res": real[s["pid"]] - base.get(s["pid"], 0.0)})
        print(f"  semana {wk:>2}: {len(preds):>2} partidos · {sc.height:>3} jugadores"
              f"  ({time.time()-tw:.0f}s)")

    Fd, Gd = pl.DataFrame(F), pl.DataFrame(G)
    out: dict = {}
    B = "=" * 72

    # =================================================== FANTASY
    print(f"\n{B}\nFANTASY 2025 — semanas {weeks[0]} a {weeks[-1]}\n{B}")
    print(f"Decisiones evaluadas: {Fd.height}\n")
    print("SEMÁFORO (puntos reales vs el propio promedio del jugador)")
    print(f"  {'luz':<10}{'n':>6}{'reales':>9}{'promedio':>10}{'DIFERENCIA':>12}")
    lights = {}
    for lg in ("verde", "amarillo", "rojo"):
        d = Fd.filter(pl.col("light") == lg)
        print(f"  {lg:<10}{d.height:>6}{d['fp'].mean():>9.2f}{d['base'].mean():>10.2f}"
              f"{d['res'].mean():>+12.2f}")
        lights[lg] = {"n": d.height, "res": round(float(d["res"].mean()), 3)}
    g = Fd.filter(pl.col("light") == "verde")["res"].to_numpy()
    r = Fd.filter(pl.col("light") == "rojo")["res"].to_numpy()
    t, pv = stats.ttest_ind(g, r, equal_var=False)
    gap = float(g.mean() - r.mean())
    rho = float(np.corrcoef(Fd["score"].to_numpy().argsort().argsort(),
                            Fd["res"].to_numpy().argsort().argsort())[0, 1])
    print(f"\n  BRECHA verde-rojo: {gap:+.3f} pts   t={t:.2f}   p={pv:.6f}   rho={rho:+.3f}")
    out["fantasy"] = {"n": Fd.height, "lights": lights, "gap": round(gap, 3),
                      "t": round(float(t), 2), "p": float(pv), "rho": round(rho, 3)}

    print("\nPOR POSICIÓN")
    print(f"  {'pos':<6}{'n verde':>9}{'verde':>9}{'n rojo':>9}{'rojo':>9}{'BRECHA':>10}")
    bypos = {}
    for pos in ("QB", "RB", "WR", "TE"):
        d = Fd.filter(pl.col("pos") == pos)
        gv = d.filter(pl.col("light") == "verde")["res"]
        rv = d.filter(pl.col("light") == "rojo")["res"]
        if len(gv) < 20 or len(rv) < 20:
            continue
        bp = float(gv.mean()) - float(rv.mean())
        print(f"  {pos:<6}{len(gv):>9}{gv.mean():>+9.2f}{len(rv):>9}{rv.mean():>+9.2f}{bp:>+10.2f}")
        bypos[pos] = round(bp, 2)
    out["fantasy"]["by_pos"] = bypos

    print("\nPOR NIVEL DE RIESGO DE LESIÓN (validación de la mejora nueva)")
    print(f"  {'estado':<12}{'n':>6}{'vs su promedio':>17}")
    risk = {}
    for rk in ("ninguno", "bajo", "medio", "alto", "fuera"):
        d = Fd.filter(pl.col("risk") == rk)
        if d.height < 15:
            continue
        print(f"  {rk:<12}{d.height:>6}{d['res'].mean():>+17.2f}")
        risk[rk] = {"n": d.height, "res": round(float(d["res"].mean()), 2)}
    sano = Fd.filter(pl.col("risk") == "ninguno")["res"].to_numpy()
    herido = Fd.filter(pl.col("risk") != "ninguno")["res"].to_numpy()
    if len(herido) > 30:
        t2, p2 = stats.ttest_ind(sano, herido, equal_var=False)
        print(f"\n  Sano vs en reporte: {sano.mean()-herido.mean():+.3f} pts   "
              f"t={t2:.2f}   p={p2:.6f}")
        out["risk"] = {"levels": risk, "gap": round(float(sano.mean()-herido.mean()), 3),
                       "t": round(float(t2), 2), "p": float(p2)}

    print("\nPOR DECIL DEL SCORE DE MATCHUP (0-100)")
    print(f"  {'rango':<12}{'n':>6}{'vs su promedio':>17}")
    dec = []
    for lo in range(0, 100, 10):
        d = Fd.filter((pl.col("score") >= lo) & (pl.col("score") < lo + 10))
        if d.height < 20:
            continue
        bar = "█" * max(0, int(round((d["res"].mean() + 1.2) * 6)))
        print(f"  {lo:>3}-{lo+9:<8}{d.height:>6}{d['res'].mean():>+17.2f}  {bar}")
        dec.append({"lo": lo, "n": d.height, "res": round(float(d["res"].mean()), 2)})
    out["fantasy"]["deciles"] = dec

    # =================================================== PARTIDOS
    print(f"\n{B}\nPARTIDOS 2025 — semanas {weeks[0]} a {weeks[-1]}\n{B}")
    n = Gd.height
    acc = 100 * Gd["ok"].sum() / n
    gm = Gd.filter(pl.col("mkt_ok").is_not_null())
    vacc = 100 * gm["mkt_ok"].sum() / gm.height
    brier = float(((Gd["p_home"] - Gd["home_won"].cast(pl.Float64)) ** 2).mean())
    bmkt = float(gm.select(((pl.col("p_mkt") - pl.col("home_won").cast(pl.Float64)) ** 2)
                           .alias("b")).to_series().mean())
    ga = Gd.filter(pl.col("ats_ok").is_not_null())
    ats = 100 * ga["ats_ok"].sum() / ga.height
    print(f"  Partidos evaluados        {n}")
    print(f"  Acierto del MODELO        {acc:.1f}%")
    print(f"  Acierto de VEGAS          {vacc:.1f}%")
    print(f"  Brier modelo / Vegas      {brier:.4f} / {bmkt:.4f}")
    print(f"  Contra el spread (todos)  {ats:.1f}%")
    out["games"] = {"n": int(n), "acc": round(acc, 1), "vegas": round(vacc, 1),
                    "brier": round(brier, 4), "brier_vegas": round(bmkt, 4),
                    "ats": round(ats, 1)}

    print("\n  POSTURA FRENTE AL MERCADO (la regla nueva)")
    print(f"  {'postura':<18}{'n':>5}{'%':>7}{'acierto':>10}{'Vegas':>8}")
    st = {}
    for s in ("coincide", "discrepa_debil", "discrepa", "discrepa_fuerte"):
        d = Gd.filter(pl.col("stance") == s)
        if d.height == 0:
            continue
        dv = d.filter(pl.col("mkt_ok").is_not_null())
        va = 100 * dv["mkt_ok"].sum() / dv.height if dv.height else float("nan")
        a2 = 100 * d["ok"].sum() / d.height
        print(f"  {s:<18}{d.height:>5}{100*d.height/n:>6.0f}%{a2:>9.1f}%{va:>7.1f}%")
        st[s] = {"n": d.height, "acc": round(a2, 1), "vegas": round(va, 1)}
    out["games"]["stance"] = st

    picks = Gd.filter(pl.col("ats_pick").is_not_null())
    if picks.height:
        pa = 100 * picks["ats_ok"].sum() / picks.height
        print(f"\n  Lados de spread señalados: {picks.height} de {n} "
              f"({100*picks.height/n:.0f}%) · acierto {pa:.1f}%")
        out["games"]["ats_picks"] = {"n": picks.height, "acc": round(pa, 1)}

    print("\n  SEMANA POR SEMANA")
    print(f"  {'sem':>4}{'n':>4}{'modelo':>9}{'Vegas':>8}   fantasy verde-rojo")
    wk_rows = []
    for wk in weeks:
        w = Gd.filter(pl.col("week") == wk)
        wm = w.filter(pl.col("mkt_ok").is_not_null())
        if w.height == 0:
            continue
        a2 = 100 * w["ok"].sum() / w.height
        v2 = 100 * wm["mkt_ok"].sum() / wm.height if wm.height else float("nan")
        fw = Fd.filter(pl.col("week") == wk)
        gv = fw.filter(pl.col("light") == "verde")["res"]
        rv = fw.filter(pl.col("light") == "rojo")["res"]
        fg = (float(gv.mean()) - float(rv.mean())) if len(gv) > 4 and len(rv) > 4 else float("nan")
        mark = " <<" if a2 > v2 else ("  =" if a2 == v2 else "")
        print(f"  {wk:>4}{w.height:>4}{a2:>8.1f}%{v2:>7.1f}%{mark}      {fg:+.2f}")
        wk_rows.append({"week": int(wk), "n": int(w.height), "model": round(a2, 1),
                        "vegas": round(v2, 1),
                        "fantasy_gap": (None if fg != fg else round(fg, 2))})
    out["by_week"] = wk_rows

    with open("/home/claude/nfl-edge/tests/simulacion_final_2025.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\nTiempo total: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
