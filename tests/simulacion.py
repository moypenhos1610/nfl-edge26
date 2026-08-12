"""SIMULACIÓN: ¿qué combinación de información hace al modelo más preciso?

Prueba sistemáticamente combinaciones de variables de entrada, tanto para
partidos como para fantasy, sobre la temporada 2025 completa (semanas 1-18 más
playoffs), con validación walk-forward estricta: para predecir la semana W solo
se usa información anterior a W.

La pregunta clave del módulo de partidos no es "¿acertamos más?", sino:
    ¿nuestro modelo contiene ALGO de información que el mercado no tenga?
Eso se responde con un apilamiento (stacking): se mete el logit del mercado y
el logit del modelo en la misma regresión y se mira si el coeficiente del
modelo sobrevive. Si sobrevive, tenemos señal propia. Si no, no la tenemos.
"""
from __future__ import annotations

import json
import math
import sys
import time

import numpy as np
import polars as pl
import nflreadpy as nfl

sys.path.insert(0, "/home/claude/nfl-edge")
from pipeline import config as C            # noqa: E402
from pipeline import defense, games, matchup, players  # noqa: E402

TEST_SEASON = 2025
TRAIN_FROM = 2016
FMT = "half_ppr"
R_EARTH_MI = 3958.8


# ------------------------------------------------------------------ utilidades
def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_EARTH_MI * math.asin(math.sqrt(h))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                 iters: int = 60) -> np.ndarray:
    """Regresión logística multivariable (Newton-Raphson con regularización)."""
    n, p = X.shape
    Xa = np.column_stack([np.ones(n), X])
    b = np.zeros(p + 1)
    reg = np.eye(p + 1) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        z = np.clip(Xa @ b, -30, 30)
        mu = 1 / (1 + np.exp(-z))
        W = np.clip(mu * (1 - mu), 1e-6, None)
        g = Xa.T @ (y - mu) - reg @ b
        H = Xa.T @ (Xa * W[:, None]) + reg
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        b += step
        if np.abs(step).max() < 1e-9:
            break
    return b


def predict_logistic(b: np.ndarray, X: np.ndarray) -> np.ndarray:
    z = np.clip(np.column_stack([np.ones(len(X)), X]) @ b, -30, 30)
    return 1 / (1 + np.exp(-z))


def metrics(p: np.ndarray, y: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {
        "acc": round(100 * float(((p > 0.5) == (y > 0.5)).mean()), 2),
        "brier": round(float(((p - y) ** 2).mean()), 4),
        "logloss": round(float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), 4),
        "n": int(len(y)),
    }


