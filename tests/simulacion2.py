"""Segunda ronda: probar a fondo la variante ganadora (cambio de QB).

La primera ronda dejó dos preguntas abiertas:
  1. La prueba de apilamiento se hizo con la variante 7, no con la 6 (la del
     cambio de QB), que fue la única que le ganó a Vegas en los desacuerdos.
  2. ¿Los desacuerdos ganados son señal real o cabe el azar? Hay que medirlo.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import polars as pl
from scipy import stats

sys.path.insert(0, "/home/claude/nfl-edge")
import nflreadpy as nfl                                    # noqa: E402
from pipeline import defense                               # noqa: E402
from tests.simulacion import (TEST_SEASON, VARIANTS, build_game_features,  # noqa: E402
                              fit_logistic, logit, metrics, predict_logistic)

EXTRA = {
    "12. Mercado + cambio de QB": ["market_logit", "qb_change_home", "qb_change_away"],
    "13. Mercado + QB + descanso": ["market_logit", "qb_change_home",
                                    "qb_change_away", "rest_diff"],
    "14. Mercado + QB + Elo + EPA": ["market_logit", "qb_change_home",
                                     "qb_change_away", "elo_diff", "epa_margin"],
}


def walkforward(df: pl.DataFrame, feats: list[str]) -> tuple[np.ndarray, np.ndarray, pl.DataFrame]:
    test = df.filter(pl.col("season") == TEST_SEASON)
    preds, ys, keep = [], [], []
    for wk in sorted(test["week"].unique().to_list()):
        tr = df.filter((pl.col("season") < TEST_SEASON)
                       | ((pl.col("season") == TEST_SEASON) & (pl.col("week") < wk))
                       ).drop_nulls(feats + ["y"])
        te = test.filter(pl.col("week") == wk).drop_nulls(feats + ["y"])
        if tr.height < 200 or te.height == 0:
            continue
        Xtr = tr.select(feats).to_numpy().astype(float)
        mu, sd = Xtr.mean(0), Xtr.std(0)
        sd[sd < 1e-9] = 1.0
        b = fit_logistic((Xtr - mu) / sd, tr["y"].to_numpy().astype(float))
        preds.append(predict_logistic(b, (te.select(feats).to_numpy().astype(float) - mu) / sd))
        ys.append(te["y"].to_numpy().astype(float))
        keep.append(te)
    return np.concatenate(preds), np.concatenate(ys), pl.concat(keep, how="diagonal_relaxed")


def main() -> None:
    print("Cargando…")
    sched = nfl.load_schedules(list(range(2010, TEST_SEASON + 1)))
    pbp = nfl.load_pbp([TEST_SEASON - 2, TEST_SEASON - 1, TEST_SEASON])
    ros = nfl.load_rosters_weekly([TEST_SEASON - 2, TEST_SEASON - 1, TEST_SEASON])
    ev = defense.build_play_events(pbp, ros)
    df = build_game_features(sched, ev)
    df.write_parquet("/home/claude/nfl-edge/tests/game_features.parquet")

    todos = {**VARIANTS, **EXTRA}
    out: dict = {}
    store: dict[str, tuple] = {}

    print("\nEvaluando…")
    for name, feats in todos.items():
        try:
            p, y, te = walkforward(df, feats)
        except ValueError:
            continue
        store[name] = (p, y, te)
        out[name] = metrics(p, y)

    # mercado como referencia, sobre el mismo conjunto
    _, y0, te0 = store["8. SOLO el mercado (Vegas)"]
    mkt = te0["market_p"].to_numpy().astype(float)
    out["MERCADO crudo"] = metrics(mkt, y0)

    print(f"\n{'variante':<34}{'acierto':>9}{'Brier':>9}{'logloss':>10}")
    for name, m in sorted(out.items(), key=lambda kv: kv[1]["brier"]):
        print(f"{name:<34}{m['acc']:>8.1f}%{m['brier']:>9.4f}{m['logloss']:>10.4f}")

    # ---------------------------------------- desacuerdos con significancia
    print(f"\n{'variante':<34}{'n':>5}{'modelo':>9}{'vegas':>8}{'p-valor':>10}")
    dis: dict = {}
    for name, (p, y, te) in store.items():
        m = te["market_p"].to_numpy().astype(float)
        d = (p > 0.5) != (m > 0.5)
        if d.sum() < 10:
            continue
        wins = int(((p[d] > 0.5) == (y[d] > 0.5)).sum())
        n = int(d.sum())
        # ¿es distinguible de lanzar una moneda?
        pv = float(stats.binomtest(wins, n, 0.5).pvalue)
        dis[name] = {"n": n, "aciertos": wins,
                     "modelo": round(100 * wins / n, 1),
                     "vegas": round(100 * (n - wins) / n, 1), "p": round(pv, 3)}
        print(f"{name:<34}{n:>5}{100*wins/n:>8.1f}%{100*(n-wins)/n:>7.1f}%{pv:>10.3f}")

    # ---------------------------------------- prueba decisiva por variante
    print("\nPRUEBA DECISIVA — mercado y modelo en la misma regresión")
    print("(si el coeficiente del modelo es ~0 o negativo, no aportamos nada nuevo)")
    print(f"{'variante propia':<34}{'coef mercado':>14}{'coef modelo':>13}")
    stacks: dict = {}
    for name in ("1. Solo Elo", "6. + CAMBIO DE QB", "7. TODO menos el mercado"):
        if name not in store:
            continue
        p, y, te = store[name]
        m = te["market_p"].to_numpy().astype(float)
        X = np.column_stack([[logit(v) for v in m], [logit(v) for v in p]])
        mu, sd = X.mean(0), X.std(0)
        sd[sd < 1e-9] = 1.0
        b = fit_logistic((X - mu) / sd, y, l2=0.5)
        stacks[name] = {"mercado": round(float(b[1]), 3), "modelo": round(float(b[2]), 3)}
        print(f"{name:<34}{b[1]:>+14.3f}{b[2]:>+13.3f}")

    # ---------------------------------------- ¿cuánto pesa el cambio de QB?
    d = df.filter((pl.col("season") == TEST_SEASON)
                  & (pl.col("market_p").is_not_null()))
    chg = d.filter((pl.col("qb_change_home") == 1) | (pl.col("qb_change_away") == 1))
    print(f"\nPartidos de 2025 con cambio de QB: {chg.height} de {d.height} "
          f"({100*chg.height/d.height:.0f}%)")
    if chg.height > 8:
        mk = chg["market_p"].to_numpy().astype(float)
        yy = chg["y"].to_numpy().astype(float)
        print(f"  Vegas acierta en esos partidos: {100*((mk>0.5)==(yy>0.5)).mean():.1f}%")
        for name in ("6. + CAMBIO DE QB", "12. Mercado + cambio de QB"):
            if name in store:
                p, y, te = store[name]
                mask = ((te["qb_change_home"] == 1) | (te["qb_change_away"] == 1)).to_numpy()
                if mask.sum() > 5:
                    print(f"  {name}: {100*((p[mask]>0.5)==(y[mask]>0.5)).mean():.1f}% "
                          f"(n={int(mask.sum())})")

    json.dump({"variantes": out, "desacuerdos": dis, "apilamiento": stacks},
              open("/home/claude/nfl-edge/tests/simulacion2_resultados.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
