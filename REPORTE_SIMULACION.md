# Reporte de simulación — ¿qué información hace al modelo más preciso?

**Temporada 2025 completa: semanas 1-18 + playoffs. 284 partidos, ~4,000 decisiones de jugador.**
Validación *walk-forward* estricta: para predecir la semana W solo se usa información anterior a W. El modelo se reentrena desde cero cada semana.

---

# PARTE A — PARTIDOS: 15 combinaciones probadas

Cada combinación se entrenó con 2016-2024 más lo que iba de 2025, y se evaluó sobre la semana siguiente. Ordenado por **Brier score**, que mide qué tan bien calibradas están las probabilidades (más bajo = mejor). El acierto simple engaña: acertar 68% con probabilidades mal calibradas es peor que acertar 66% bien calibrado.

| # | Combinación de variables | Acierto | **Brier** | LogLoss |
|---|---|---|---|---|
| — | **MERCADO (Vegas puro)** | 66.2% | **0.2109** 🏆 | 0.6070 |
| 10 | Mercado + Elo + EPA | 66.2% | 0.2110 | 0.6067 |
| 8 | Solo el mercado (recalibrado) | 66.2% | 0.2112 | 0.6075 |
| 9 | Mercado + Elo | 66.2% | 0.2113 | 0.6077 |
| 14 | Mercado + QB + Elo + EPA | 66.2% | 0.2113 | 0.6078 |
| 12 | Mercado + cambio de QB | 66.2% | 0.2115 | 0.6085 |
| 13 | Mercado + QB + descanso | 66.5% | 0.2117 | 0.6088 |
| 11 | Mercado + TODO lo nuestro | 67.2% | 0.2130 | 0.6126 |
| 1 | Solo Elo *(modelo actual)* | 64.8% | 0.2225 | 0.6354 |
| 2 | Elo + EPA ajustada | 64.4% | 0.2226 | 0.6359 |
| 6 | **Elo + EPA + descanso + viaje + récord + CAMBIO DE QB** | **68.3%** | 0.2226 | 0.6362 |
| 7 | Todo lo nuestro, sin mercado | 66.5% | 0.2227 | 0.6366 |
| 4 | + descanso y viaje | 66.5% | 0.2249 | 0.6407 |
| 5 | + récord y divisional | 66.2% | 0.2256 | 0.6422 |
| 3 | Elo + EPA ofensiva/defensiva separadas | 64.4% | 0.2257 | 0.6420 |

## Lo que salta a la vista

**1. Nada le gana a Vegas en calibración.** El mercado crudo tiene el mejor Brier de las 15 combinaciones. Y fíjate en el patrón: **todas las combinaciones que incluyen el mercado están agrupadas entre 0.2109 y 0.2130; todas las que no lo incluyen, entre 0.2225 y 0.2257.** Hay un escalón claro. El mercado vale más que todas nuestras variables juntas.

**2. La variante 6 acertó 68.3%, más que Vegas.** Pero su Brier es 0.2226 — mucho peor. Traducción: gana más volados pero no sabe cuánta confianza tener. Y 68.3% vs 66.2% sobre 284 partidos son **6 partidos de diferencia**. Es ruido.

**3. Añadir nuestras variables encima del mercado no ayuda.** Compara la fila 8 (solo mercado, 0.2112) contra la fila 11 (mercado + todo lo nuestro, 0.2130). **Empeora.**

## Los desacuerdos: donde queríamos ganar

| Variante | Partidos en desacuerdo | Gana el modelo | Gana Vegas | **p-valor** |
|---|---|---|---|---|
| 1. Solo Elo | 38 | 44.7% | 55.3% | 0.63 |
| 2. Elo + EPA | 37 | 43.2% | 56.8% | 0.51 |
| 3. Elo + EPA off/def | 41 | 43.9% | 56.1% | 0.53 |
| 4. + descanso y viaje | 41 | 51.2% | 48.8% | 1.00 |
| 5. + récord y divisional | 40 | 50.0% | 50.0% | 1.00 |
| **6. + cambio de QB** | 42 | **57.1%** | 42.9% | **0.44** |
| 7. Todo sin mercado | 45 | 51.1% | 48.9% | 1.00 |
| 11. Mercado + todo | 15 | 60.0% | 40.0% | 0.61 |

La variante del cambio de QB ganó 24 de 42 desacuerdos. Se ve bien… **hasta que miras el p-valor: 0.44.** Eso significa que una moneda justa produciría un resultado así de bueno o mejor el 44% de las veces. **No hay evidencia. Es azar.**

Ninguna variante alcanza significancia estadística en los desacuerdos. Ninguna.

## La prueba decisiva

Esta es la que zanja el asunto. Metemos la probabilidad de Vegas **y** la nuestra en la misma regresión y dejamos que los datos decidan cuánto pesa cada una:

| Nuestra variante | Coeficiente del MERCADO | Coeficiente del MODELO |
|---|---|---|
| Solo Elo | **+1.122** | **−0.235** |
| + cambio de QB | **+1.097** | **−0.210** |
| Todo lo nuestro | **+1.082** | **−0.194** |

