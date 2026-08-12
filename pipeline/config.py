"""Configuración central de NFL EDGE 2026."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- temporada
SEASON = int(os.environ.get("NFL_EDGE_SEASON", 2026))
PRIOR_SEASONS = [SEASON - 1, SEASON - 2]          # 2025 (peso alto), 2024 (peso bajo)
PRIOR_WEIGHTS = {SEASON - 1: 0.70, SEASON - 2: 0.30}

# Backtest / calibración del modelo de partidos
CALIBRATION_SEASONS = list(range(2015, SEASON))

LOCAL_TZ = ZoneInfo("America/Mexico_City")

# ------------------------------------------------- encogimiento (cold start)
# peso_2026 = n / (n + K).  K más alto = confiar más tiempo en el prior.
K_DEFENSE = 4.0
K_PLAYER = 5.0

# Ventanas de recencia para la matriz defensiva (semanas)
WINDOWS = {"season": None, "l4": 4, "l2": 2}
WINDOW_BLEND = {"season": 0.45, "l4": 0.35, "l2": 0.20}

# ------------------------------------------------------------------ scoring
SCORING_FORMATS = {
    "ppr":      {"rec": 1.0, "rec_yd": 0.1, "rush_yd": 0.1, "td": 6.0, "pass_yd": 0.04,
                 "pass_td": 4.0, "int": -2.0, "fumble_lost": -2.0},
    "half_ppr": {"rec": 0.5, "rec_yd": 0.1, "rush_yd": 0.1, "td": 6.0, "pass_yd": 0.04,
                 "pass_td": 4.0, "int": -2.0, "fumble_lost": -2.0},
    "standard": {"rec": 0.0, "rec_yd": 0.1, "rush_yd": 0.1, "td": 6.0, "pass_yd": 0.04,
                 "pass_td": 4.0, "int": -2.0, "fumble_lost": -2.0},
}
DEFAULT_FORMAT = "half_ppr"

FANTASY_POSITIONS = ["QB", "RB", "WR", "TE"]

# --------------------------------------------------------------- semáforo
LIGHT_GREEN = 70.0
LIGHT_YELLOW = 45.0

# ------------------------------------------------------ canales de producción
# Cada jugador se descompone en estos canales según su uso real.
CHANNELS = ["rush", "rec_short", "rec_deep", "redzone"]
CHANNEL_AIR_SPLIT = 10.0       # <10 yardas aéreas = corto; >=10 = profundo
DEEP_AIR_YARDS = 20.0          # "bomba" real, métrica aparte en la matriz
REDZONE_YARDLINE = 20          # yardline_100 <= 20

# Cuánto del rendimiento defensivo del año pasado se conserva al siguiente.
# La correlación año-a-año de EPA defensivo ronda 0.4-0.5: el resto es ruido,
# rotación de roster y regresión a la media. No inflamos esto.
PRIOR_CARRYOVER = 0.55

# ------------------------------------------------------------------ ridge
RIDGE_LAMBDA = 12.0            # regularización del ajuste ofensa/defensa

# ------------------------------------------------------------------ estadios
# lat/lon para el clima (Open-Meteo). Domos marcados -> se ignora el clima.
STADIUMS = {
    "ARI": (33.5277, -112.2626, True),   "ATL": (33.7554, -84.4008, True),
    "BAL": (39.2780, -76.6227, False),   "BUF": (42.7738, -78.7870, False),
    "CAR": (35.2258, -80.8528, False),   "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0955, -84.5161, False),   "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True),    "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True),    "GB":  (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True),    "IND": (39.7601, -86.1639, True),
    "JAX": (30.3239, -81.6373, False),   "KC":  (39.0489, -94.4839, False),
    "LA":  (33.9535, -118.3392, True),   "LAC": (33.9535, -118.3392, True),
    "LV":  (36.0909, -115.1833, True),   "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9736, -93.2575, True),    "NE":  (42.0909, -71.2643, False),
    "NO":  (29.9511, -90.0812, True),    "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False),   "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False),   "SEA": (47.5952, -122.3316, False),
    "SF":  (37.4030, -121.9700, False),  "TB":  (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False),   "WAS": (38.9077, -76.8645, False),
}

# Viento a partir del cual castigamos el juego aéreo (mph)
WIND_PENALTY_START = 12.0
WIND_PENALTY_HARD = 20.0

# ---------------------------------------------- reporte de lesiones (medido)
# Hallazgo del backtest 2025: aparecer en el reporte de lesiones cuesta 0.92
# puntos fantasy vs el propio promedio (t=3.48, p=0.0005) INCLUSO cuando el
# jugador practicó completo. El efecto es mayor que toda la señal de matchup.
PRACTICE_PENALTY = {
    "Full Participation in Practice":    -0.45,
    "Limited Participation in Practice": -0.62,
    "Did Not Participate In Practice":   -0.78,
}
REPORT_PENALTY = {"Questionable": -0.30, "Doubtful": -1.20}

# ------------------------------------------- confianza en receptores (medido)
# HIPÓTESIS PROBADA Y DESCARTADA. La observación era real: en receptores que
# separan poco la señal se invertía (-0.83 vs +0.28). Pero al aplicarlo como
# encogimiento sobre TODA la población, el resultado EMPEORA:
#     base +0.914 · sólo separación +0.867 · sólo slot +0.767 · ambas +0.795
# Los t-stat originales (-1.45 y +1.62) nunca alcanzaron significancia: era
# ruido con una explicación bonita. Se dejan apagados y el dato se muestra
# solamente como contexto para el usuario, sin tocar el score.
SEP_LOW, SEP_HIGH = 2.55, 3.00      # yardas de separación promedio (NGS)
SEP_FACTOR_MIN = 1.0                # 1.0 = sin encogimiento (probado, no ayuda)
SEP_CAUTION = 2.60                  # por debajo de esto sólo se AVISA
SLOT_ROUTING = False                # enrutar slot a métricas propias: no ayuda

# ------------------------------------------------- anclaje al mercado (medido)
# 15 combinaciones probadas: ninguna le gana a Vegas. Coeficiente de nuestro
# modelo junto al del mercado: -0.21 (negativo). Conclusión: anclarse al
# mercado y separarse sólo cuando la evidencia propia sea abrumadora.
MARKET_ANCHOR = 0.88        # 88% mercado, 12% modelo (subido tras medir 2025)
DISAGREE_MIN_PTS = 4.0      # puntos de diferencia mínimos para siquiera hablar
DISAGREE_STRONG_PTS = 6.5   # a partir de aquí la discrepancia es "fuerte"
ATS_MIN_EDGE = 5.0          # no se marca lado del spread por debajo de esto
CONSENSUS_REQUIRED = 2      # señales propias que deben coincidir para discrepar

# MEDIDO sobre la temporada 2025 completa con el modelo ya anclado:
#   coincide con Vegas (72% de los partidos): 65.8% de acierto
#   discrepa con respaldo (11%):              53.3%  vs Vegas 56.7%
#   discrepa FUERTE con respaldo (8%):        57.1%  vs Vegas 66.7%
#   lados de spread señalados (36 casos):     41.7%
# O sea: mientras MÁS seguro está el modelo de que Vegas se equivoca, PEOR le
# va. Nuestra confianza al discrepar es anti-predictiva. Por eso no se publica
# un lado de spread como recomendación: la discrepancia se muestra sólo como
# aviso de "aquí el modelo y el mercado no se entienden, y el mercado suele
# tener razón".
SHOW_ATS_PICK = False

# ------------------------------------------------------------------- rutas
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".cache")
DOCS_DIR = os.path.join(ROOT, "docs")
DATA_DIR = os.path.join(DOCS_DIR, "data")

for _d in (CACHE_DIR, DATA_DIR):
    os.makedirs(_d, exist_ok=True)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


def stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M CDMX")
