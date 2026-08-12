# NFL EDGE — qué juego matemático estamos jugando

---

## La idea, en una línea

> Todo el mundo pregunta *"¿esta defensa es buena o mala?"*.
> Nosotros preguntamos *"¿en qué exactamente es mala, y mi jugador vive exactamente de eso?"*

---

## Primero, la confesión: NO estamos prediciendo el futuro

Esto es lo más importante que hay que entender, y casi nadie lo dice.

Un modelo de fantasy no adivina cuántos puntos va a hacer un jugador. **Eso es imposible** — un TD de suerte de 60 yardas vale 6 puntos y no lo predice nadie.

Lo que hacemos es otra cosa, más humilde y más útil:

> **Quitarle el sesgo a una medición sucia.**

Cuando lees "Denver permite 180 yardas aéreas por partido", ese número está **contaminado**. Contaminado por a quién enfrentó, por si iba ganando o perdiendo, por el ritmo del partido, por si llovió. El número que ves no es la habilidad de Denver: es la habilidad de Denver *más* un montón de ruido.

Todo nuestro trabajo es **separar la señal del ruido de esa medición**. Después, la decisión es fácil.

Es la diferencia entre un adivino y un topógrafo. No somos adivinos.

---

## Las cuatro piezas de matemáticas (y de dónde salió cada una)

### 🏆 1. Elo — sí, el del ajedrez

**Arpad Elo**, profesor de física húngaro-americano y ajedrecista, diseñó en los años 50 un sistema para rankear jugadores de ajedrez. La federación de EE.UU. lo adoptó en 1960.

La idea es de una elegancia brutal: **cada jugador tiene un número; ganarle a alguien mejor te sube mucho, ganarle a alguien peor te sube poco.** El sistema se autocorrige solo, partido a partido.

Nosotros lo aplicamos a los 32 equipos, alimentado con todos los partidos desde 2010, con dos ajustes:

- **Margen de victoria:** ganar 40-3 sube más que ganar 20-17. Con un amortiguador logarítmico, porque la diferencia entre ganar por 3 y por 10 importa mucho más que entre ganar por 40 y por 47.
- **Regresión entre temporadas:** cada equipo regresa un tercio hacia la media en el offseason. Los rosters cambian; el rating tiene que olvidar un poco.

**Lo bonito:** el mismo sistema que dice si Magnus Carlsen es mejor que Hikaru Nakamura dice si Baltimore es mejor que Cincinnati. Es literalmente el mismo álgebra.

---

### ⚖️ 2. La regresión ridge — el corazón de todo

Aquí está la pieza que separa esta herramienta de un ranking de internet.

**El problema:** una defensa que enfrentó a Baltimore, Detroit y Buffalo se ve peor de lo que es. Otra que enfrentó a Carolina, Cleveland y Tennessee se ve mejor. Los rankings crudos **mienten sistemáticamente**.

**La solución** es plantear cada resultado como una suma de culpas:

```
yardas_permitidas  =  promedio_liga  +  efecto_defensa  +  efecto_ofensa
```

Con 272 partidos y 64 incógnitas (32 defensas + 32 ofensas), el sistema tiene solución. Le preguntas a los datos: *"de todo lo que pasó, ¿cuánto fue mérito de la defensa y cuánto del rival?"* — y el álgebra las separa.

**De dónde viene:** es un modelo de **efectos fijos de dos vías**, resuelto con **regularización de Tikhonov** (1943), rebautizada *ridge regression* por Hoerl y Kennard en 1970. La misma matemática se usa para separar el efecto de un maestro del efecto de sus alumnos en investigación educativa, y para el *adjusted plus-minus* de la NBA — medir cuánto aporta un jugador descontando con quién estaba en la cancha.

El término de regularización (la "cresta" del nombre) evita que una defensa que solo jugó dos partidos salga con un rating disparatado. **Castiga la confianza excesiva.** Es matemáticamente honesto por diseño.

**Cuánto vale:** medido. Quitarlo baja la señal de fantasy de +0.83 a +0.68 puntos por partido.

---

### 🎯 3. El producto punto — el cruce

Esta es la más simple y la más original.