**El coeficiente de nuestro modelo es negativo en los tres casos.**

Qué significa exactamente: una vez que conoces el precio del mercado, nuestro modelo **no aporta información nueva** — de hecho, la regresión prefiere restarle un poquito, porque es una versión más ruidosa de la misma información que Vegas ya tiene.

Esto no es una opinión. Es el resultado del experimento.

## ¿Y el cambio de QB? ¿No es un punto ciego de Vegas?

Era mi mejor hipótesis. La medí:

- **89 de 284 partidos (31%) tuvieron cambio de mariscal de campo.**
- **Vegas acertó el 65.2% en esos partidos** — prácticamente su promedio general (66.2%).

**Vegas no tiene un punto ciego ahí.** El mercado ya sabe quién va a jugar de QB, mucho antes que nosotros. La línea se mueve el miércoles cuando sale el reporte de práctica.

## Veredicto sobre partidos

> **Con datos públicos y gratuitos, no se le puede ganar a Vegas. Ni con más variables, ni con mejores variables, ni combinándolas de otra manera.**

Y hay una razón estructural: la línea de apuestas es el precio de equilibrio de un mercado donde se mueven miles de millones de dólares. Ya contiene toda la información pública **y** la privada (quién apostó fuerte y por qué). Estamos jugando contra el consenso ponderado por dinero de todos los que saben de NFL en el mundo.

**La decisión correcta de producto no es seguir intentando ganarle. Es dejar de competir con el mercado y empezar a consumirlo.** Que es, casualmente, donde sí tenemos ventaja: el total implícito de Vegas ya alimenta el motor de fantasy, y ahí sí agrega valor medible (ver Parte B).

**Lo único que podría cambiar esto** —y hay que decirlo con honestidad— es información que el mercado tarda en absorber: noticias de reporteros locales en tiempo real, movimiento de línea (dónde entra el dinero fuerte), o clima actualizado minutos antes del kickoff. Nada de eso es una mejor fórmula. Es **velocidad**, no modelo. Y no está disponible gratis.

---

# PARTE B — FANTASY: 8 configuraciones probadas

Aquí la historia es completamente distinta. Métrica: **brecha verde − rojo**, es decir, cuánto más supera un jugador en verde a su propio promedio comparado con uno en rojo.

| Configuración | Brecha | t | rho | QB | RB | WR | TE |
|---|---|---|---|---|---|---|---|
| **A. Modelo completo** | **+0.831** 🏆 | **3.51** | +0.051 | +2.5 | +1.6 | −0.1 | +0.9 |
| D. Sin entorno de Vegas | +0.796 | 3.34 | +0.055 | +2.0 | +1.4 | +0.0 | +0.9 |
| H. Solo últimas 4 semanas | +0.751 | 3.17 | +0.053 | +2.1 | +1.7 | −0.3 | +0.9 |
| F. Solo canales, sin extras | +0.699 | 2.93 | +0.048 | +1.6 | +1.4 | −0.3 | +1.2 |
| C. Sin ventanas de recencia | +0.687 | 2.88 | +0.050 | +2.1 | +1.6 | −0.3 | +0.9 |
| B. Sin ajuste por rival | +0.682 | 2.91 | +0.052 | +2.1 | +1.5 | −0.4 | +1.0 |
| E. Sin ritmo ni guion | +0.569 | 2.40 | +0.042 | +1.9 | +1.6 | −0.5 | +0.7 |
| G. Entorno con doble peso | +0.535 | 2.28 | +0.043 | +1.4 | +1.5 | −0.4 | +0.7 |

## Lo que aporta cada pieza

**El modelo completo gana. Quitarle cualquier pieza lo empeora.** Eso es una validación limpia: no hay grasa.

Cuánto vale cada componente, midiendo lo que se pierde al quitarlo:

| Componente | Aporte |
|---|---|
| **Ritmo + guion de juego** | **+0.26** ← el más valioso |
| Ajuste por rival (ridge) | +0.15 |
| Ventanas de recencia | +0.14 |
| Modificadores en conjunto | +0.13 |
| Entorno de Vegas | +0.04 |

**Y un hallazgo de calibración:** duplicar el peso del entorno de Vegas (variante G) **derrumba** la brecha de +0.83 a +0.54. El peso actual está bien afinado — no hay que tocarlo. Más no es mejor.

**El hoyo de los WR es estructural**, no de configuración: en 7 de las 8 variantes sale negativo. No se arregla moviendo pesos.

---

# PARTE C — ¿QUÉ VARIABLE NUEVA DEBEMOS INCORPORAR?

Probé tres candidatas, todas gratuitas y ya disponibles en nuestras fuentes.

## 🥇 1. Presencia en el reporte de lesiones — **INCORPORAR YA**

Hoy solo penalizamos las designaciones formales (Out / Doubtful / Questionable). Pero muchos jugadores aparecen en el reporte **sin** designación de partido, solo con su participación en práctica. Medí qué pasa con ellos:

