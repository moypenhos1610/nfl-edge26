"""El cruce: fortaleza del jugador × debilidad ESPECÍFICA del rival.

No comparamos "jugador bueno vs defensa mala". Descomponemos al jugador en
canales de producción según su uso real y evaluamos cada canal contra la
permisividad de la defensa EN ESE CANAL. Así es como sale el insight no obvio:
un RB puede tener matchup rojo por tierra y verde por el aire contra la misma
defensa, y lo que importa es cuál de los dos canales es el suyo.
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl

from . import config as C

# canal -> [(métrica defensiva, peso)] por posición del atacante
CHANNEL_METRICS: dict[str, dict[str, list[tuple[str, float]]]] = {
    "rush": {
        "RB": [("rb_rush_ypc", 0.50), ("rb_rush_ypg", 0.25), ("rb_expl_pg", 0.15),
               ("rush_epa", 0.10)],
        "SWR": [("rush_epa", 1.00)],
        "QB": [("qb_rush_ypc", 0.45), ("qb_rush_ypg", 0.40), ("rush_epa", 0.15)],
        "WR": [("rush_epa", 1.00)],
        "TE": [("rush_epa", 1.00)],
    },
    "rec_short": {
        "RB": [("rb_rec_ypt", 0.40), ("rb_tgt_pg", 0.30), ("rb_rec_ypr", 0.30)],
        "SWR": [("swr_rec_ypt", 0.45), ("swr_tgt_pg", 0.25), ("wr_short_ypt", 0.30)],
        "WR": [("wr_short_ypt", 0.50), ("wr_yac_pr", 0.25), ("wr_tgt_pg", 0.25)],
        "TE": [("te_short_ypt", 0.45), ("te_tgt_pg", 0.35), ("te_rec_ypt", 0.20)],
        "QB": [("pass_epa", 1.00)],
    },
    "rec_deep": {
        "RB": [("rb_rec_ypt", 0.60), ("pass_epa", 0.40)],
        "SWR": [("swr_rec_ypt", 0.40), ("wr_deep_ypt", 0.35), ("pass_epa", 0.25)],
        "WR": [("wr_deep_ypt", 0.55), ("bomb_ypt", 0.30), ("pass_epa", 0.15)],
        "TE": [("te_rec_ypt", 0.55), ("bomb_ypt", 0.25), ("pass_epa", 0.20)],
        "QB": [("bomb_ypt", 0.60), ("pass_epa", 0.40)],
    },
    "redzone": {
        "RB": [("rz_td_rate", 0.45), ("rb_rush_td_pg", 0.35), ("rb_rec_td_pg", 0.20)],
        "SWR": [("rz_td_rate", 0.45), ("wr_rec_td_pg", 0.55)],
        "WR": [("rz_td_rate", 0.45), ("wr_rec_td_pg", 0.55)],
        "TE": [("rz_td_rate", 0.45), ("te_rec_td_pg", 0.55)],
        "QB": [("rz_td_rate", 1.00)],
    },
    "pass": {
        "QB": [("pass_epa", 0.55), ("wr_rec_ypt", 0.25), ("bomb_ypt", 0.20)],
        "RB": [("pass_epa", 1.0)], "WR": [("pass_epa", 1.0)], "TE": [("pass_epa", 1.0)],
        "SWR": [("pass_epa", 1.0)],
    },
}

CHANNEL_ES = {"rush": "por tierra", "rec_short": "de recepción corta",
              "rec_deep": "de recepción profunda", "redzone": "en zona roja",
              "pass": "de pase"}

# Cuánto pesa cada modificador sobre el score bruto
W_ENV = 0.45      # total implícito del equipo (entorno de anotación)
W_PACE = 0.18     # ritmo del rival
W_SCRIPT = 0.22   # guion de juego (favorito/underdog)
W_VACATED = 0.30  # objetivos/acarreos liberados por lesiones


# ------------------------------------------------------------------ helpers
def implied_totals(spread_line: float | None, total_line: float | None,
                   is_home: bool) -> tuple[float, float]:
    """Convierte spread + total en puntos implícitos del equipo y del rival.

    En nflverse `spread_line` es el spread del LOCAL (positivo = local favorito).
    """
    if spread_line is None or total_line is None:
        return (float("nan"), float("nan"))
    home_pts = total_line / 2.0 + spread_line / 2.0
    away_pts = total_line / 2.0 - spread_line / 2.0
    return (home_pts, away_pts) if is_home else (away_pts, home_pts)


def _z(matrix_row: dict, metric: str) -> float | None:
    v = matrix_row.get(f"z_{metric}")
    return None if v is None else float(v)


def _rank(matrix_row: dict, metric: str) -> int | None:
    v = matrix_row.get(f"rank_{metric}")
    return None if v is None else int(v)


def channel_weakness(matrix_row: dict, channel: str, pos: str
                     ) -> tuple[float, list[tuple[str, float, int | None]]]:
    """Debilidad de la defensa en un canal: z ponderado + detalle por métrica."""
    specs = CHANNEL_METRICS.get(channel, {}).get(pos, [])
    total_w, acc = 0.0, 0.0
    detail: list[tuple[str, float, int | None]] = []
    for metric, w in specs:
        z = _z(matrix_row, metric)
        if z is None:
            continue
        acc += z * w
        total_w += w
        detail.append((metric, z, _rank(matrix_row, metric)))
    if total_w <= 0:
        return 0.0, detail
    return acc / total_w, detail


def separation_factor(sep: float | None, pos: str) -> float:
    """Cuánta señal de matchup conservamos para este receptor.

    Medido en el backtest 2025: en receptores que separan poco la señal se
    INVIERTE (-0.83) y en los que separan bien funciona (+0.28). Donde el dato
    no es confiable, encogemos la señal hacia cero en vez de fingir certeza.
    """
    if pos not in ("WR", "TE") or sep is None:
        return 1.0
    t = (float(sep) - C.SEP_LOW) / max(C.SEP_HIGH - C.SEP_LOW, 1e-6)
    t = min(max(t, 0.0), 1.0)
    return C.SEP_FACTOR_MIN + (1.0 - C.SEP_FACTOR_MIN) * t


def _pct_to_score(values: np.ndarray) -> np.ndarray:
    """Percentil 0-100 dentro del grupo (robusto a outliers)."""
    if len(values) == 0:
        return values
    if len(values) == 1:
        return np.array([60.0])
    order = values.argsort().argsort()
    return 100.0 * order / (len(values) - 1)


def light(score: float) -> str:
    if score >= C.LIGHT_GREEN:
        return "verde"
    if score >= C.LIGHT_YELLOW:
        return "amarillo"
    return "rojo"


# ---------------------------------------------------------------- narrativa
def build_insight(name: str, pos: str, opp: str, channels: dict[str, float],
                  contributions: list[tuple[str, float, float, list]],
                  env: dict, flags: list[str], matrix_labels: dict) -> str:
    """Explicación en español, generada por reglas (determinista, sin inventar).

    Ordena los canales por su contribución al score y describe los dos que más
    pesaron, citando el ranking real de la defensa en la métrica concreta.
    """
    parts: list[str] = []
    ranked = sorted(contributions, key=lambda x: abs(x[2]), reverse=True)
    pos_terms = [c for c in ranked if c[2] > 0.03]
    neg_terms = [c for c in ranked if c[2] < -0.03]

    def describe(term, positive: bool) -> str:
        ch, weight, contrib, detail = term
        if not detail:
            return ""
        metric, _z, rank = max(detail, key=lambda d: abs(d[1]))
        label = matrix_labels.get(metric, metric)
        pos_txt = f"{int(round(weight * 100))}% de su valor"
        if rank is None:
            return (f"su producción {CHANNEL_ES.get(ch, ch)} ({pos_txt}) "
                    f"{'encaja' if positive else 'choca'} con esta defensa")
        cual = "permisivas" if positive else "duras"
        return (f"{pos_txt} viene {CHANNEL_ES.get(ch, ch)}, y {opp} es "
                f"{rank}º de 32 en {label} — de las más {cual} en esa casilla")

    if pos_terms:
        parts.append("Ventaja: " + describe(pos_terms[0], True) + ".")
        if len(pos_terms) > 1 and abs(pos_terms[1][2]) > 0.12:
            parts.append("También suma " + describe(pos_terms[1], True) + ".")
    if neg_terms and abs(neg_terms[0][2]) > 0.12:
        parts.append("En contra: " + describe(neg_terms[0], False) + ".")

    # el insight no obvio: canal fuerte dentro de una defensa globalmente dura
    if pos_terms and neg_terms:
        best_ch = pos_terms[0][0]
        worst_ch = neg_terms[0][0]
        if best_ch != worst_ch and pos_terms[0][2] > 0.15 and neg_terms[0][2] < -0.15:
            parts.append(f"Insight: el matchup NO es parejo. Búscalo "
                         f"{CHANNEL_ES.get(best_ch, best_ch)}, no "
                         f"{CHANNEL_ES.get(worst_ch, worst_ch)}.")

    it = env.get("implied_total")
    if it is not None and not (isinstance(it, float) and math.isnan(it)):
        if it >= 26:
            parts.append(f"Entorno de anotación alto: Vegas implica {it:.1f} puntos "
                         f"para su equipo.")
        elif it <= 18.5:
            parts.append(f"Entorno pobre: Vegas sólo implica {it:.1f} puntos para "
                         f"su equipo.")
    sp = env.get("spread_own")
    if sp is not None and not (isinstance(sp, float) and math.isnan(sp)):
        if sp <= -6 and pos in ("WR", "TE", "QB"):
            parts.append("Su equipo es underdog claro: más volumen de pase por guion.")
        elif sp >= 6 and pos == "RB":
            parts.append("Su equipo es favorito claro: guion de juego favorece acarreos.")

    parts.extend(flags)
    if not parts:
        parts.append(f"Matchup neutro contra {opp}: sin ventaja ni desventaja marcada.")
    return " ".join(parts)


# ---------------------------------------------------------------- principal
def compute(profiles: pl.DataFrame, matrix: pl.DataFrame, week_games: list[dict],
            injuries: dict[str, str], weather: dict[str, dict],
            metric_labels: dict, fmt: str = C.DEFAULT_FORMAT,
            confidence: str = "media") -> pl.DataFrame:
    """Score de matchup por jugador para la semana objetivo."""
    if profiles.height == 0 or matrix.height == 0 or not week_games:
        return pl.DataFrame()

    mrows = {r["team"]: r for r in matrix.to_dicts()}

    # contexto por equipo: rival, casa/visita, líneas, clima
    ctx: dict[str, dict] = {}
    for g in week_games:
        h, a = g.get("home_team"), g.get("away_team")
        sp, tot = g.get("spread_line"), g.get("total_line")
        wx = weather.get(g.get("game_id"), {})
        for team, opp, is_home in ((h, a, True), (a, h, False)):
            if not team:
                continue
            own, opp_pts = implied_totals(sp, tot, is_home)
            own_spread = (sp if is_home else -sp) if sp is not None else None
            ctx[team] = {
                "opp": opp, "is_home": is_home, "game_id": g.get("game_id"),
                "gameday": g.get("gameday"), "gametime": g.get("gametime"),
                "spread_own": own_spread, "total_line": tot,
                "implied_total": own, "implied_opp": opp_pts, "weather": wx,
            }

    # entorno normalizado a través de los equipos que juegan esta semana
    its = np.array([v["implied_total"] for v in ctx.values()
                    if v["implied_total"] == v["implied_total"]], dtype=float)
    it_mu, it_sd = (its.mean(), its.std() or 1.0) if len(its) else (22.0, 3.5)

    # objetivos/acarreos liberados por compañeros fuera
    team_vacated: dict[str, float] = {}
    prof_rows = profiles.to_dicts()
    for r in prof_rows:
        st = injuries.get(r["pid"], "")
        if st in ("Out", "IR", "Doubtful"):
            team_vacated[r["team"]] = team_vacated.get(r["team"], 0.0) + \
                float(r.get("target_share") or 0) + 0.5 * float(r.get("carry_share") or 0)

    out: list[dict] = []
    for r in prof_rows:
        team, pos = r.get("team"), r.get("pos")
        c = ctx.get(team)
        if not c or pos not in C.FANTASY_POSITIONS:
            continue
        mrow = mrows.get(c["opp"])
        if not mrow:
            continue

        chw: dict[str, float] = dict(r.get(f"ch_{fmt}") or {})
        if not chw or sum(chw.values()) <= 0:
            continue

        # los receptores de slot enfrentan defensores distintos: se enrutan a
        # las métricas "vs slot" cuando la fuente los identifica como tales
        mpos = ("SWR" if (C.SLOT_ROUTING and pos == "WR" and r.get("is_slot"))
                else pos)

        contribs: list[tuple[str, float, float, list]] = []
        raw = 0.0
        for ch, w in chw.items():
            if w <= 0.005:
                continue
            zc, detail = channel_weakness(mrow, ch, mpos)
            contribs.append((ch, w, w * zc, detail))
            raw += w * zc

        # ---------------------------------------------------- modificadores
        flags: list[str] = []
        env_z = 0.0
        if c["implied_total"] == c["implied_total"]:
            env_z = (c["implied_total"] - it_mu) / it_sd
        pace_z = _z(mrow, "plays_pg") or 0.0

        script = 0.0
        sp = c["spread_own"]
        if sp is not None:
            if pos == "RB":
                script = np.clip(sp / 10.0, -1.0, 1.0) * chw.get("rush", 0)
            elif pos in ("WR", "TE", "QB"):
                script = np.clip(-sp / 10.0, -1.0, 1.0) * (1 - chw.get("rush", 0))

        vac = min(team_vacated.get(team, 0.0), 0.45)
        if vac > 0.06:
            flags.append(f"Se liberan objetivos: compañeros fuera suman "
                         f"{vac*100:.0f}% del uso del equipo.")

        wx = c.get("weather") or {}
        wx_adj = 0.0
        if wx and not wx.get("dome", True):
            wind = float(wx.get("wind_mph", 0) or 0)
            if wind >= C.WIND_PENALTY_START:
                sev = min((wind - C.WIND_PENALTY_START) /
                          (C.WIND_PENALTY_HARD - C.WIND_PENALTY_START), 1.5)
                wx_adj -= sev * 0.55 * (chw.get("rec_deep", 0) + chw.get("pass", 0))
                wx_adj += sev * 0.15 * chw.get("rush", 0)
                flags.append(f"Alerta de viento: {wind:.0f} mph castiga el juego aéreo "
                             f"profundo y favorece el terrestre.")
            if float(wx.get("precip_mm", 0) or 0) >= 5:
                wx_adj -= 0.12
                flags.append("Se pronostica lluvia.")

        # ---- reporte de lesiones: el hallazgo más grande del backtest
        rec_inj = injuries.get(r["pid"], "")
        if isinstance(rec_inj, dict):
            status = rec_inj.get("status") or ""
            practice = rec_inj.get("practice") or ""
        else:
            status, practice = (rec_inj or ""), ""

        inj_pen = 0.0
        if status in ("Out", "IR"):
            inj_pen = -99.0
            flags.append("FUERA del partido según el reporte oficial.")
        else:
            inj_pen += C.REPORT_PENALTY.get(status, 0.0)
            inj_pen += C.PRACTICE_PENALTY.get(practice, 0.0)
            if status == "Doubtful":
                flags.append("Dudoso: riesgo alto de no jugar.")
            elif status == "Questionable":
                flags.append("Cuestionable: confírmalo antes del kickoff.")
            if practice:
                corta = {"Full Participation in Practice": "practicó completo",
                         "Limited Participation in Practice": "práctica limitada",
                         "Did Not Participate In Practice": "no practicó"}.get(
                             practice, practice)
                flags.append(f"Aparece en el reporte de lesiones ({corta}). "
                             f"Medido en 2025: los jugadores en el reporte rinden "
                             f"0.9 puntos por debajo de su promedio, incluso "
                             f"practicando completo.")

        # ---- confianza según separación (sólo receptores)
        sep = r.get("separation")
        sep_f = separation_factor(sep, pos)
        if (sep is not None and pos in ("WR", "TE")
                and float(sep) < C.SEP_CAUTION):
            flags.append(f"Ojo: separación promedio de {float(sep):.1f} yardas, "
                         f"de las más bajas de la liga. Suele significar que se "
                         f"lleva al mejor esquinero encima, y ahí el promedio "
                         f"defensivo del equipo es menos confiable.")

        # IMPORTANTE: el riesgo por lesión NO entra en el score de matchup.
        # El semáforo responde "¿qué tan bueno es el rival para él?"; la salud
        # es una pregunta distinta y se reporta aparte. Mezclarlas ensuciaba
        # ambas señales (medido: la brecha verde-rojo caía 0.03 al mezclarlas).
        score_raw = (raw * sep_f + W_ENV * env_z + W_PACE * pace_z + W_SCRIPT * script
                     + W_VACATED * vac + wx_adj)

        if inj_pen <= -90:
            risk_level, risk_txt = "fuera", "Fuera del partido"
        elif inj_pen <= -0.95:
            risk_level, risk_txt = "alto", "Riesgo alto"
        elif inj_pen <= -0.40:
            risk_level, risk_txt = "medio", "En el reporte de lesiones"
        elif inj_pen < 0:
            risk_level, risk_txt = "bajo", "Mención en el reporte"
        else:
            risk_level, risk_txt = "ninguno", "Sano"

        out.append({
            "pid": r["pid"], "name": r["name"], "pos": pos, "team": team,
            "opp": c["opp"], "is_home": c["is_home"], "game_id": c["game_id"],
            "gameday": c["gameday"],
            "raw": float(score_raw), "matchup_raw": float(raw),
            "risk_pen": float(max(inj_pen, -3.0)), "risk_level": risk_level,
            "risk_txt": risk_txt,
            "env_z": float(env_z), "pace_z": float(pace_z),
            "implied_total": c["implied_total"], "spread_own": c["spread_own"],
            "status": status or "", "practice": practice or "",
            "separation": (float(sep) if sep is not None else None),
            "sep_factor": round(sep_f, 3), "is_slot": bool(r.get("is_slot")),
            "channels": {k: round(v, 3) for k, v in chw.items()},
            "_contribs": contribs, "_flags": flags,
            "fppg": r.get(f"fppg_{fmt}"), "snap_pct": r.get("snap_pct"),
            "target_share": r.get("target_share"), "carry_share": r.get("carry_share"),
            "wopr": r.get("wopr"), "usage_trend": r.get("usage_trend"),
            "xfp_pg": r.get("xfp_pg"), "fp_over_xfp": r.get("fp_over_xfp"),
            "games": r.get("games"), "opportunities_pg": r.get("opportunities_pg"),
            "confidence": confidence,
        })

    if not out:
        return pl.DataFrame()

    # ------------------------------------------- percentiles dentro de posición
    # Se agrupa en Python puro: las contribuciones son estructuras heterogéneas
    # que no caben en un DataFrame hasta que las convertimos en texto.
    by_pos: dict[str, list[dict]] = {}
    for s in out:
        by_pos.setdefault(s["pos"], []).append(s)

    scored: list[dict] = []
    for pos, sub in by_pos.items():
        vals = np.array([s["raw"] for s in sub], dtype=float)
        pct = _pct_to_score(vals)
        # calidad del jugador (para el ranking de "mejores starts")
        q = np.array([float(s["fppg"] or 0) for s in sub], dtype=float)
        qpct = _pct_to_score(q)
        for i, s in enumerate(sub):
            s["matchup_score"] = round(float(pct[i]), 1)
            s["quality_score"] = round(float(qpct[i]), 1)
            # el ranking de alineación SÍ descuenta el riesgo de lesión
            risk_hit = 0.0 if s["risk_pen"] <= -90 else abs(s["risk_pen"]) * 14.0
            s["start_score"] = round(
                float(max(0.0, 0.55 * qpct[i] + 0.45 * pct[i] - risk_hit)), 1)
            s["light"] = light(s["matchup_score"])
            if s["status"] in ("Out", "IR"):
                s["start_score"] = 0.0
            s["insight"] = build_insight(
                s["name"], s["pos"], s["opp"], s["channels"], s["_contribs"],
                {"implied_total": s["implied_total"], "spread_own": s["spread_own"]},
                s["_flags"], metric_labels)
            s.pop("_contribs", None)
            s.pop("_flags", None)
            scored.append(s)
    return pl.DataFrame(scored, infer_schema_length=None)


def waiver_ranking(scored: pl.DataFrame, trending: dict) -> pl.DataFrame:
    """Ranking de pickups. La bandera clave es 'antes de la manada'."""
    if scored.height == 0:
        return scored
    rows = scored.to_dicts()
    adds = trending.get("add", {}) if isinstance(trending, dict) else {}
    max_add = max(adds.values()) if adds else 0

    for r in rows:
        trend_mkt = 0.0
        sid = r.get("sleeper_id")
        if sid and max_add:
            trend_mkt = min(adds.get(str(sid), 0) / max_add, 1.0)
        r["market_trend"] = round(trend_mkt, 3)

        usage = float(r.get("usage_trend") or 0)
        usage_n = float(np.clip((usage + 0.35) / 0.9, 0, 1))
        matchup_n = float(r.get("matchup_score") or 0) / 100.0

        # oportunidad: xFP subestimado + snaps altos con poca producción
        opp = 0.0
        fox = r.get("fp_over_xfp")
        if fox is not None:
            opp += float(np.clip(-fox, -0.5, 0.6))
        sp = r.get("snap_pct")
        if sp is not None and float(sp) >= 55 and float(r.get("fppg") or 0) < 9:
            opp += 0.25
        opp = float(np.clip(opp, 0, 1))

        r["waiver_score"] = round(100 * (0.35 * matchup_n + 0.35 * usage_n
                                         + 0.20 * opp + 0.10 * trend_mkt), 1)
        r["before_the_herd"] = bool(usage_n >= 0.62 and trend_mkt < 0.18)
    return pl.DataFrame(rows, infer_schema_length=None).sort("waiver_score", descending=True)
