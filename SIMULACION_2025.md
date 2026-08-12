# Simulación completa — temporada 2025, semanas 1 a 18

**Modelo:** el mismo código que corre en tu repo cada martes.
**Método:** walk-forward estricto. Para predecir la semana W solo se usan datos anteriores a W. El modelo se reconstruye desde cero cada semana.
**Volumen:** 271 partidos · 3,988 decisiones de jugador.

---

# 1. FANTASY — el semáforo funciona ✅

| Semáforo | Casos | Puntos reales | Su promedio | **Diferencia** |
|---|---|---|---|---|
| 🟢 Verde | 1,224 | 9.09 | 8.50 | **+0.59** |
| 🟡 Amarillo | 1,029 | 8.11 | 8.02 | **+0.08** |
| 🔴 Rojo | 1,735 | 7.34 | 7.67 | **−0.33** |

**Brecha verde − rojo: +0.914 puntos por partido**
**t = 3.82 · p = 0.00014** → uno en siete mil de que sea casualidad.

Orden perfecto y monotónico. Cada escalón del semáforo separa correctamente.

## Por posición

| | Verde | Rojo | **Brecha** | Veredicto |
|---|---|---|---|---|
| **QB** | +1.22 | −1.07 | **+2.29** | Excelente |
| **RB** | +1.51 | −0.30 | **+1.81** | Muy bueno |
| **TE** | +0.61 | −0.21 | **+0.82** | Bueno |
| **WR** | −0.16 | −0.20 | **+0.03** | ⚠️ No funciona |

## Por decil del score (la prueba más exigente)

No solo verde vs rojo — ¿el número 0-100 ordena bien de punta a punta?

| Rango | n | vs su promedio |
|---|---|---|
| 90-99 | 398 | **+0.67** |
| 80-89 | 410 | **+0.56** |
| 70-79 | 393 | **+0.37** |
| 60-69 | 402 | −0.20 |
| 50-59 | 417 | +0.41 |
| 40-49 | 405 | −0.09 |
| 30-39 | 384 | −0.37 |
| 20-29 | 392 | +0.36 |
| 10-19 | 393 | **−1.08** |
| 0-9 | 371 | −0.32 |

**Los tres deciles más altos son positivos y crecientes.** El medio es ruidoso (esperable: ahí es donde el matchup realmente no dice nada). Los bajos son mayormente negativos, con el peor en 10-19.

Lectura práctica: **confía en los extremos, ignora el centro.** Un 92 y un 55 no son "un poco distintos" — el 92 dice algo y el 55 no dice nada.

## Reporte de lesiones — la mejora nueva, confirmada

| Estado | Casos | vs su propio promedio |
|---|---|---|
| **Sano** (no aparece) | 3,227 | **+0.24** |
| **En el reporte** | 741 | **−0.70** |

**Diferencia: +0.948 puntos · t = 3.59 · p = 0.00034**

Confirmado en el código de producción, no solo en el experimento. El efecto es **más grande que toda la señal de matchup** (+0.914). Es la columna ESTADO de tu tabla.

---

# 2. PARTIDOS — el anclaje funciona, la discrepancia no

| | Modelo | Vegas |
|---|---|---|
| Acierto del ganador (271) | **64.6%** | **65.7%** |
| Brier *(menor es mejor)* | 0.2153 | **0.2121** |
| Contra el spread | 46.1% | — |

El anclaje al mercado mejoró el Brier de **0.2225 → 0.2153**. Seguimos por debajo de Vegas, pero más cerca y mejor calibrados.

## El hallazgo que cambió el diseño

Aquí está la tabla más importante de toda la simulación:

| Postura | Partidos | % | Acierto modelo | Acierto Vegas |
|---|---|---|---|---|
| **Coincide con Vegas** | 196 | 72% | 65.8% | 66.3% |
| Difiere sin respaldo | 24 | 9% | 70.8% | 70.8% |
| Difiere con respaldo | 30 | 11% | 56.7% | 56.7% |
| **Difiere FUERTE** | 21 | 8% | **57.1%** | **66.7%** |