A cada jugador lo convertimos en un **vector de cómo produce**:

```
McCaffrey  = [ tierra 0.45 · recepción corta 0.35 · profunda 0.03 · zona roja 0.18 ]
Josh Allen = [ pase 0.76 · tierra 0.18 · zona roja 0.06 ]
```

A cada defensa la convertimos en un **vector de por dónde sangra**, con los efectos ya limpios del paso anterior:

```
Denver = [ tierra −0.4 · rec. corta a RB +1.9 · profunda −1.1 · zona roja +0.3 ]
```

Y el score del matchup es, literalmente, **el producto punto de los dos vectores**:

```
score = Σ (peso_del_canal × debilidad_del_rival_en_ese_canal)
```

Eso es todo. Multiplicar y sumar.

**Y ahí sale la magia:** un corredor puede tener el canal de tierra en rojo y el de recepción en verde **contra la misma defensa**. El promedio los cancelaría y te diría "matchup neutro". El producto punto **respeta de dónde viene el valor de ese jugador en particular** y te dice: *búscalo por el aire, no por tierra*.

Es geometría, no estadística. Estás midiendo si dos flechas apuntan en la misma dirección.

---

### 🎲 4. Encogimiento bayesiano — la humildad, formalizada

**El problema:** hoy es agosto. No existe un solo dato de 2026. En la semana 3 tendremos dos partidos por equipo. ¿Cuánto le creemos a dos partidos?

**La respuesta:** un promedio ponderado que se mueve solo.

```
peso_de_2026  =  n / (n + K)
```

Con n = 0 partidos, confías 100% en el año pasado. Con n = 4 y K = 4, mitad y mitad. Para la semana 8, casi todo es 2026. **La confianza crece sola con la evidencia. No la ajusta nadie a mano.**

**De dónde viene — y esta historia es preciosa:** es el estimador de **James-Stein**, y su demostración más famosa la publicaron **Efron y Morris en 1975 usando promedios de bateo de béisbol**. Demostraron algo que parecía imposible: si quieres predecir el promedio final de temporada de 18 bateadores, **encoger todos sus promedios hacia la media de la liga predice mejor que usar sus promedios reales**. Aunque suene absurdo. Aunque cada estimación individual empeore.

Se conoce como la **paradoja de Stein**, y rompió cabezas en los años 50 porque contradice la intuición de todos.

Nosotros lo usamos exactamente igual: un rating defensivo de dos partidos se encoge hacia el promedio de la liga, y del año anterior solo conservamos el **55%** — porque la correlación real año contra año del EPA defensivo ronda 0.45. El resto es ruido, agencia libre y regresión a la media. **No lo inflamos.**

---

## Un detalle que vale la pena presumir

El **EPA** (puntos esperados añadidos), la métrica que usamos para medir cada jugada, tiene un origen que casi nadie conoce:

Lo inventó **Virgil Carter**, mariscal de campo de los Cincinnati Bengals, junto con el profesor Robert Machol. Publicaron *"Operations Research on Football"* en la revista **Operations Research** en **1971** — mientras Carter todavía era jugador activo de la NFL.

Un quarterback en activo publicando investigación de operaciones. Fue el primer intento serio de responder "¿cuánto vale realmente esta jugada?" en lugar de contar yardas. **Cincuenta y cinco años después, sigue siendo la base de todo el análisis moderno de la NFL** — y de este modelo.

---

## Qué revisa, cada martes a las 7 de la mañana

Sin que nadie toque nada:

| | |
|---|---|
| **~50,000** | jugadas del play-by-play, cada una etiquetada con la posición de quien tocó el balón |
| **32 × 28** | defensas × categorías, todas ajustadas por calidad del rival |
| **~500** | jugadores perfilados: snaps, target share, WOPR, tendencia, puntos esperados |
| **3** | ventanas de tiempo (temporada, últimas 4, últimas 2) mezcladas |
| **3** | formatos de puntuación calculados en paralelo |
| **16** | partidos con línea de Vegas, clima por estadio y reporte de lesiones |
| **< 2 min** | de cómputo total, costo cero |

---

## Lo que probamos — y lo que fracasó