# ============================================================ A) PARTIDOS
def build_game_features(sched: pl.DataFrame, ev: pl.DataFrame) -> pl.DataFrame:
    """Tabla de una fila por partido con TODAS las variables candidatas,
    cada una calculada solo con información previa al partido."""
    print("\n[A] Construyendo variables de partidos…")

    # --- Elo previo a cada partido (ya evita fuga por construcción)
    _, hist = games.compute_elo(sched)
    elo = {(r["game_id"]): r["elo_diff"] for r in hist.iter_rows(named=True)}

    # --- EPA ajustada por rival, acumulada semana a semana
    gl = defense._game_level(ev)
    epa_by_week: dict[tuple[int, int], dict] = {}
    seasons = sorted(gl["season"].unique().to_list())
    for s in seasons:
        wks = sorted(gl.filter(pl.col("season") == s)["week"].unique().to_list())
        for w in wks:
            prior = gl.filter((pl.col("season") < s)
                              | ((pl.col("season") == s) & (pl.col("week") < w)))
            if prior.height < 60:
                continue
            # solo temporada actual + anterior, para captar forma
            prior = prior.filter(pl.col("season") >= s - 1)
            epa_by_week[(s, w)] = games.epa_ratings(prior, s)

    # --- QB titular por equipo y semana (quién lanzó más pases)
    qb = (ev.filter(pl.col("play_type") == "pass")
          .drop_nulls("passer")
          .group_by(["season", "week", "off", "passer"])
          .agg(pl.len().alias("att"))
          .sort("att", descending=True)
          .group_by(["season", "week", "off"]).agg(pl.col("passer").first()))
    qbmap = {(r["season"], r["week"], r["off"]): r["passer"]
             for r in qb.iter_rows(named=True)}

    def qb_changed(season: int, week: int, team: str) -> int:
        """1 si el QB de esta semana no es el que venía jugando."""
        cur = qbmap.get((season, week, team))
        if cur is None:
            return 0
        prev = [qbmap.get((season, w, team)) for w in range(max(1, week - 4), week)]
        prev = [p for p in prev if p]
        if not prev:
            return 0
        from collections import Counter
        common = Counter(prev).most_common(1)[0][0]
        return int(cur != common)

    # --- récords previos
    rows: list[dict] = []
    s_all = sched.filter(pl.col("home_score").is_not_null()).sort(["season", "week"])
    wins: dict[tuple[int, str], list[int]] = {}

    for g in s_all.iter_rows(named=True):
        s, w = g["season"], g["week"]
        h, a = g["home_team"], g["away_team"]
        if s < TRAIN_FROM:
            for t, own, opp in ((h, g["home_score"], g["away_score"]),
                                (a, g["away_score"], g["home_score"])):
                wins.setdefault((s, t), []).append(1 if own > opp else 0)
            continue

        hw = wins.get((s, h), [])
        aw = wins.get((s, a), [])
        e = epa_by_week.get((s, w), {})
        eh = e.get(h, {"off": 0.0, "def": 0.0})
        ea = e.get(a, {"off": 0.0, "def": 0.0})

        loc_h = C.STADIUMS.get(h)
        loc_a = C.STADIUMS.get(a)
        travel = (haversine(loc_a[:2], loc_h[:2]) if (loc_h and loc_a) else 0.0)
        tz = abs((loc_h[1] - loc_a[1]) / 15.0) if (loc_h and loc_a) else 0.0

        mkt = games.devig_moneyline(g.get("home_moneyline"), g.get("away_moneyline"))
        margin = g["home_score"] - g["away_score"]

        rows.append({
            "season": s, "week": w, "game_id": g["game_id"],
            "game_type": g.get("game_type"),
            "home_team": h, "away_team": a,
            "elo_diff": elo.get(g["game_id"], 0.0),
            "epa_margin": ((eh["off"] - ea["off"]) - (eh["def"] - ea["def"])),
            "epa_off_diff": eh["off"] - ea["off"],
            "epa_def_diff": -(eh["def"] - ea["def"]),
            "rest_diff": float((g.get("home_rest") or 7) - (g.get("away_rest") or 7)),
            "travel_1000mi": travel / 1000.0,
            "tz_shift": tz,
            "wpct_diff": ((np.mean(hw) if hw else 0.5) - (np.mean(aw) if aw else 0.5)),
            "games_played": min(len(hw), len(aw)),
            "qb_change_home": qb_changed(s, w, h),
            "qb_change_away": qb_changed(s, w, a),
            "div_game": int(g.get("div_game") or 0),
            "roof_dome": int(str(g.get("roof") or "").lower() in ("dome", "closed")),
            "market_p": mkt, "market_logit": (logit(mkt) if mkt is not None else None),
            "vegas_spread": g.get("spread_line"),
            "vegas_total": g.get("total_line"),
            "margin": margin, "y": 1.0 if margin > 0 else 0.0,
            "is_tie": int(margin == 0),
        })
        for t, own, opp in ((h, g["home_score"], g["away_score"]),
                            (a, g["away_score"], g["home_score"])):
            wins.setdefault((s, t), []).append(1 if own > opp else 0)

    df = pl.DataFrame(rows).filter(pl.col("is_tie") == 0)
    print(f"    {df.height} partidos con variables ({TRAIN_FROM}-{TEST_SEASON})")
    return df


# combinaciones de variables a probar
VARIANTS: dict[str, list[str]] = {
    "1. Solo Elo":                     ["elo_diff"],
    "2. Elo + EPA ajustada":           ["elo_diff", "epa_margin"],
    "3. Elo + EPA ofensiva/defensiva": ["elo_diff", "epa_off_diff", "epa_def_diff"],
    "4. + descanso y viaje":           ["elo_diff", "epa_off_diff", "epa_def_diff",
                                        "rest_diff", "travel_1000mi", "tz_shift"],
    "5. + récord y divisional":        ["elo_diff", "epa_off_diff", "epa_def_diff",
                                        "rest_diff", "travel_1000mi", "tz_shift",
                                        "wpct_diff", "div_game"],
    "6. + CAMBIO DE QB":               ["elo_diff", "epa_off_diff", "epa_def_diff",
                                        "rest_diff", "travel_1000mi", "tz_shift",
                                        "wpct_diff", "div_game",
                                        "qb_change_home", "qb_change_away"],
    "7. TODO menos el mercado":        ["elo_diff", "epa_off_diff", "epa_def_diff",
                                        "rest_diff", "travel_1000mi", "tz_shift",
                                        "wpct_diff", "div_game",
                                        "qb_change_home", "qb_change_away",
                                        "roof_dome"],
    "8. SOLO el mercado (Vegas)":      ["market_logit"],
    "9. Mercado + Elo":                ["market_logit", "elo_diff"],
    "10. Mercado + Elo + EPA":         ["market_logit", "elo_diff", "epa_margin"],
    "11. Mercado + TODO lo nuestro":   ["market_logit", "elo_diff", "epa_off_diff",
                                        "epa_def_diff", "rest_diff", "travel_1000mi",
                                        "tz_shift", "wpct_diff", "div_game",
                                        "qb_change_home", "qb_change_away"],
}


