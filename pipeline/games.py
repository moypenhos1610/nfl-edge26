"""Modelo de resultados de partidos + comparación contra la línea de Vegas.

Tres señales que se combinan:
  1. Elo con margen de victoria (histórico, estable, capta la calidad general)
  2. EPA por jugada ofensiva y defensiva AJUSTADA POR RIVAL (capta la forma actual)
  3. Contexto: localía, días de descanso, viaje

La conversión de margen a probabilidad se CALIBRA con temporadas reales, no se
asume. Y el resultado se compara contra la probabilidad implícita del mercado
(quitándole el vig) para marcar dónde el modelo ve valor.
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl

from . import config as C

HFA_PRIOR = 1.7          # puntos de localía (se re-estima con datos)
ELO_K = 20.0
ELO_MEAN = 1505.0
ELO_REVERT = 1.0 / 3.0   # regresión a la media entre temporadas


# ------------------------------------------------------------------- Elo
def compute_elo(sched: pl.DataFrame, before: tuple[int, int] | None = None
                ) -> tuple[dict[str, float], pl.DataFrame]:
    """Elo con margen de victoria. Devuelve ratings finales y el histórico previo
    a cada partido (para poder calibrar sin fuga de información)."""
    need = {"season", "week", "home_team", "away_team", "home_score", "away_score"}
    if sched.height == 0 or not need.issubset(set(sched.columns)):
        return {}, pl.DataFrame()

    games = (sched
             .filter(pl.col("home_score").is_not_null() & pl.col("away_score").is_not_null())
             .sort(["season", "week"]))
    if before:  # sólo partidos ANTERIORES a la semana objetivo (evita fuga de info)
        bs, bw = before
        games = games.filter((pl.col("season") < bs)
                             | ((pl.col("season") == bs) & (pl.col("week") < bw)))
    ratings: dict[str, float] = {}
    last_season: int | None = None
    hist: list[dict] = []

    for g in games.iter_rows(named=True):
        s, h, a = g["season"], g["home_team"], g["away_team"]
        if last_season is not None and s != last_season:
            for t in ratings:  # regresión a la media entre temporadas
                ratings[t] = ratings[t] + ELO_REVERT * (ELO_MEAN - ratings[t])
        last_season = s

        rh = ratings.setdefault(h, ELO_MEAN)
        ra = ratings.setdefault(a, ELO_MEAN)
        diff = rh + 55.0 - ra                      # 55 Elo ≈ localía
        exp_h = 1.0 / (1.0 + 10 ** (-diff / 400.0))

        hist.append({"game_id": g.get("game_id"), "season": s, "week": g["week"],
                     "home_team": h, "away_team": a,
                     "elo_diff": rh - ra, "elo_exp_home": exp_h,
                     "margin": g["home_score"] - g["away_score"]})

        mov = abs(g["home_score"] - g["away_score"])
        winner_favored = diff if g["home_score"] > g["away_score"] else -diff
        mult = math.log(max(mov, 1) + 1) * (2.2 / (winner_favored * 0.001 + 2.2))
        actual = 1.0 if g["home_score"] > g["away_score"] else (
            0.5 if g["home_score"] == g["away_score"] else 0.0)
        delta = ELO_K * mult * (actual - exp_h)
        ratings[h] = rh + delta
        ratings[a] = ra - delta

    return ratings, pl.DataFrame(hist) if hist else pl.DataFrame()


# ------------------------------------------------- EPA ajustada por rival
def epa_ratings(game_level: pl.DataFrame, season: int) -> dict[str, dict[str, float]]:
    """EPA ofensiva y defensiva por equipo, ajustada por calidad del rival."""
    if game_level.height == 0:
        return {}
    from .defense import _ridge_adjust  # mismo motor ridge

    g = game_level.filter(pl.col("season") >= season - 1)
    if g.height == 0:
        g = game_level

    g = g.with_columns(
        ((pl.col("n_pass_epa").fill_null(0) + pl.col("n_rush_epa").fill_null(0))
         / pl.when(pl.col("d_plays") > 0).then(pl.col("d_plays")).otherwise(None))
        .alias("_v"),
        pl.col("d_plays").cast(pl.Float64).alias("_w"),
    )
    # efecto defensa = EPA permitida vs ofensa promedio (menor = mejor defensa)
    def_eff = _ridge_adjust(g, "_v", "_w")
    # efecto ofensa: invertimos los roles
    g2 = g.rename({"def": "_tmp"}).rename({"off": "def"}).rename({"_tmp": "off"})
    off_eff = _ridge_adjust(g2, "_v", "_w")

    teams = set(def_eff) | set(off_eff)
    if not teams:
        return {}
    dm = np.mean(list(def_eff.values())) if def_eff else 0.0
    om = np.mean(list(off_eff.values())) if off_eff else 0.0
    return {t: {"off": float(off_eff.get(t, om) - om),
                "def": float(def_eff.get(t, dm) - dm)} for t in teams}


# ------------------------------------------------------------ calibración
def _logistic_fit(x: np.ndarray, y: np.ndarray, iters: int = 250) -> tuple[float, float]:
    """Regresión logística de 1 variable por Newton-Raphson. Devuelve (b0, b1)."""
    b0, b1 = 0.0, 0.12
    for _ in range(iters):
        z = b0 + b1 * x
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        w = np.clip(p * (1 - p), 1e-6, None)
        r = y - p
        X = np.column_stack([np.ones_like(x), x])
        H = X.T @ (X * w[:, None]) + np.eye(2) * 1e-6
        gvec = X.T @ r
        try:
            step = np.linalg.solve(H, gvec)
        except np.linalg.LinAlgError:
            break
        b0 += step[0]
        b1 += step[1]
        if abs(step).max() < 1e-8:
            break
    return float(b0), float(b1)


def calibrate(elo_hist: pl.DataFrame) -> dict:
    """Ajusta margen ~ elo_diff y prob ~ margen sobre temporadas históricas."""
    if elo_hist.height < 200:
        return {"m_a": 0.0, "m_b": 0.043, "hfa": HFA_PRIOR, "b0": 0.0, "b1": 0.145,
                "n": 0, "note": "valores por defecto (datos insuficientes)"}
    d = elo_hist.drop_nulls(["elo_diff", "margin"])
    x = d["elo_diff"].to_numpy().astype(float)
    y = d["margin"].to_numpy().astype(float)
    # margen = a + b*elo_diff ; el intercepto captura la localía
    A = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    hfa, b = float(coef[0]), float(coef[1])

    pred_margin = hfa + b * x
    win = (y > 0).astype(float)
    mask = y != 0
    b0, b1 = _logistic_fit(pred_margin[mask], win[mask])
    return {"m_a": hfa, "m_b": b, "hfa": hfa, "b0": b0, "b1": b1,
            "n": int(d.height), "note": f"calibrado con {d.height} partidos"}


def win_prob(margin: float, cal: dict) -> float:
    z = cal["b0"] + cal["b1"] * margin
    return float(1.0 / (1.0 + math.exp(-max(min(z, 30), -30))))


# ------------------------------------------------------------------ mercado
def devig_moneyline(ml_home: float | None, ml_away: float | None) -> float | None:
    """Probabilidad real del mercado para el LOCAL, quitando la comisión."""
    def imp(ml):
        if ml is None:
            return None
        ml = float(ml)
        return (-ml) / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)
    ph, pa = imp(ml_home), imp(ml_away)
    if ph is None or pa is None or (ph + pa) <= 0:
        return None
    return ph / (ph + pa)


# ------------------------------------------------------------------ backtest
def backtest(elo_hist: pl.DataFrame, sched: pl.DataFrame) -> dict:
    """Validación walk-forward honesta: entrena con el pasado, prueba con el futuro."""
    if elo_hist.height < 500:
        return {"available": False}
    seasons = sorted(elo_hist["season"].unique().to_list())
    test_seasons = [s for s in seasons if s >= seasons[0] + 3]
    correct = n = 0
    brier = 0.0
    mkt_correct = mkt_n = 0
    ats_correct = ats_n = 0

    ml = None
    if {"spread_line", "home_moneyline", "away_moneyline", "game_id"}.issubset(set(sched.columns)):
        ml = sched.select(["game_id", "spread_line", "home_moneyline",
                           "away_moneyline"]).to_dicts()
        ml = {r["game_id"]: r for r in ml}

    for s in test_seasons:
        train = elo_hist.filter(pl.col("season") < s)
        test = elo_hist.filter(pl.col("season") == s)
        if train.height < 200 or test.height == 0:
            continue
        cal = calibrate(train)
        for r in test.iter_rows(named=True):
            if r["margin"] == 0:
                continue
            m = cal["m_a"] + cal["m_b"] * r["elo_diff"]
            p = win_prob(m, cal)
            actual = 1.0 if r["margin"] > 0 else 0.0
            n += 1
            correct += int((p > 0.5) == (actual > 0.5))
            brier += (p - actual) ** 2
            if ml and r["game_id"] in ml:
                row = ml[r["game_id"]]
                pm = devig_moneyline(row.get("home_moneyline"), row.get("away_moneyline"))
                if pm is not None:
                    mkt_n += 1
                    mkt_correct += int((pm > 0.5) == (actual > 0.5))
                sp = row.get("spread_line")
                if sp is not None:
                    ats_n += 1
                    # ¿acertamos el lado del spread?
                    ats_correct += int((m > sp) == (r["margin"] > sp))
    if n == 0:
        return {"available": False}
    return {
        "available": True, "n": n,
        "accuracy": round(100 * correct / n, 1),
        "brier": round(brier / n, 4),
        "market_accuracy": round(100 * mkt_correct / mkt_n, 1) if mkt_n else None,
        "ats_accuracy": round(100 * ats_correct / ats_n, 1) if ats_n else None,
        "seasons": f"{test_seasons[0]}-{test_seasons[-1]}" if test_seasons else "",
    }


# ------------------------------------------------------------------ predicción
def _records(sched: pl.DataFrame, season: int, week: int) -> dict[str, dict]:
    """Récord de temporada regular ANTES de la semana objetivo."""
    rec: dict[str, dict] = {}
    need = {"season", "week", "game_type", "home_score", "away_score",
            "home_team", "away_team"}
    if sched.height == 0 or not need.issubset(set(sched.columns)):
        return rec
    d = sched.filter((pl.col("season") == season)
                     & (pl.col("week") < week)
                     & (pl.col("game_type") == "REG")
                     & pl.col("home_score").is_not_null())
    for g in d.iter_rows(named=True):
        for t, own, opp in ((g["home_team"], g["home_score"], g["away_score"]),
                            (g["away_team"], g["away_score"], g["home_score"])):
            r = rec.setdefault(t, {"w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0})
            r["pf"] += own
            r["pa"] += opp
            if own > opp:
                r["w"] += 1
            elif own < opp:
                r["l"] += 1
            else:
                r["t"] += 1
    return rec


def predict_week(sched: pl.DataFrame, game_level: pl.DataFrame, season: int,
                 week: int) -> tuple[list[dict], dict]:
    """Predicciones de la semana con favorito, confianza y edge contra Vegas."""
    if sched is None or sched.height == 0 or "season" not in sched.columns:
        return [], {"calibration": calibrate(pl.DataFrame()),
                    "backtest": {"available": False}}
    elo, hist = compute_elo(sched, before=(season, week))
    cal = calibrate(hist) if hist.height else calibrate(pl.DataFrame())
    bt = backtest(hist, sched) if hist.height else {"available": False}
    epa = epa_ratings(game_level, season)
    rec = _records(sched, season, week)

    games = sched.filter((pl.col("season") == season) & (pl.col("week") == week))
    out: list[dict] = []
    for g in games.iter_rows(named=True):
        h, a = g["home_team"], g["away_team"]
        eh, ea = elo.get(h, ELO_MEAN), elo.get(a, ELO_MEAN)
        elo_margin = cal["m_a"] + cal["m_b"] * (eh - ea)

        # señal de forma actual: EPA ajustada
        eph = epa.get(h, {"off": 0.0, "def": 0.0})
        epa_a = epa.get(a, {"off": 0.0, "def": 0.0})
        # def_eff bajo = defensa dura -> restamos
        epa_margin = ((eph["off"] - epa_a["off"]) - (eph["def"] - epa_a["def"])) * 62.0

        rest_adj = 0.0
        if g.get("home_rest") is not None and g.get("away_rest") is not None:
            rest_adj = np.clip((float(g["home_rest"]) - float(g["away_rest"])) * 0.10, -1.2, 1.2)

        blend = 0.68 if epa else 1.0
        margin = blend * elo_margin + (1 - blend) * (elo_margin + epa_margin) / 2 + rest_adj
        if epa:
            margin = 0.62 * elo_margin + 0.38 * (epa_margin + cal["hfa"]) + rest_adj

        # ---------------------------------------------- ANCLAJE AL MERCADO
        # 15 combinaciones probadas contra la temporada 2025: ninguna le gana a
        # Vegas, y el coeficiente de nuestro modelo junto al del mercado es
        # NEGATIVO. Conclusión honesta: el mercado manda, y sólo nos separamos
        # de él cuando nuestras señales propias coinciden entre sí y la
        # diferencia es grande.
        raw_margin = float(margin)
        vs = g.get("spread_line")
        consensus, signals = 0, []
        if vs is not None:
            diff = raw_margin - float(vs)
            direction = 1 if diff > 0 else -1
            # señal 1: Elo apunta al mismo lado
            if (eh - ea) * direction > 40:
                consensus += 1
                signals.append("rating Elo")
            # señal 2: EPA ajustada apunta al mismo lado
            if epa and epa_margin * direction > 1.5:
                consensus += 1
                signals.append("EPA ajustada por rival")
            # señal 3: descanso
            if abs(rest_adj) > 0.4 and rest_adj * direction > 0:
                consensus += 1
                signals.append("días de descanso")

            strong = (abs(diff) >= C.DISAGREE_MIN_PTS
                      and consensus >= C.CONSENSUS_REQUIRED)
            anchor = C.MARKET_ANCHOR if not strong else (C.MARKET_ANCHOR - 0.25)
            margin = anchor * float(vs) + (1 - anchor) * raw_margin
        else:
            strong = False

        p_home = win_prob(margin, cal)
        p_mkt = devig_moneyline(g.get("home_moneyline"), g.get("away_moneyline"))
        edge = (p_home - p_mkt) if p_mkt is not None else None

        fav, pfav = (h, p_home) if p_home >= 0.5 else (a, 1 - p_home)
        conf = ("alta" if pfav >= 0.68 else "media" if pfav >= 0.58 else "baja")

        # ---- postura frente al mercado, con reglas explícitas
        gap = (raw_margin - float(vs)) if vs is not None else None
        if gap is None:
            stance, ats_pick = "sin_linea", None
        elif abs(gap) < C.DISAGREE_MIN_PTS:
            stance, ats_pick = "coincide", None
        elif not strong:
            stance, ats_pick = "discrepa_debil", None
        else:
            stance = ("discrepa_fuerte" if abs(gap) >= C.DISAGREE_STRONG_PTS
                      else "discrepa")
            ats_pick = ((h if gap > 0 else a) if abs(gap) >= C.ATS_MIN_EDGE else None)

        rh = rec.get(h, {"w": 0, "l": 0, "t": 0})
        ra = rec.get(a, {"w": 0, "l": 0, "t": 0})

        out.append({
            "game_id": g.get("game_id"), "week": week, "gameday": g.get("gameday"),
            "gametime": g.get("gametime"),
            "home_team": h, "away_team": a,
            "home_record": f"{rh['w']}-{rh['l']}" + (f"-{rh['t']}" if rh["t"] else ""),
            "away_record": f"{ra['w']}-{ra['l']}" + (f"-{ra['t']}" if ra["t"] else ""),
            "elo_home": round(eh, 1), "elo_away": round(ea, 1),
            "model_margin": round(float(margin), 2),
            "raw_margin": round(raw_margin, 2),
            "stance": stance, "ats_pick": ats_pick,
            "consensus": consensus, "consensus_signals": signals,
            "vegas_spread": g.get("spread_line"),
            "vegas_total": g.get("total_line"),
            "p_home": round(p_home, 4),
            "p_market_home": round(p_mkt, 4) if p_mkt is not None else None,
            "favorite": fav, "favorite_pct": round(100 * pfav, 1),
            "confidence": conf,
            "edge": round(100 * edge, 1) if edge is not None else None,
            "spread_edge": (round(float(margin) - float(g["spread_line"]), 2)
                            if g.get("spread_line") is not None else None),
            "epa_off_home": round(eph["off"], 4), "epa_def_home": round(eph["def"], 4),
            "epa_off_away": round(epa_a["off"], 4), "epa_def_away": round(epa_a["def"], 4),
            "explanation": _explain(h, a, fav, pfav, raw_margin, g.get("spread_line"),
                                    rh, ra, eh, ea, edge, stance, signals, ats_pick),
        })
    return out, {"calibration": cal, "backtest": bt}


STANCE_ES = {
    "coincide": "Coincide con el mercado.",
    "discrepa_debil": ("Diferencia con el mercado, pero SIN respaldo suficiente de "
                       "nuestras propias señales: se manda con Vegas."),
    "discrepa": "Discrepamos del mercado y las señales propias lo respaldan.",
    "discrepa_fuerte": "DISCREPANCIA FUERTE con el mercado, con respaldo múltiple.",
    "sin_linea": "Sin línea de mercado disponible.",
}


def _explain(h, a, fav, pfav, margin, spread, rh, ra, eh, ea, edge,
             stance="coincide", signals=None, ats_pick=None) -> str:
    parts = [f"{fav} favorito con {100*pfav:.0f}% de probabilidad."]
    elo_gap = abs(eh - ea)
    stronger = h if eh > ea else a
    if elo_gap > 120:
        parts.append(f"{stronger} es claramente el mejor equipo por rating "
                     f"({eh:.0f} vs {ea:.0f}).")
    elif elo_gap < 35:
        parts.append(f"Equipos muy parejos por rating ({eh:.0f} vs {ea:.0f}); "
                     f"la localía decide.")
    if rh["w"] + rh["l"] > 0:
        parts.append(f"Récords: {h} {rh['w']}-{rh['l']} (local) vs "
                     f"{a} {ra['w']}-{ra['l']}.")
    if spread is not None:
        diff = margin - float(spread)
        lado = h if diff > 0 else a
        parts.append(f"Margen bruto del modelo {margin:+.1f} vs spread de Vegas "
                     f"{spread:+.1f} (diferencia {diff:+.1f}).")
        parts.append(STANCE_ES.get(stance, ""))
        if stance in ("discrepa", "discrepa_fuerte"):
            sig = ", ".join(signals or []) or "señales internas"
            parts.append(f"Respaldo: {sig}. Se inclina hacia {lado}.")
            if ats_pick:
                parts.append(f"Lado del spread señalado: {ats_pick} "
                             f"(informativo — contra la línea el modelo mide 46-50% "
                             f"histórico, sin ventaja demostrada).")
        elif stance == "discrepa_debil":
            parts.append("Regla del modelo: sin al menos dos señales propias "
                         "coincidiendo, no se apuesta contra Vegas.")
    return " ".join(parts)