Un modelo sin backtest es una opinión con decimales. Corrimos la temporada 2025 completa, semana por semana, sin dejar que el modelo viera un solo dato del futuro.

### ✅ El semáforo de fantasy funciona

Comparando a cada jugador **contra sí mismo** (sus puntos de esa semana menos su propio promedio), sobre 3,988 casos:

| | Rendimiento vs su propio promedio |
|---|---|
| 🟢 Verde | **+0.54** |
| 🟡 Amarillo | +0.07 |
| 🔴 Rojo | **−0.29** |

Orden perfecto. Brecha de **+0.83 puntos**, con **t = 3.51** — no es azar.

Funciona en **QB (+2.5), RB (+1.6) y TE (+0.9)**.
**No funciona en WR (−0.1)**, y ya sabemos por qué: los receptores que no crean separación se llevan al esquinero estrella encima, y el promedio de equipo miente justo ahí.

### ❌ Y no le ganamos a Vegas

Probamos **15 combinaciones de variables**. Todas perdieron.

- Vegas: 66.2% de acierto, Brier 0.2109. **El mejor de las 15.**
- Cuando discrepamos, Vegas tiene razón más seguido — y ninguna variante alcanza significancia estadística.
- **La prueba decisiva:** metimos la probabilidad de Vegas y la nuestra en la misma regresión. Coeficiente del mercado **+1.10**. Coeficiente nuestro: **−0.21**. Negativo.

Traducción sin anestesia: **una vez que conoces el precio del mercado, nuestro modelo no aporta absolutamente nada.** Es una versión más ruidosa de la misma información.

Y tiene sentido. La línea de apuestas es el precio de equilibrio de un mercado con miles de millones de dólares dentro. Estás jugando contra el consenso ponderado por dinero de todo el que sabe de NFL en el planeta.

*(El Brier score, por cierto, lo inventó **Glenn Brier en 1950 para calificar pronósticos del clima**. Mide si tus probabilidades son honestas: si dices "70%" 100 veces, ¿acertaste 70? Es el detector de mentiras de los modelos probabilísticos.)*

---

## La lección, que vale más que el modelo

El experimento dejó una frase que resume todo:

> ### Donde compites contra un mercado eficiente, pierdes. Donde nadie está compitiendo, ganas.

**Nadie está poniendo dinero a "¿este corredor va a superar su promedio esta semana?"**. No hay mercado, no hay nadie corrigiendo el precio, y por eso la señal sobrevive.

**En el resultado del partido sí hay mercado.** Por eso no sobrevive.

La herramienta apuesta todo a lo primero. Y por eso el módulo de partidos no está para decirte a quién apostarle — está para darte el **total implícito de Vegas**, que es un dato que el mercado ya calculó bien y que alimenta las decisiones de fantasy donde sí tenemos ventaja.

**Usamos al mercado. No competimos con él.**

---

## Para presumirlo en 20 segundos

> "Toma las 50,000 jugadas de la temporada y, con la misma regresión que se usa para medir cuánto aporta un maestro descontando a sus alumnos, calcula en qué es mala cada defensa — por posición y por tipo de jugada, corrigiendo por qué tan buenos fueron los rivales que enfrentó. A cada jugador lo convierte en un vector de cómo produce, y el matchup es el producto punto entre ese vector y las debilidades del rival. Los ratings de equipo son Elo, el mismo del ajedrez. La confianza en los datos crece sola con un estimador de James-Stein, el de la paradoja del béisbol. Lo probamos contra la temporada 2025 completa: los verdes superan su propio promedio, los rojos se quedan cortos, con significancia estadística. Contra Vegas perdemos, y lo decimos abierto — porque ahí compites contra un mercado de miles de millones, y en fantasy no compites contra nadie."

---

**Referencias de las ideas:** Elo (1960, ajedrez) · Tikhonov (1943) y Hoerl-Kennard (1970, ridge) · James-Stein / Efron-Morris (1975, béisbol) · Carter & Machol (1971, EPA) · Brier (1950, clima).

*Herramienta informativa con datos públicos y gratuitos. Las predicciones de partidos son estimaciones estadísticas, no consejo financiero.*
