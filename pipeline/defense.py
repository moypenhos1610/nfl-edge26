"""Matriz de permisividad defensiva, ajustada por calidad del rival.

Este es el corazón de la herramienta. La idea:

1. Desde el play-by-play, etiquetar cada jugada con la POSICIÓN del jugador que
   tocó el balón (uniendo con el roster de esa semana). Sin esto no se puede
   responder "¿cuántas yardas por recepción le permite esta defensa a los RB?".

2. Agregar por (defensa, partido) en ~25 categorías.

3. AJUSTAR POR RIVAL. Una defensa que enfrentó a Baltimore y Detroit se ve peor
   de lo que es. Resolvemos  y = mu + efecto_defensa + efecto_ofensa  con ridge,
   lo que separa el mérito de la defensa del mérito del rival. Casi ninguna
   herramienta gratuita hace esto, y por eso sus rankings mienten.

4. Rankear 1-32 por categoría (1 = defensa más dura, 32 = más permisiva) en tres
   ventanas de recencia, y mezclarlas.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from . import config as C

# nombre -> (columna de valor, columna de peso/oportunidad, "más alto es más permisivo")
# El peso se usa para las tasas (yardas por acarreo) y para saber cuánta
# información aporta cada partido.


def _positions(rosters: pl.DataFrame) -> pl.DataFrame:
    """Mapa (season, week, gsis_id) -> posición simplificada."""
    if rosters.height == 0:
        return pl.DataFrame(schema={"season": pl.Int32, "week": pl.Int32,
                                    "pid": pl.Utf8, "pos": pl.Utf8})
    pos = (
        rosters
        .filter(pl.col("gsis_id").is_not_null())
        .select(
            pl.col("season").cast(pl.Int32),
            pl.col("week").cast(pl.Int32),
            pl.col("gsis_id").alias("pid"),
            pl.col("position").alias("pos_raw"),
            pl.col("ngs_position").alias("ngs"),
        )
        .with_columns(
            pl.when(pl.col("pos_raw").is_in(["RB", "FB", "HB"])).then(pl.lit("RB"))
            .when((pl.col("pos_raw") == "WR") & (pl.col("ngs") == "SLOT_WR"))
            .then(pl.lit("SWR"))
            .when(pl.col("pos_raw") == "WR").then(pl.lit("WR"))
            .when(pl.col("pos_raw") == "TE").then(pl.lit("TE"))
            .when(pl.col("pos_raw") == "QB").then(pl.lit("QB"))
            .otherwise(pl.lit("OTHER")).alias("pos")
        )
        .drop(["pos_raw", "ngs"])
        .unique(subset=["season", "week", "pid"], keep="first")
    )
    return pos


def build_play_events(pbp: pl.DataFrame, rosters: pl.DataFrame) -> pl.DataFrame:
    """Play-by-play filtrado y etiquetado con la posición del jugador con balón."""
    if pbp.height == 0:
        return pl.DataFrame()

    pos = _positions(rosters)

    base = (
        pbp
        .filter(
            (pl.col("season_type") == "REG")
            & pl.col("defteam").is_not_null()
            & pl.col("posteam").is_not_null()
            & (pl.col("play_type").is_in(["run", "pass"]))
            & (pl.col("two_point_attempt").fill_null(0) == 0)
        )
        .select(
            pl.col("game_id"), pl.col("season").cast(pl.Int32),
            pl.col("week").cast(pl.Int32),
            pl.col("posteam").alias("off"), pl.col("defteam").alias("def"),
            pl.col("play_type"),
            pl.col("rusher_player_id").alias("rusher"),
            pl.col("receiver_player_id").alias("receiver"),
            pl.col("passer_player_id").alias("passer"),
            pl.col("rushing_yards").fill_null(0.0).alias("rush_yds"),
            pl.col("receiving_yards").fill_null(0.0).alias("rec_yds"),
            pl.col("yards_after_catch").fill_null(0.0).alias("yac"),
            pl.col("air_yards").alias("air"),
            pl.col("complete_pass").fill_null(0).alias("comp"),
            pl.col("epa"),
            pl.col("touchdown").fill_null(0).alias("td"),
            pl.col("pass_touchdown").fill_null(0).alias("pass_td"),
            pl.col("rush_touchdown").fill_null(0).alias("rush_td"),
            pl.col("yardline_100"),
            pl.col("sack").fill_null(0),
            pl.col("qb_hit").fill_null(0),
            pl.col("qb_dropback").fill_null(0),
            pl.col("interception").fill_null(0).alias("intc"),
        )
    )

    # posición del corredor y del receptor
    base = (
        base
        .join(pos.rename({"pid": "rusher", "pos": "rush_pos"}),
              on=["season", "week", "rusher"], how="left")
        .join(pos.rename({"pid": "receiver", "pos": "rec_pos"}),
              on=["season", "week", "receiver"], how="left")
        .with_columns(
            pl.col("rush_pos").fill_null("OTHER"),
            pl.col("rec_pos").fill_null("OTHER"),
        )
    )

    base = base.with_columns(
        # un target = intento de pase con receptor identificado (excluye sacks)
        (pl.col("receiver").is_not_null() & (pl.col("sack") == 0)).alias("is_target"),
        # una corrida = jugada de tierra con corredor identificado (incluye scrambles)
        ((pl.col("play_type") == "run") & pl.col("rusher").is_not_null()).alias("is_rush"),
        (pl.col("air").fill_null(0) >= C.CHANNEL_AIR_SPLIT).alias("is_deep_ch"),
        (pl.col("air").fill_null(0) >= C.DEEP_AIR_YARDS).alias("is_bomb"),
        (pl.col("yardline_100").fill_null(50) <= C.REDZONE_YARDLINE).alias("is_rz"),
    )
    return base


# --------------------------------------------------------------- agregación
def _game_level(ev: pl.DataFrame) -> pl.DataFrame:
    """Suma por (defensa, partido) los numeradores y denominadores de cada métrica."""
    def s(expr) -> pl.Expr:
        return expr.sum()

    g = ev.group_by(["season", "week", "game_id", "def", "off"]).agg([
        # ---- corrida
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "RB"))
          .then(pl.col("rush_yds")).otherwise(0.0)).alias("n_rb_rush_yds"),
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "RB"))
          .then(1).otherwise(0)).alias("d_rb_carries"),
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "RB") & (pl.col("rush_td") == 1))
          .then(1).otherwise(0)).alias("n_rb_rush_td"),
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "RB") & (pl.col("rush_yds") >= 10))
          .then(1).otherwise(0)).alias("n_rb_expl_rush"),
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "QB"))
          .then(pl.col("rush_yds")).otherwise(0.0)).alias("n_qb_rush_yds"),
        s(pl.when(pl.col("is_rush") & (pl.col("rush_pos") == "QB"))
          .then(1).otherwise(0)).alias("d_qb_carries"),
        s(pl.when(pl.col("is_rush")).then(pl.col("epa")).otherwise(None)).alias("n_rush_epa"),
        s(pl.when(pl.col("is_rush")).then(1).otherwise(0)).alias("d_rush_plays"),

        # ---- recepción por posición
        *[
            e for pos in ("RB", "WR", "TE", "SWR") for e in (
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos))
                  .then(1).otherwise(0)).alias(f"d_{pos.lower()}_tgt"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & (pl.col("comp") == 1))
                  .then(1).otherwise(0)).alias(f"d_{pos.lower()}_rec"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos))
                  .then(pl.col("rec_yds")).otherwise(0.0)).alias(f"n_{pos.lower()}_rec_yds"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos))
                  .then(pl.col("yac")).otherwise(0.0)).alias(f"n_{pos.lower()}_yac"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & (pl.col("pass_td") == 1))
                  .then(1).otherwise(0)).alias(f"n_{pos.lower()}_rec_td"),
                # corto / profundo
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & ~pl.col("is_deep_ch"))
                  .then(pl.col("rec_yds")).otherwise(0.0)).alias(f"n_{pos.lower()}_short_yds"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & ~pl.col("is_deep_ch"))
                  .then(1).otherwise(0)).alias(f"d_{pos.lower()}_short_tgt"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & pl.col("is_deep_ch"))
                  .then(pl.col("rec_yds")).otherwise(0.0)).alias(f"n_{pos.lower()}_deep_yds"),
                s(pl.when(pl.col("is_target") & (pl.col("rec_pos") == pos) & pl.col("is_deep_ch"))
                  .then(1).otherwise(0)).alias(f"d_{pos.lower()}_deep_tgt"),
            )
        ],

        # ---- contexto / defensa global
        s(pl.when(pl.col("qb_dropback") == 1).then(pl.col("epa")).otherwise(None)).alias("n_pass_epa"),
        s(pl.when(pl.col("qb_dropback") == 1).then(1).otherwise(0)).alias("d_dropbacks"),
        s(pl.col("sack")).alias("n_sacks"),
        s(pl.col("qb_hit")).alias("n_qb_hits"),
        s(pl.col("intc")).alias("n_int"),
        pl.len().alias("d_plays"),
        s(pl.when(pl.col("is_rz")).then(pl.col("td")).otherwise(0)).alias("n_rz_td"),
        s(pl.when(pl.col("is_rz")).then(1).otherwise(0)).alias("d_rz_plays"),
        s(pl.when(pl.col("is_bomb") & (pl.col("comp") == 1))
          .then(pl.col("rec_yds")).otherwise(0.0)).alias("n_bomb_yds"),
        s(pl.when(pl.col("is_bomb")).then(1).otherwise(0)).alias("d_bomb_tgt"),
    ])
    return g


# Definición de las métricas de la matriz.
# (clave, numerador, denominador o None para "por partido", etiqueta en español)
METRICS: list[tuple[str, str, str | None, str]] = [
    ("rb_rush_ypc",    "n_rb_rush_yds",  "d_rb_carries",     "yardas por acarreo a RB"),
    ("rb_rush_ypg",    "n_rb_rush_yds",  None,               "yardas por tierra a RB por juego"),
    ("rb_rush_td_pg",  "n_rb_rush_td",   None,               "TDs de tierra a RB por juego"),
    ("rb_expl_pg",     "n_rb_expl_rush", None,               "carreras explosivas de RB por juego"),
    ("qb_rush_ypg",    "n_qb_rush_yds",  None,               "yardas por corrida a QB por juego"),
    ("qb_rush_ypc",    "n_qb_rush_yds",  "d_qb_carries",     "yardas por acarreo a QB"),
    ("rush_epa",       "n_rush_epa",     "d_rush_plays",     "EPA por corrida permitido"),

    ("rb_rec_ypt",     "n_rb_rec_yds",   "d_rb_tgt",         "yardas por objetivo a RB"),
    ("rb_rec_ypr",     "n_rb_rec_yds",   "d_rb_rec",         "yardas por recepción a RB"),
    ("rb_tgt_pg",      "d_rb_tgt",       None,               "objetivos a RB por juego"),
    ("rb_rec_ypg",     "n_rb_rec_yds",   None,               "yardas aéreas a RB por juego"),
    ("rb_rec_td_pg",   "n_rb_rec_td",    None,               "TDs aéreos a RB por juego"),

    ("wr_rec_ypt",     "n_wr_rec_yds",   "d_wr_tgt",         "yardas por objetivo a WR"),
    ("wr_tgt_pg",      "d_wr_tgt",       None,               "objetivos a WR por juego"),
    ("wr_rec_ypg",     "n_wr_rec_yds",   None,               "yardas a WR por juego"),
    ("wr_short_ypt",   "n_wr_short_yds", "d_wr_short_tgt",   "yardas a WR en ruta corta"),
    ("wr_deep_ypt",    "n_wr_deep_yds",  "d_wr_deep_tgt",    "yardas a WR en ruta profunda"),
    ("wr_rec_td_pg",   "n_wr_rec_td",    None,               "TDs a WR por juego"),
    ("wr_yac_pr",      "n_wr_yac",       "d_wr_rec",         "YAC por recepción a WR"),

    ("te_rec_ypt",     "n_te_rec_yds",   "d_te_tgt",         "yardas por objetivo a TE"),
    ("te_tgt_pg",      "d_te_tgt",       None,               "objetivos a TE por juego"),
    ("te_rec_ypg",     "n_te_rec_yds",   None,               "yardas a TE por juego"),
    ("te_rec_td_pg",   "n_te_rec_td",    None,               "TDs a TE por juego"),
    ("te_short_ypt",   "n_te_short_yds", "d_te_short_tgt",   "yardas a TE en ruta corta"),

    # receptores de slot (cobertura parcial en la fuente, se usa como refuerzo)
    ("swr_rec_ypt",    "n_swr_rec_yds",  "d_swr_tgt",        "yardas por objetivo a WR de slot"),
    ("swr_tgt_pg",     "d_swr_tgt",      None,               "objetivos a WR de slot por juego"),

    ("pass_epa",       "n_pass_epa",     "d_dropbacks",      "EPA por intento de pase permitido"),
    ("bomb_ypt",       "n_bomb_yds",     "d_bomb_tgt",       "yardas en pases de 20+ aéreas"),
    ("rz_td_rate",     "n_rz_td",        "d_rz_plays",       "TDs por jugada en zona roja"),
    ("plays_pg",       "d_plays",        None,               "jugadas permitidas por juego (ritmo)"),
]

# Métricas donde MÁS ALTO = defensa MÁS DURA (hay que invertir el signo).
INVERTED = {"n_sacks", "n_qb_hits", "n_int"}


def _ridge_adjust(df: pl.DataFrame, value_col: str, weight_col: str | None) -> dict[str, float]:
    """Separa efecto_defensa de efecto_ofensa con mínimos cuadrados regularizados.

    Devuelve {equipo: valor ajustado} = lo que esa defensa permitiría contra una
    ofensa promedio de la liga.
    """
    d = df.filter(pl.col(value_col).is_not_null())
    if weight_col:
        d = d.filter(pl.col(weight_col) > 0)
    if d.height < 12:
        return {}

    defs = sorted(d["def"].unique().to_list())
    offs = sorted(d["off"].unique().to_list())
    di = {t: i for i, t in enumerate(defs)}
    oi = {t: i for i, t in enumerate(offs)}

    y = d[value_col].to_numpy().astype(float)
    w = (d[weight_col].to_numpy().astype(float) if weight_col
         else np.ones(len(y)))
    w = np.clip(w, 0.0, None)
    if w.sum() <= 0:
        return {}
    # recencia: los partidos más recientes pesan un poco más
    wk = d["week"].to_numpy().astype(float)
    sn = d["season"].to_numpy().astype(float)
    age = (sn.max() - sn) * 18 + (wk.max() - wk)
    w = w * np.exp(-age / 26.0)

    n = len(y)
    p = 1 + len(defs) + len(offs)
    X = np.zeros((n, p))
    X[:, 0] = 1.0
    for r, (dt, ot) in enumerate(zip(d["def"].to_list(), d["off"].to_list())):
        X[r, 1 + di[dt]] = 1.0
        X[r, 1 + len(defs) + oi[ot]] = 1.0

    W = np.sqrt(w)
    Xw = X * W[:, None]
    yw = y * W

    reg = np.eye(p) * C.RIDGE_LAMBDA
    reg[0, 0] = 0.0  # no penalizar el intercepto
    try:
        beta = np.linalg.solve(Xw.T @ Xw + reg, Xw.T @ yw)
    except np.linalg.LinAlgError:
        return {}

    mu = beta[0]
    # centrar los efectos para que representen "vs ofensa promedio"
    def_eff = beta[1:1 + len(defs)]
    def_eff = def_eff - def_eff.mean()
    return {t: float(mu + def_eff[di[t]]) for t in defs}


def _raw_metric(g: pl.DataFrame, num: str, den: str | None) -> pl.DataFrame:
    """Valor por partido de una métrica (tasa o conteo por juego)."""
    if den is None:
        return g.with_columns(pl.col(num).cast(pl.Float64).alias("_v"),
                              pl.lit(1.0).alias("_w"))
    return (g.with_columns(
        pl.when(pl.col(den) > 0)
        .then(pl.col(num).cast(pl.Float64) / pl.col(den).cast(pl.Float64))
        .otherwise(None).alias("_v"),
        pl.col(den).cast(pl.Float64).alias("_w"),
    ))


def build_matrix(games: pl.DataFrame, weeks_back: int | None = None) -> pl.DataFrame:
    """Matriz ajustada por rival, para una ventana de recencia."""
    if games.height == 0:
        return pl.DataFrame()
    g = games
    if weeks_back:
        maxs = g["season"].max()
        gs = g.filter(pl.col("season") == maxs)
        if gs.height:
            cut = gs["week"].max() - weeks_back + 1
            g = gs.filter(pl.col("week") >= cut)
        if g.height < 12:
            g = games

    rows: dict[str, dict[str, float]] = {}
    for key, num, den, _label in METRICS:
        if num not in g.columns or (den and den not in g.columns):
            continue
        gm = _raw_metric(g, num, den)
        adj = _ridge_adjust(gm, "_v", "_w" if den else None)
        for team, val in adj.items():
            rows.setdefault(team, {})[key] = val

    if not rows:
        return pl.DataFrame()
    recs = [{"team": t, **vals} for t, vals in sorted(rows.items())]
    return pl.DataFrame(recs)


def blend_with_prior(current: pl.DataFrame, prior: pl.DataFrame,
                     games_played: int) -> pl.DataFrame:
    """Encogimiento bayesiano empírico entre la temporada actual y el prior.

    peso_actual = n / (n + K).  Con n=0 (antes de Week 1) el resultado es 100%
    prior, ya regresado hacia la media por PRIOR_CARRYOVER.
    """
    if prior.height:
        pcols = [c for c in prior.columns if c != "team"]
        prior = prior.with_columns([
            (pl.col(c).mean() + C.PRIOR_CARRYOVER * (pl.col(c) - pl.col(c).mean())).alias(c)
            for c in pcols
        ])
    if current.height == 0:
        return prior
    if prior.height == 0:
        return current

    w = games_played / (games_played + C.K_DEFENSE)
    cols = [c for c in current.columns if c != "team"]
    merged = current.join(prior, on="team", how="full", suffix="_prior", coalesce=True)
    exprs = []
    for c in cols:
        pc = f"{c}_prior"
        if pc in merged.columns:
            exprs.append(
                (pl.coalesce([pl.col(c), pl.col(pc)]) * w
                 + pl.coalesce([pl.col(pc), pl.col(c)]) * (1 - w)).alias(c)
            )
        else:
            exprs.append(pl.col(c))
    return merged.with_columns(exprs).select(["team"] + cols)


def rank_and_z(matrix: pl.DataFrame) -> pl.DataFrame:
    """Añade z-score y ranking 1-32 por categoría.

    Convención: rank 1 = defensa MÁS DURA, rank 32 = MÁS PERMISIVA.
    z positivo = más permisiva = mejor matchup para el atacante.
    """
    if matrix.height == 0:
        return matrix
    cols = [c for c in matrix.columns if c != "team"]
    out = matrix
    for c in cols:
        mean = out[c].mean()
        std = out[c].std()
        std = std if (std and std > 1e-9) else 1.0
        out = out.with_columns(
            ((pl.col(c) - mean) / std).alias(f"z_{c}"),
            pl.col(c).rank(method="ordinal").cast(pl.Int32).alias(f"rank_{c}"),
        )
    return out


def build(pbp: pl.DataFrame, rosters: pl.DataFrame, season: int) -> dict:
    """Pipeline completo de la matriz defensiva, con priors y ventanas."""
    ev = build_play_events(pbp, rosters)
    if ev.height == 0:
        return {"matrix": pl.DataFrame(), "events": pl.DataFrame(),
                "game_level": pl.DataFrame(), "games_played": 0, "confidence": "baja"}

    gl = _game_level(ev)
    cur = gl.filter(pl.col("season") == season)
    pri = gl.filter(pl.col("season") < season)

    games_played = 0
    if cur.height:
        games_played = int(cur.select(pl.col("week").n_unique()).item())

    # ventanas de recencia sobre la temporada actual
    parts: list[tuple[float, pl.DataFrame]] = []
    for name, wb in C.WINDOWS.items():
        m = build_matrix(cur, wb) if cur.height else pl.DataFrame()
        if m.height:
            parts.append((C.WINDOW_BLEND[name], m))

    if parts:
        total_w = sum(w for w, _ in parts)
        acc = parts[0][1].select("team")
        cols = [c for c in parts[0][1].columns if c != "team"]
        exprs = []
        for c in cols:
            terms = []
            for w, m in parts:
                if c in m.columns:
                    acc = acc.join(m.select(["team", pl.col(c).alias(f"{c}__{id(m)}")]),
                                   on="team", how="left")
                    terms.append(pl.col(f"{c}__{id(m)}").fill_null(0.0) * (w / total_w))
            if terms:
                exprs.append(sum(terms[1:], terms[0]).alias(c))
        current = acc.with_columns(exprs).select(["team"] + cols)
    else:
        current = pl.DataFrame()

    prior = build_matrix(pri) if pri.height else pl.DataFrame()
    blended = blend_with_prior(current, prior, games_played)
    ranked = rank_and_z(blended)

    conf = "alta" if games_played >= 8 else ("media" if games_played >= 4 else "baja")
    return {"matrix": ranked, "events": ev, "game_level": gl,
            "games_played": games_played, "confidence": conf}


METRIC_LABELS = {k: lab for k, _n, _d, lab in METRICS}