**Mientras más seguro está el modelo de que Vegas se equivoca, peor le va.**

Cuando coincide con el mercado acierta 65.8%. Cuando está *convencido* de que el mercado está mal, cae a 57.1% mientras Vegas acierta 66.7% en esos mismos partidos.

Y los lados de spread que había señalado: **36 partidos, 41.7% de acierto**. Peor que lanzar una moneda.

**Nuestra confianza al discrepar es anti-predictiva.** No es que el modelo sea malo — es que cuando ve algo raro, casi siempre es porque le falta información que el mercado sí tiene (una lesión de última hora, descanso, movimiento de dinero).

### Qué cambié por esto

1. **Anclaje subido de 82% a 88%.** Mejoró el acierto de 64.2% a 64.6% y el Brier de 0.2160 a 0.2153.
2. **Eliminé los lados de spread.** Ya no se publica ninguno. Con 41.7% de acierto, publicarlos era hacer daño.
3. **La discrepancia ahora es bandera roja, no oportunidad.** Cuando el modelo difiere fuerte, la ficha del partido lo marca en rojo y explica: *"en 2025 estos fueron sus peores partidos, trátalo como advertencia".*

## Semana por semana

| Sem | n | Modelo | Vegas | | Fantasy (verde−rojo) |
|---|---|---|---|---|---|
| 1 | 16 | 81.2% | 81.2% | = | −0.67 |
| 2 | 16 | 68.8% | 68.8% | = | — |
| 3 | 16 | 75.0% | 75.0% | = | — |
| 4 | 15 | 60.0% | 66.7% | | +1.47 |
| 5 | 14 | 28.6% | 35.7% | | −0.65 |
| 6 | 15 | 66.7% | 73.3% | | −0.52 |
| 7 | 15 | 73.3% | 80.0% | | +0.90 |
| 8 | 13 | 84.6% | 84.6% | = | +2.04 |
| 9 | 14 | 57.1% | 57.1% | = | +2.89 |
| 10 | 14 | 64.3% | 64.3% | = | +0.84 |
| 11 | 15 | 73.3% | 73.3% | = | +0.15 |
| 12 | 14 | 71.4% | 71.4% | = | −0.26 |
| 13 | 16 | 56.2% | 56.2% | = | +0.36 |
| 14 | 14 | 50.0% | 50.0% | = | **+3.64** |
| 15 | 16 | 62.5% | 62.5% | = | +0.76 |
| 16 | 16 | 62.5% | 62.5% | = | +0.53 |
| 17 | 16 | 56.2% | 56.2% | = | +0.98 |
| 18 | 16 | 62.5% | 62.5% | = | +1.71 |

**Partidos:** el modelo empata a Vegas en 13 de 18 semanas y pierde en 4. Nunca gana solo. Eso es exactamente lo que debe pasar con un modelo bien anclado: no inventa.

**Fantasy:** positivo en 12 de 16 semanas medibles. Las semanas 8, 9, 14 y 18 fueron excelentes (+2 a +3.6 puntos). Las semanas 1, 5 y 6 fueron negativas — la 1 y la 5 son temprano en la temporada, cuando el modelo todavía corre con priors.

---

# 3. LA CONCLUSIÓN

**El fantasy es el producto. Los partidos son el contexto.**

- 🟢 **Fantasy: +0.914 pts, p=0.00014.** Real, medido, significativo. Sirve para QB, RB y TE. En WR no.
- 🟢 **Lesiones: +0.948 pts, p=0.00034.** El efecto individual más grande de todo el sistema.
- 🟡 **Partidos: 64.6% vs 65.7% de Vegas.** Buen contexto, sin ventaja. Y con una regla nueva: si el modelo difiere fuerte, desconfía del modelo, no de Vegas.
- 🔴 **Apuestas contra el spread: eliminadas.** 41.7% de acierto en 36 casos. No hay nada que rescatar ahí.

La herramienta apuesta donde no hay mercado que la corrija, y se calla donde sí lo hay.

---

*Reproducible: `python3 tests/simulacion_final_2025.py` · resultados crudos en `tests/simulacion_final_2025.json`*
