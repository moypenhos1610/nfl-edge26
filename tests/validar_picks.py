"""Valida las reglas de picks (spread / moneyline / upset) con historia real.

    python -m tests.validar_picks

Usa EXACTAMENTE las mismas funciones que producción (pipeline/picks.py).
Separa dos épocas independientes (2006-2015 y 2016-2025) para ver si cada
señal se REPLICA, y deja 2026 aparte como prueba fuera de muestra.
Escribe pipeline/picks_hist.json, que la página muestra como "histórico".
"""
from __future__ import annotations

import json
import sys

import nflreadpy as nfl
import polars as pl

sys.path.insert(0, ".")
from pipeline import picks as P  # noqa: E402

FIRST, LAST = 2006, 2025


def main(season_live: int = 2026) -> dict:
    s = nfl.load_schedules(list(range(FIRST, season_live + 1)))
    s = s.filter(pl.col("game_type") == "REG")
    forms = P.ats_forms(s)
    done = s.filter(pl.col("home_score").is_not_null() & pl.col("spread_line").is_not_null())

    eras = {"2006-2015": (2006, 2015), "2016-2025": (2016, 2025),
            f"{season_live}": (season_live, season_live)}
    tiers = ["fuerte", "media", "leve", "moneda"]
    agg = {e: {t: [0, 0, 0] for t in tiers} for e in eras}         # gana, pierde, push
    agg_all = {t: [0, 0, 0] for t in tiers}
    ups = {e: {"n": 0, "cover": 0, "win": 0, "exp_win": 0.0} for e in eras}
    mlb = [{"lo": .5, "hi": .6}, {"lo": .6, "hi": .7}, {"lo": .7, "hi": .8}, {"lo": .8, "hi": 1.01}]
    for b in mlb:
        b.update({"w": 0, "n": 0})
    per_week_live: dict[int, list[int]] = {}

    for g in done.iter_rows(named=True):
        era = next((k for k, (a, b) in eras.items() if a <= g["season"] <= b), None)
        if era is None or g["spread_line"] == 0:
            continue
        pk = P.spread_pick(g, forms.get(g["game_id"]), g["week"])
        res = g["home_score"] - g["away_score"]
        cm = res - g["spread_line"]
        side_home = pk["side"] == g["home_team"]
        out = 2 if cm == 0 else (0 if (cm > 0) == side_home else 1)
        agg[era][pk["tier"]][out] += 1
        if g["season"] <= LAST:
            agg_all[pk["tier"]][out] += 1
        if g["season"] == season_live and out != 2:
            wk = per_week_live.setdefault(g["week"], [0, 0])
            wk[out] += 1

        ph, pa = P.american_to_prob(g.get("home_moneyline")), P.american_to_prob(g.get("away_moneyline"))
        p_home = ph / (ph + pa) if ph and pa else 1 / (1 + 10 ** (-g["spread_line"] / 13.5))
        if res != 0 and g["season"] <= LAST:
            p = max(p_home, 1 - p_home)
            hit = (p_home >= .5) == (res > 0)
            for b in mlb:
                if b["lo"] <= p < b["hi"]:
                    b["n"] += 1
                    b["w"] += int(hit)
        al = P.upset_alert(g, pk, p_home)
        if al and cm != 0:
            u = ups[era]
            u["n"] += 1
            u["cover"] += int(out == 0)
            u["win"] += int((res > 0) == side_home)
            u["exp_win"] += al["p_win"]

    def pct(x):
        w, l, _ = x
        return round(100 * w / (w + l), 1) if (w + l) else None

    print("\nSPREAD por nivel de confianza (gana-pierde-push, % sin push)")
    print(f"{'nivel':8s}" + "".join(f"{e:>24s}" for e in eras))
    for t in tiers:
        print(f"{t:8s}" + "".join(f"{pct(agg[e][t]) or 0:>9}% ({agg[e][t][0]}-{agg[e][t][1]}-{agg[e][t][2]})"
                                  .rjust(24) for e in eras))
    print("\nALERTA DE UPSET (el perro cubre / gana directo)")
    for e, u in ups.items():
        if u["n"]:
            print(f"  {e}: n={u['n']}  cubre {100*u['cover']/u['n']:.1f}%  "
                  f"gana directo {100*u['win']/u['n']:.1f}% (mercado esperaba {100*u['exp_win']/u['n']:.1f}%)")
    print("\nMONEYLINE (pick = el más probable) por probabilidad")
    for b in mlb:
        print(f"  {int(b['lo']*100)}-{min(int(b['hi']*100),100)}%: {100*b['w']/max(b['n'],1):.1f}% n={b['n']}")
    print(f"\n{season_live} por semana (spread, todos los partidos):",
          {w: f"{v[0]}-{v[1]}" for w, v in sorted(per_week_live.items())})

    hist = {
        "seasons": f"{FIRST}-{LAST}",
        "spread_tiers": {t: {"pct": pct(agg_all[t]), "n": sum(agg_all[t][:2]),
                             "by_era": {e: pct(agg[e][t]) for e in eras}} for t in tiers},
        "upset": {e: ({"n": u["n"], "cover": round(100 * u["cover"] / u["n"], 1),
                       "win": round(100 * u["win"] / u["n"], 1),
                       "exp_win": round(100 * u["exp_win"] / u["n"], 1)} if u["n"] else None)
                  for e, u in ups.items()},
        "ml_buckets": [{"lo": b["lo"], "hi": b["hi"], "n": b["n"],
                        "pct": round(100 * b["w"] / b["n"], 1) if b["n"] else None} for b in mlb],
        "breakeven": 52.4,
    }
    with open(P.HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    print(f"\n-> {P.HIST_PATH}")
    return hist


if __name__ == "__main__":
    main()