def run_games(df: pl.DataFrame) -> dict:
    print("\n[A] Evaluando combinaciones de variables (walk-forward)…")
    test = df.filter(pl.col("season") == TEST_SEASON)
    weeks = sorted(test["week"].unique().to_list())

    results: dict[str, dict] = {}
    preds_store: dict[str, np.ndarray] = {}
    y_store: list[float] = []
    mkt_store: list[float] = []
    meta_store: list[dict] = []

    for name, feats in VARIANTS.items():
        preds, ys = [], []
        for wk in weeks:
            tr = df.filter((pl.col("season") < TEST_SEASON)
                           | ((pl.col("season") == TEST_SEASON) & (pl.col("week") < wk)))
            te = test.filter(pl.col("week") == wk)
            tr = tr.drop_nulls(feats + ["y"])
            te2 = te.drop_nulls(feats + ["y"])
            if tr.height < 200 or te2.height == 0:
                continue
            Xtr = tr.select(feats).to_numpy().astype(float)
            ytr = tr["y"].to_numpy().astype(float)
            Xte = te2.select(feats).to_numpy().astype(float)
            mu, sd = Xtr.mean(0), Xtr.std(0)
            sd[sd < 1e-9] = 1.0
            b = fit_logistic((Xtr - mu) / sd, ytr)
            preds.append(predict_logistic(b, (Xte - mu) / sd))
            ys.append(te2["y"].to_numpy().astype(float))
            if name == list(VARIANTS)[0]:
                mkt_store.extend(te2["market_p"].to_list())
                meta_store.extend(te2.select(["week", "game_type", "game_id"]).to_dicts())
        if not preds:
            continue
        p = np.concatenate(preds)
        y = np.concatenate(ys)
        preds_store[name] = p
        if not y_store:
            y_store = y.tolist()
        results[name] = metrics(p, y)

    y = np.array(y_store)
    mkt = np.array([m if m is not None else np.nan for m in mkt_store])
    ok = ~np.isnan(mkt)
    results["_market_raw"] = metrics(mkt[ok], y[ok])

    # ---- ¿aportamos algo que Vegas no tenga?  (donde discrepamos)
    disagree: dict[str, dict] = {}
    for name, p in preds_store.items():
        if not name.startswith(("1", "2", "3", "4", "5", "6", "7")):
            continue
        d = ok & ((p > 0.5) != (mkt > 0.5))
        if d.sum() < 5:
            continue
        disagree[name] = {
            "n": int(d.sum()),
            "modelo": round(100 * float(((p[d] > 0.5) == (y[d] > 0.5)).mean()), 1),
            "vegas": round(100 * float(((mkt[d] > 0.5) == (y[d] > 0.5)).mean()), 1),
        }

    # ---- prueba decisiva: coeficiente del modelo junto al del mercado
    best_own = "7. TODO menos el mercado"
    p_own = preds_store.get(best_own)
    stack = None
    if p_own is not None:
        X = np.column_stack([np.array([logit(v) for v in mkt[ok]]),
                             np.array([logit(v) for v in p_own[ok]])])
        mu, sd = X.mean(0), X.std(0)
        sd[sd < 1e-9] = 1.0
        b = fit_logistic((X - mu) / sd, y[ok], l2=0.5)
        stack = {"coef_mercado": round(float(b[1]), 3),
                 "coef_modelo": round(float(b[2]), 3)}
        print(f"    apilamiento -> mercado {b[1]:+.3f} · modelo {b[2]:+.3f}")

    return {"variants": results, "disagree": disagree, "stack": stack,
            "meta": meta_store}