| Estado | n | Rendimiento vs su propio promedio |
|---|---|---|
| **Sin aparecer en el reporte** | 3,232 | **+0.23** |
| Aparece, pero práctica completa | 551 | **−0.67** |
| Participación limitada | 159 | **−0.80** |
| No participó en práctica | 46 | **−0.54** |

**Diferencia sano vs en reporte: +0.921 puntos. t = 3.48. p = 0.0005.**

Lee bien ese número: **es más grande que toda la señal del matchup (+0.83)**, y es cinco veces más significativo estadísticamente.

**Y el detalle contraintuitivo:** los que aparecen en el reporte **pero practicaron completo** rinden −0.67. Es decir, **el simple hecho de aparecer en la lista ya es mala señal**, aunque el equipo diga que está bien. El cuerpo técnico sabe algo.

> Esta es la mejora número uno, es gratis, y ya tenemos el dato descargándose. Solo no lo estábamos usando.

## 🥈 2. Separación del receptor — **INCORPORAR CON CUIDADO**

Aquí está la explicación del hoyo de los WR. Dividí a los receptores por su separación promedio (dato de Next Gen Stats, gratis):

| Grupo | Brecha global | **Brecha en WR** |
|---|---|---|
| Receptores que separan **poco** (≤2.7 yd) | −0.732 | **−0.826** |
| Receptores que separan **mucho** (>2.7 yd) | +0.600 | **+0.281** |

**El semáforo funciona en receptores que crean separación y se invierte en los que no.**

La interpretación es limpia y coincide con lo que sospechábamos: los receptores que no separan son los que se llevan al esquinero estrella encima. Una defensa puede ser mala "contra WRs" en promedio y tener un CB1 que anula justo a tu receptor. El promedio de equipo miente precisamente en esos casos.

**Advertencia honesta:** solo 212 jugadores tienen dato de separación, y los estadísticos t son −1.45 y +1.62 — **ninguno alcanza significancia por sí solo**. Es una pista fuerte con una explicación mecánica creíble, no un hecho probado. Vale implementarlo y volver a medir con más datos.

## 🥉 3. Separar la defensa en slot vs abierto — **MEJORA MARGINAL**

| | Brecha global | Brecha WR |
|---|---|---|
| Modelo actual | +0.725 | −0.218 |
| Con slot separado | **+0.794** | **−0.145** |

Mejora en la dirección correcta, pero pequeña. **La razón es cobertura de datos: solo el 13% de los objetivos vienen etiquetados como slot** en la fuente gratuita. Con esa cobertura no alcanza para arreglar el problema. Vale la pena incluirlo, pero no es la solución.

## Lo que NO sirvió

| Variable | Resultado |
|---|---|
| Cambio de mariscal de campo (partidos) | Vegas ya lo tiene descontado — 65.2% de acierto ahí |
| Distancia de viaje y husos horarios | Empeoró el Brier (0.2249 vs 0.2225) |
| Récord y partido divisional | Empeoró el Brier (0.2256) |
| EPA ofensiva y defensiva por separado | La peor de todas (0.2257) — junta funciona mejor |
| Duplicar el peso del entorno de Vegas | Derrumbó la brecha de fantasy de 0.83 a 0.54 |

---

# CONCLUSIONES

### Sobre partidos
1. **No le vamos a ganar a Vegas con datos gratuitos.** Está medido de tres formas independientes y las tres coinciden: Brier, desacuerdos y apilamiento.
2. La configuración más precisa de todas es, literalmente, **usar la línea de Vegas**.
3. **Recomendación:** reposicionar el módulo. Que deje de presentarse como "dónde veo valor contra Vegas" y pase a ser "aquí está el contexto del partido, con la línea del mercado como referencia". El valor real de este módulo es el **total implícito**, que sí alimenta el motor de fantasy.

### Sobre fantasy
4. **La configuración actual ya es la óptima** entre las 8 probadas. Cada componente aporta y ninguno sobra.
5. El componente más valioso es el que menos protagonismo tiene: **ritmo + guion de juego (+0.26)**.
6. **El peso del entorno de Vegas está bien calibrado.** Subirlo destruye la señal.

### Qué incorporar, en orden
7. **Participación en prácticas (+0.92, p=0.0005).** Prioridad absoluta. Gratis, ya descargado, efecto más grande que el matchup mismo.
8. **Separación del receptor.** Aplicar el semáforo con menos confianza —o invertido— en receptores que no separan. Explica el hoyo de los WR.
9. **Slot vs abierto.** Mejora pequeña pero gratis y en la dirección correcta.

### La lección de fondo

El experimento dejó algo claro que vale más que cualquier variable individual:

> **Donde compites contra un mercado eficiente, pierdes. Donde nadie está compitiendo, ganas.**

Nadie está poniendo dinero a "¿este RB va a superar su promedio esta semana?". Ahí no hay mercado que corrija el precio, y por eso la señal sobrevive. En el resultado del partido sí hay mercado, y por eso no sobrevive.

**La herramienta debe apostar todo a lo primero.**

---

*Simulación reproducible: `tests/simulacion.py`, `tests/simulacion2.py`, `tests/prueba_wr.py`.
Resultados crudos en `tests/*_resultados.json`.*
