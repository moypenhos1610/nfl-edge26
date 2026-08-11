# NFL EDGE 2026

Herramienta gratuita y automática para decidir **start/sit**, detectar **waivers**
y predecir **resultados de partidos** de la NFL, cruzando el uso real de cada
jugador contra la debilidad específica de la defensa rival.

**Dashboard:** https://moypenhos1610.github.io/nfl-edge26/

## Qué hace distinto

- **Cruce por canales.** Cada jugador se descompone en canales de producción
  (tierra, recepción corta, recepción profunda, zona roja, pase) según su uso
  real, y cada canal se evalúa contra la permisividad del rival *en ese canal*.
  Por eso detecta ventajas no obvias: un RB puede tener matchup malo por tierra
  y bueno por el aire contra la misma defensa.
- **Rankings defensivos ajustados por rival.** Una defensa que enfrentó a las
  mejores ofensivas se ve peor de lo que es. Se resuelve
  `valor = media + efecto_defensa + efecto_ofensa` con regresión ridge.
- **Arranque en frío explícito.** Antes de la semana 5 los rankings se apoyan en
  un prior de 2025/2024 regresado a la media, con encogimiento bayesiano
  `n/(n+K)` y un badge visible de confianza de datos.
- **Backtest honesto publicado.** El modelo de partidos se valida walk-forward
  sobre 2018-2025 y el dashboard muestra su accuracy real comparada con el
  mercado de apuestas, incluyendo dónde *no* tiene ventaja.

## Fuentes (todas gratuitas, sin llave de API)

| Fuente | Qué aporta |
|---|---|
| [nflverse](https://github.com/nflverse/nflreadpy) vía `nflreadpy` | play-by-play, stats semanales, snap counts, rosters, lesiones, calendario y **líneas de Vegas** |
| [Sleeper API](https://docs.sleeper.com/) | altas/bajas en tendencia de 24h (señal de mercado) |
| ESPN API pública | designaciones de lesión casi en tiempo real |
| [Open-Meteo](https://open-meteo.com/) | viento y lluvia por estadio |

> `nfl_data_py` está **deprecado y archivado** desde septiembre de 2025. Este
> proyecto usa `nflreadpy`, su sucesor oficial.

## Automatización

GitHub Actions corre el pipeline y publica los resultados solo:

| Cron (UTC) | Hora CDMX | Para qué |
|---|---|---|
| `0 13 * * 2` | martes 7:00 am | corrida principal — ya incluye el Monday Night Football |
| `0 13 * * 5` | viernes 7:00 am | correcciones de estadística de la NFL + reporte final de lesiones |
| `0 15 * * 0` | domingo 9:00 am | inactivos previos al kickoff |

También hay botón manual: pestaña **Actions → Actualizar NFL EDGE → Run workflow**.

## Correr localmente

```bash
pip install -r requirements.txt
python -m pipeline.run                # semana automática
python -m pipeline.run --season 2025 --week 12
python -m http.server 8000 -d docs    # abre http://localhost:8000
```

## Estructura

```
pipeline/
  config.py    parámetros, formatos de scoring, estadios
  ingest.py    descarga de todas las fuentes (degrada si alguna cae)
  defense.py   matriz de permisividad ajustada por rival (ridge)
  players.py   uso, tendencia, xFP y descomposición por canales
  matchup.py   el cruce, el score 0-100, el semáforo y el insight en español
  games.py     Elo + EPA ajustada, calibración y comparación vs Vegas
  run.py       orquestador -> docs/data/week_N.json
docs/
  index.html   dashboard (un solo archivo, sin dependencias)
  data/        JSON por semana, generado por el pipeline
```

## Aviso

Herramienta informativa construida con datos públicos. Las predicciones de
partidos son estimaciones estadísticas, no consejo financiero.