# ============================================================ B) FANTASY
FANT_VARIANTS = {
    "A. Modelo completo":            dict(),
    "B. Sin ajuste por rival":       dict(no_ridge=True),
    "C. Sin ventanas de recencia":   dict(season_only=True),
    "D. Sin entorno de Vegas":       dict(w_env=0.0),
    "E. Sin ritmo ni guion":         dict(w_pace=0.0, w_script=0.0),
    "F. Solo canales (sin extras)":  dict(w_env=0.0, w_pace=0.0, w_script=0.0,
                                          w_vac=0.0),
    "G. Entorno con doble peso":     dict(w_env=0.90),
    "H. Solo últimas 4 semanas":     dict(recent_only=True),
}


def matrix_variant(ev: pl.DataFrame, season: int, week: int, cfg: dict):
    prior = ev.filter((pl.col("season") < season)
                      | ((pl.col("season") == season) & (pl.col("week") < week)))
    if prior.height == 0:
        return pl.DataFrame()
    gl = defense._game_level(prior)
    cur = gl.filter(pl.col("season") == season)
    pri = gl.filter(pl.col("season") < season)
    gp = int(cur["week"].n_unique()) if cur.height else 0

    old_lambda = C.RIDGE_LAMBDA
    if cfg.get("no_ridge"):
        # lambda gigantesca -> los efectos se anulan y queda el promedio crudo
        defense.C.RIDGE_LAMBDA = 1e7

    windows = C.WINDOWS
    blend = C.WINDOW_BLEND
    if cfg.get("season_only"):
        windows, blend = {"season": None}, {"season": 1.0}
    if cfg.get("recent_only"):
        windows, blend = {"l4": 4}, {"l4": 1.0}

    parts = []
    for name, wb in windows.items():
        m = defense.build_matrix(cur, wb) if cur.height else pl.DataFrame()
        if m.height:
            parts.append((blend[name], m))
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
    prior_m = defense.build_matrix(pri) if pri.height else pl.DataFrame()
    out = defense.rank_and_z(defense.blend_with_prior(current, prior_m, gp))
    defense.C.RIDGE_LAMBDA = old_lambda
    return out


def run_fantasy(ev: pl.DataFrame, ros, snaps, ffopp, sched) -> dict:
    print("\n[B] Evaluando variantes de fantasy…")
    pg = players.player_game_usage(ev)
    weeks = sorted(sched.filter((pl.col("season") == TEST_SEASON)
                                & pl.col("home_score").is_not_null())["week"]
                   .unique().to_list())

    saved = (matchup.W_ENV, matchup.W_PACE, matchup.W_SCRIPT, matchup.W_VACATED)
    out: dict[str, dict] = {}

    for vname, cfg in FANT_VARIANTS.items():
        t = time.time()
        rows: list[dict] = []
        matchup.W_ENV = cfg.get("w_env", saved[0])
        matchup.W_PACE = cfg.get("w_pace", saved[1])
        matchup.W_SCRIPT = cfg.get("w_script", saved[2])
        matchup.W_VACATED = cfg.get("w_vac", saved[3])

        for wk in weeks:
            mtx = matrix_variant(ev, TEST_SEASON, wk, cfg)
            if mtx.height == 0:
                continue
            prior = ev.filter((pl.col("season") < TEST_SEASON)
                              | ((pl.col("season") == TEST_SEASON) & (pl.col("week") < wk)))
            prof = players.build_profiles(prior, ros, snaps, ffopp, TEST_SEASON)
            if prof.height == 0:
                continue
            wg = sched.filter((pl.col("season") == TEST_SEASON)
                              & (pl.col("week") == wk)).to_dicts()
            sc = matchup.compute(prof, mtx, wg, {}, {}, defense.METRIC_LABELS, fmt=FMT)
            if sc.height == 0:
                continue
            real = {r["pid"]: players.fantasy_points(r, FMT)
                    for r in pg.filter((pl.col("season") == TEST_SEASON)
                                       & (pl.col("week") == wk)).to_dicts()}
            pr = prof.to_dicts()
            base = {r["pid"]: (r.get(f"fppg_{FMT}") or 0.0) for r in pr}
            gcnt = {r["pid"]: (r.get("games") or 0) for r in pr}
            for s in sc.to_dicts():
                if s["pid"] not in real or gcnt.get(s["pid"], 0) < 3:
                    continue
                rows.append({"pos": s["pos"], "light": s["light"],
                             "score": s["matchup_score"],
                             "residual": real[s["pid"]] - base.get(s["pid"], 0.0)})
        F = pl.DataFrame(rows)
        if F.height == 0:
            continue
        g = F.filter(pl.col("light") == "verde")["residual"].to_numpy()
        r = F.filter(pl.col("light") == "rojo")["residual"].to_numpy()
        gap = float(g.mean() - r.mean()) if len(g) and len(r) else 0.0
        se = float(np.sqrt(g.var(ddof=1)/len(g) + r.var(ddof=1)/len(r))) if len(g) > 2 else 1
        sp = float(np.corrcoef(F["score"].to_numpy().argsort().argsort(),
                               F["residual"].to_numpy().argsort().argsort())[0, 1])
        by_pos = {}
        for pos in ("QB", "RB", "WR", "TE"):
            d = F.filter(pl.col("pos") == pos)
            gv = d.filter(pl.col("light") == "verde")["residual"]
            rv = d.filter(pl.col("light") == "rojo")["residual"]
            if len(gv) >= 25 and len(rv) >= 25:
                by_pos[pos] = round(float(gv.mean()) - float(rv.mean()), 2)
        out[vname] = {"n": int(F.height), "gap": round(gap, 3),
                      "t": round(gap / se, 2) if se else 0.0,
                      "spearman": round(sp, 4), "by_pos": by_pos}
        print(f"    {vname:<32} brecha {gap:+.3f}  t={gap/se:5.2f}  "
              f"rho={sp:+.3f}  ({time.time()-t:.0f}s)")

    matchup.W_ENV, matchup.W_PACE, matchup.W_SCRIPT, matchup.W_VACATED = saved
    return out


