"""Perfil de jugador: uso, eficiencia, tendencia y descomposición por canales.

La pieza clave es `channel_weights`: qué fracción del valor fantasy de un jugador
viene de correr, de recibir corto, de recibir profundo y de la zona roja. Eso es
lo que después se cruza contra la debilidad ESPECÍFICA del rival en ese canal.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from . import config as C

# Valores de referencia de la liga para convertir volumen en puntos esperados
LG = {"ypc": 4.3, "ypt_short": 6.6, "ypt_deep": 9.4,
      "td_per_carry": 0.031, "td_per_short_tgt": 0.034, "td_per_deep_tgt": 0.075,
      "catch_short": 0.72, "catch_deep": 0.44}


def player_game_usage(ev: pl.DataFrame) -> pl.DataFrame:
    """Uso por jugador y partido, derivado del play-by-play."""
    if ev.height == 0:
        return pl.DataFrame()

    rush = (
        ev.filter(pl.col("is_rush"))
        .group_by(["season", "week", "game_id", "off", "def", "rusher"])
        .agg(
            pl.len().alias("carries"),
            pl.col("rush_yds").sum().alias("rush_yds"),
            pl.col("rush_td").sum().alias("rush_td"),
            (pl.when(pl.col("is_rz")).then(1).otherwise(0)).sum().alias("rz_carries"),
        )
        .rename({"rusher": "pid"})
    )

    rec = (
        ev.filter(pl.col("is_target"))
        .group_by(["season", "week", "game_id", "off", "def", "receiver"])
        .agg(
            pl.len().alias("targets"),
            pl.col("comp").sum().alias("receptions"),
            pl.col("rec_yds").sum().alias("rec_yds"),
            pl.col("pass_td").sum().alias("rec_td"),
            pl.col("air").fill_null(0).sum().alias("air_yards"),
            (~pl.col("is_deep_ch")).sum().alias("short_tgt"),
            pl.col("is_deep_ch").sum().alias("deep_tgt"),
            (pl.when(~pl.col("is_deep_ch")).then(pl.col("rec_yds")).otherwise(0.0))
            .sum().alias("short_yds"),
            (pl.when(pl.col("is_deep_ch")).then(pl.col("rec_yds")).otherwise(0.0))
            .sum().alias("deep_yds"),
            (pl.when(pl.col("is_rz")).then(1).otherwise(0)).sum().alias("rz_targets"),
        )
        .rename({"receiver": "pid"})
    )

    # el QB: yardas y TDs de pase (el pbp trae al pasador aparte)
    pas = (
        ev.filter((pl.col("play_type") == "pass") & pl.col("passer").is_not_null())
        .group_by(["season", "week", "game_id", "off", "def", "passer"])
        .agg(
            pl.len().alias("pass_att"),
            (pl.col("rec_yds") * pl.col("comp")).sum().alias("pass_yds"),
            pl.col("pass_td").sum().alias("pass_td_thrown"),
            pl.col("intc").sum().alias("interceptions"),
        )
        .rename({"passer": "pid"})
    )

    pg = (rush.join(rec, on=["season", "week", "game_id", "off", "def", "pid"], how="full",
                    coalesce=True)
              .join(pas, on=["season", "week", "game_id", "off", "def", "pid"], how="full",
                    coalesce=True))
    numeric = [c for c in pg.columns
               if c not in ("season", "week", "game_id", "off", "def", "pid")]
    pg = pg.with_columns([pl.col(c).fill_null(0) for c in numeric])

    # cuotas de equipo (target share, carry share, air yards share)
    team_tot = (
        pg.group_by(["season", "week", "game_id", "off"])
        .agg(pl.col("targets").sum().alias("tm_tgt"),
             pl.col("carries").sum().alias("tm_car"),
             pl.col("air_yards").sum().alias("tm_air"),
             pl.col("rz_targets").sum().alias("tm_rz_tgt"),
             pl.col("rz_carries").sum().alias("tm_rz_car"))
    )
    pg = pg.join(team_tot, on=["season", "week", "game_id", "off"], how="left")
    pg = pg.with_columns(
        (pl.col("targets") / pl.when(pl.col("tm_tgt") > 0).then(pl.col("tm_tgt")).otherwise(None))
        .fill_null(0.0).alias("target_share"),
        (pl.col("carries") / pl.when(pl.col("tm_car") > 0).then(pl.col("tm_car")).otherwise(None))
        .fill_null(0.0).alias("carry_share"),
        (pl.col("air_yards") / pl.when(pl.col("tm_air") > 0).then(pl.col("tm_air")).otherwise(None))
        .fill_null(0.0).alias("air_share"),
        ((pl.col("rz_targets") + pl.col("rz_carries"))
         / pl.when((pl.col("tm_rz_tgt") + pl.col("tm_rz_car")) > 0)
         .then(pl.col("tm_rz_tgt") + pl.col("tm_rz_car")).otherwise(None))
        .fill_null(0.0).alias("rz_share"),
    )
    # WOPR: la métrica estándar de oportunidad (1.5*target share + 0.7*air share)
    pg = pg.with_columns(
        (1.5 * pl.col("target_share") + 0.7 * pl.col("air_share")).alias("wopr")
    )
    return pg


def fantasy_points(row: dict, fmt: str) -> float:
    s = C.SCORING_FORMATS[fmt]
    return (row.get("rush_yds", 0) * s["rush_yd"]
            + row.get("rec_yds", 0) * s["rec_yd"]
            + row.get("receptions", 0) * s["rec"]
            + (row.get("rush_td", 0) + row.get("rec_td", 0)) * s["td"]
            + row.get("pass_yds", 0) * s["pass_yd"]
            + row.get("pass_td_thrown", 0) * s["pass_td"]
            + row.get("interceptions", 0) * s["int"])


def _ewma(vals: list[float], halflife: float = 2.0) -> float:
    if not vals:
        return 0.0
    a = np.array(vals, dtype=float)
    n = len(a)
    w = np.exp(-np.arange(n - 1, -1, -1) / halflife)
    return float((a * w).sum() / w.sum())


def attach_snaps(profiles: pl.DataFrame, snaps: pl.DataFrame,
                 rosters: pl.DataFrame) -> pl.DataFrame:
    """Une snap counts (que vienen con id de PFR) al perfil por gsis_id."""
    if snaps.height == 0 or rosters.height == 0 or profiles.height == 0:
        return profiles.with_columns(pl.lit(None, dtype=pl.Float64).alias("snap_pct"))

    xref = (rosters.filter(pl.col("pfr_id").is_not_null() & pl.col("gsis_id").is_not_null())
            .select(pl.col("pfr_id"), pl.col("gsis_id").alias("pid"))
            .unique(subset=["pfr_id"], keep="first"))

    sn = (snaps.select(
            pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32),
            pl.col("pfr_player_id").alias("pfr_id"),
            # offense_pct viene como fracción (0-1); lo pasamos a porcentaje
            (pl.col("offense_pct").cast(pl.Float64) * 100.0).alias("snap_pct"))
          .filter(pl.col("pfr_id").is_not_null())
          .join(xref, on="pfr_id", how="inner")
          .drop("pfr_id"))

    # snap share reciente: promedio ponderado de las últimas semanas disponibles
    latest = sn["season"].max()
    sn = sn.filter(pl.col("season") == latest)
    recent = (sn.sort(["pid", "week"])
              .group_by("pid")
              .agg(pl.col("snap_pct").tail(4).mean().alias("snap_pct"),
                   pl.col("snap_pct").tail(2).mean().alias("snap_pct_l2"),
                   pl.col("snap_pct").mean().alias("snap_pct_season")))
    return profiles.join(recent, on="pid", how="left")


def attach_separation(prof: pl.DataFrame, ngs: pl.DataFrame,
                      season: int) -> pl.DataFrame:
    """Separación promedio del receptor (Next Gen Stats).

    No es un dato de matchup: es un dato de CONFIANZA. Los receptores que no
    separan son los que se llevan al esquinero estrella encima, y ahí el
    promedio defensivo del equipo miente.
    """
    if prof.height == 0 or ngs is None or ngs.height == 0:
        return prof.with_columns(pl.lit(None, dtype=pl.Float64).alias("separation"))
    idc = next((c for c in ("player_gsis_id", "player_id", "gsis_id")
                if c in ngs.columns), None)
    if idc is None or "avg_separation" not in ngs.columns:
        return prof.with_columns(pl.lit(None, dtype=pl.Float64).alias("separation"))
    d = ngs.with_columns(pl.col("season").cast(pl.Int32, strict=False))
    tgt = season if d.filter(pl.col("season") == season).height else d["season"].max()
    agg = (d.filter(pl.col("season") == tgt)
           .group_by(idc)
           .agg(pl.col("avg_separation").cast(pl.Float64).mean().alias("separation"))
           .rename({idc: "pid"}))
    return prof.join(agg, on="pid", how="left")


def build_profiles(ev: pl.DataFrame, rosters: pl.DataFrame, snaps: pl.DataFrame,
                   ffopp: pl.DataFrame, season: int,
                   current_roster: pl.DataFrame | None = None,
                   ngs: pl.DataFrame | None = None) -> pl.DataFrame:
    """Perfil agregado por jugador para la temporada objetivo (con prior)."""
    pg = player_game_usage(ev)
    if pg.height == 0:
        return pl.DataFrame()

    cur = pg.filter(pl.col("season") == season)
    src, is_prior = (cur, False) if cur.height > 0 else (
        pg.filter(pl.col("season") == pg["season"].max()), True)

    # identidad del jugador
    ident = (rosters
             .filter(pl.col("gsis_id").is_not_null())
             .sort(["season", "week"])
             .group_by("gsis_id")
             .agg(pl.col("full_name").last().alias("name"),
                  pl.col("position").last().alias("pos"),
                  pl.col("team").last().alias("team"),
                  pl.col("sleeper_id").last().alias("sleeper_id"),
                  pl.col("espn_id").last().alias("espn_id"),
                  pl.col("headshot_url").last().alias("headshot"),
                  pl.col("years_exp").last().alias("years_exp"))
             .rename({"gsis_id": "pid"}))

    agg = (src.group_by("pid").agg(
        pl.len().alias("games"),
        pl.col("off").last().alias("team_pbp"),
        pl.col("week").max().alias("last_week"),
        pl.col("carries").sum(), pl.col("rush_yds").sum(), pl.col("rush_td").sum(),
        pl.col("rz_carries").sum(),
        pl.col("targets").sum(), pl.col("receptions").sum(), pl.col("rec_yds").sum(),
        pl.col("rec_td").sum(), pl.col("air_yards").sum(),
        pl.col("short_tgt").sum(), pl.col("deep_tgt").sum(),
        pl.col("pass_att").sum(), pl.col("pass_yds").sum(),
        pl.col("pass_td_thrown").sum(), pl.col("interceptions").sum(),
        pl.col("short_yds").sum(), pl.col("deep_yds").sum(), pl.col("rz_targets").sum(),
        pl.col("target_share").mean().alias("target_share"),
        pl.col("carry_share").mean().alias("carry_share"),
        pl.col("air_share").mean().alias("air_share"),
        pl.col("rz_share").mean().alias("rz_share"),
        pl.col("wopr").mean().alias("wopr"),
        # series para la tendencia
        pl.col("target_share").sort_by("week").alias("_ts_series"),
        pl.col("carry_share").sort_by("week").alias("_cs_series"),
        pl.col("week").sort().alias("_weeks"),
    ))

    agg = agg.join(ident, on="pid", how="left")
    agg = agg.with_columns(
        pl.coalesce([pl.col("team"), pl.col("team_pbp")]).alias("team"),
        pl.col("name").fill_null("(desconocido)"),
        pl.col("pos").fill_null("OTHER"),
    ).drop("team_pbp")

    # Equipo VIGENTE del año objetivo. En pretemporada esto es lo que corrige
    # las altas de agencia libre; además descarta a quien ya no está en la liga.
    if current_roster is not None and current_roster.height:
        cur_map = (current_roster
                   .filter(pl.col("gsis_id").is_not_null())
                   .select(pl.col("gsis_id").alias("pid"),
                           pl.col("team").alias("team_now"),
                           pl.col("position").alias("pos_now"))
                   .unique(subset=["pid"], keep="first"))
        agg = agg.join(cur_map, on="pid", how="left")
        n_before = agg.height
        agg = agg.filter(pl.col("team_now").is_not_null())
        agg = agg.with_columns(pl.col("team_now").alias("team"),
                               pl.coalesce([pl.col("pos_now"), pl.col("pos")]).alias("pos")
                               ).drop(["team_now", "pos_now"])
        print(f"  roster {season}: {agg.height} activos "
              f"({n_before - agg.height} descartados por no estar en la liga)")

    # marca de receptor de slot (viene del roster, fuente Next Gen Stats)
    if "ngs_position" in rosters.columns:
        slot = (rosters.filter(pl.col("ngs_position") == "SLOT_WR")
                .select(pl.col("gsis_id").alias("pid")).unique()
                .with_columns(pl.lit(True).alias("is_slot")))
        agg = agg.join(slot, on="pid", how="left").with_columns(
            pl.col("is_slot").fill_null(False))
    else:
        agg = agg.with_columns(pl.lit(False).alias("is_slot"))

    agg = agg.filter(pl.col("pos").is_in(C.FANTASY_POSITIONS))
    agg = attach_snaps(agg, snaps, rosters)

    # ------------------------------------------------------- tendencia de uso
    rows = agg.to_dicts()
    for r in rows:
        ts = [float(x) for x in (r.get("_ts_series") or [])]
        cs = [float(x) for x in (r.get("_cs_series") or [])]
        use = [t + c for t, c in zip(ts + [0] * len(cs), cs + [0] * len(ts))][:max(len(ts), len(cs))]
        if not use:
            use = ts or cs
        recent = _ewma(use[-4:], halflife=1.6)
        base = float(np.mean(use)) if use else 0.0
        r["usage_recent"] = recent
        r["usage_base"] = base
        r["usage_trend"] = (recent - base) / (base + 0.02)

        # ---------------------------------------- canales de producción
        car = float(r.get("carries") or 0)
        st = float(r.get("short_tgt") or 0)
        dt = float(r.get("deep_tgt") or 0)
        rzt = float(r.get("rz_targets") or 0) + float(r.get("rz_carries") or 0)
        g = max(1.0, float(r.get("games") or 1))

        for fmt in C.SCORING_FORMATS:
            s = C.SCORING_FORMATS[fmt]
            v_rush = car * (LG["ypc"] * s["rush_yd"] + LG["td_per_carry"] * s["td"])
            v_short = st * (LG["ypt_short"] * s["rec_yd"]
                            + LG["catch_short"] * s["rec"]
                            + LG["td_per_short_tgt"] * s["td"])
            v_deep = dt * (LG["ypt_deep"] * s["rec_yd"]
                           + LG["catch_deep"] * s["rec"]
                           + LG["td_per_deep_tgt"] * s["td"])
            v_pass = (float(r.get("pass_yds") or 0) * s["pass_yd"]
                      + float(r.get("pass_td_thrown") or 0) * s["pass_td"]) / max(1.0, g) * g
            v_rz = rzt * 0.9 * s["td"] * 0.14
            tot = v_rush + v_short + v_deep + v_rz + v_pass
            if tot <= 0:
                w = {"rush": 0.0, "rec_short": 0.0, "rec_deep": 0.0,
                     "redzone": 0.0, "pass": 0.0}
            else:
                w = {"rush": v_rush / tot, "rec_short": v_short / tot,
                     "rec_deep": v_deep / tot, "redzone": v_rz / tot,
                     "pass": v_pass / tot}
            r[f"ch_{fmt}"] = w
            r[f"fp_{fmt}"] = fantasy_points(r, fmt)
            r[f"fppg_{fmt}"] = r[f"fp_{fmt}"] / g

        r["opportunities_pg"] = (car + st + dt) / g
        r["is_prior_only"] = is_prior
        for k in ("_ts_series", "_cs_series", "_weeks"):
            r.pop(k, None)

    prof = pl.DataFrame(rows, infer_schema_length=None)
    prof = attach_expected_points(prof, ffopp, season)
    prof = attach_separation(prof, ngs, season)
    return prof


def attach_expected_points(prof: pl.DataFrame, ffopp: pl.DataFrame,
                           season: int) -> pl.DataFrame:
    """Puntos fantasy esperados (xFP) desde ff_opportunity.

    Separa suerte de uso real: muy por encima del xFP = corrección a la baja;
    muy por debajo = candidato de compra.
    """
    if prof.height == 0 or ffopp.height == 0:
        return prof.with_columns(pl.lit(None, dtype=pl.Float64).alias("xfp_pg"),
                                 pl.lit(None, dtype=pl.Float64).alias("fp_over_xfp"))
    cols = ffopp.columns
    exp_col = next((c for c in ("total_fantasy_points_exp", "total_fantasy_points_expected")
                    if c in cols), None)
    act_col = next((c for c in ("total_fantasy_points", "total_fantasy_points_actual")
                    if c in cols), None)
    if not exp_col or "player_id" not in cols:
        return prof.with_columns(pl.lit(None, dtype=pl.Float64).alias("xfp_pg"),
                                 pl.lit(None, dtype=pl.Float64).alias("fp_over_xfp"))

    ffopp = ffopp.with_columns(pl.col("season").cast(pl.Int32, strict=False))
    tgt = season if ffopp.filter(pl.col("season") == season).height else ffopp["season"].max()
    f = (ffopp.filter(pl.col("season") == tgt)
         .group_by("player_id")
         .agg(pl.col(exp_col).cast(pl.Float64).sum().alias("_xfp"),
              (pl.col(act_col).cast(pl.Float64).sum().alias("_fp")
               if act_col else pl.lit(None).alias("_fp")),
              pl.len().alias("_g"))
         .rename({"player_id": "pid"}))
    out = prof.join(f, on="pid", how="left")
    out = out.with_columns(
        (pl.col("_xfp") / pl.when(pl.col("_g") > 0).then(pl.col("_g")).otherwise(None))
        .alias("xfp_pg"),
        ((pl.col("_fp") - pl.col("_xfp"))
         / pl.when(pl.col("_xfp").abs() > 1).then(pl.col("_xfp").abs()).otherwise(None))
        .alias("fp_over_xfp"),
    ).drop(["_xfp", "_fp", "_g"])
    return out