# ============================================================ main
def main() -> None:
    t0 = time.time()
    print("Cargando datos…")
    sched = nfl.load_schedules(list(range(2010, TEST_SEASON + 1)))
    pbp = nfl.load_pbp([TEST_SEASON - 2, TEST_SEASON - 1, TEST_SEASON])
    ros = nfl.load_rosters_weekly([TEST_SEASON - 2, TEST_SEASON - 1, TEST_SEASON])
    snaps = nfl.load_snap_counts([TEST_SEASON - 1, TEST_SEASON])
    ffopp = nfl.load_ff_opportunity([TEST_SEASON - 1, TEST_SEASON])
    ev = defense.build_play_events(pbp, ros)
    print(f"  listo ({time.time()-t0:.0f}s)")

    gdf = build_game_features(sched, ev)
    A = run_games(gdf)
    B = run_fantasy(ev, ros, snaps, ffopp, sched)

    res = {"season": TEST_SEASON, "games": A, "fantasy": B}
    with open("/home/claude/nfl-edge/tests/simulacion_resultados.json", "w",
              encoding="utf-8") as f:
        json.dump({k: v for k, v in res.items()}, f, ensure_ascii=False,
                  indent=1, default=str)

    print("\n" + "=" * 74)
    print("RESULTADOS — PARTIDOS (2025 completa, regular + playoffs)")
    print("=" * 74)
    print(f"{'combinación de variables':<34}{'acierto':>9}{'Brier':>9}{'logloss':>10}{'n':>6}")
    rows = sorted(A["variants"].items(), key=lambda kv: kv[1]["brier"])
    for name, m in rows:
        tag = "  <-- VEGAS" if name == "_market_raw" else ""
        nm = "MERCADO (Vegas puro)" if name == "_market_raw" else name
        print(f"{nm:<34}{m['acc']:>8.1f}%{m['brier']:>9.4f}{m['logloss']:>10.4f}"
              f"{m['n']:>6}{tag}")

    print("\nDonde el modelo DISCREPA de Vegas:")
    print(f"{'variante':<34}{'n':>5}{'modelo':>9}{'vegas':>8}")
    for name, d in A["disagree"].items():
        print(f"{name:<34}{d['n']:>5}{d['modelo']:>8.1f}%{d['vegas']:>7.1f}%")
    if A["stack"]:
        print("\nPrueba decisiva (mercado y modelo juntos en la misma regresión):")
        print(f"  coeficiente del MERCADO: {A['stack']['coef_mercado']:+.3f}")
        print(f"  coeficiente del MODELO : {A['stack']['coef_modelo']:+.3f}")

    print("\n" + "=" * 74)
    print("RESULTADOS — FANTASY (brecha verde-rojo en puntos por partido)")
    print("=" * 74)
    print(f"{'variante':<34}{'brecha':>8}{'t':>7}{'rho':>8}   por posición")
    for name, m in sorted(B.items(), key=lambda kv: -kv[1]["gap"]):
        bp = " ".join(f"{k}:{v:+.1f}" for k, v in m["by_pos"].items())
        print(f"{name:<34}{m['gap']:>+8.3f}{m['t']:>7.2f}{m['spearman']:>+8.3f}   {bp}")

    print(f"\nTiempo total: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
