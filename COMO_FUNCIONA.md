# NFL EDGE — Cómo funciona, de arriba a abajo

*Documento para entender y poder explicar la herramienta: qué hace, cómo la construimos, qué revisa cada semana, cómo calcula, y — lo más importante — qué tan bien funciona de verdad.*

---

# PARTE 1 — QUÉ ES Y PARA QUÉ SIRVE

## El problema que resuelve

Cada semana, en fantasy, tomas dos decisiones que valen partidos:

1. **A quién alineo** (start/sit)
2. **A quién agarro de la banca de agentes libres** (waivers)

La mayoría de la gente decide con "este jugador es bueno" o con rankings genéricos. El problema es que **los rankings genéricos ignoran contra quién juegas**. Y ahí está la información que nadie está usando bien.

## La idea central, en una frase

> No basta con saber si una defensa es buena o mala. Hay que saber **en qué exactamente** es mala, y si eso coincide con **cómo exactamente** produce tu jugador.

Ese es el corazón de la herramienta. Todo lo demás es la ingeniería para llegar ahí.

## El ejemplo que lo explica todo

Tienes un corredor. Enfrenta a Denver, que es **top-5 de la liga contra el pase**. Cualquier ranking te diría "matchup difícil, siéntalo".

Pero si abres el dato real, resulta que Denver es **29º de 32 en yardas por recepción permitidas a corredores**. Sus linebackers son un desastre cubriendo a RBs saliendo del backfield. Son buenos contra receptores, malos contra corredores que reciben.

Ahora mira a tu corredor: el 40% de su valor fantasy viene de recepciones, no de acarreos.

**Ese matchup no es rojo. Es verde — pero verde por el aire, no por tierra.** Esa es la clase de cosa que la herramienta encuentra y que un ranking general jamás te va a decir.

---

# PARTE 2 — CÓMO LA CONSTRUIMOS

## La arquitectura, en cristiano

Imagínate cuatro piezas conectadas:

```
   ①  FUENTES              ②  MOTORES              ③  RESULTADO       ④  ENTREGA
   ───────────             ────────────            ────────────       ──────────
   nflverse                Motor defensivo         Un archivo         Página web
   Sleeper        ──────▶  Motor de jugadores ──▶  JSON con      ──▶  en tu
   ESPN                    Motor de cruce          todo calculado     celular
   Open-Meteo              Motor de partidos
```

Y todo eso lo dispara un reloj automático (GitHub Actions) tres veces por semana, sin que nadie toque nada.

## ① Las fuentes de datos — y por qué cada una

**Todas son gratuitas y no requieren registro, tarjeta ni llave de acceso.** Esto no fue casualidad, fue un requisito de diseño.

### nflverse — el esqueleto

Es un proyecto abierto que publica los datos oficiales de la NFL procesados y limpios. Lo usamos a través de una librería de Python llamada `nflreadpy`.

> **Nota técnica importante:** originalmente el plan era usar `nfl_data_py`. Al verificarlo descubrimos que **está deprecado y archivado desde septiembre de 2025** — su propio README dice que no habrá más mantenimiento. Si hubiéramos construido sobre esa base, la herramienta se habría roto a mitad de temporada. Usamos `nflreadpy`, que es el sucesor oficial del mismo equipo.

De ahí sacamos:

| Dato | Para qué |
|---|---|
| **Play-by-play** (cada jugada de cada partido) | La materia prima. Sin esto no existe nada de lo demás |
| Estadísticas semanales | Producción de cada jugador |
| Snap counts | Cuántas jugadas estuvo en el campo — el uso real |
| Rosters semanales | Qué posición tenía cada jugador esa semana |
| Reporte de lesiones | Out / Doubtful / Questionable |
| Depth charts | Su lugar en la jerarquía del equipo |
| ff_opportunity | **Puntos fantasy esperados** dada su oportunidad |
| Calendario | Partidos, y **las líneas de apuestas de Vegas** |

**Por qué el play-by-play es indispensable:** no puedes calcular "cuántas yardas por recepción le permite esta defensa a los RB" desde una tabla de totales. Tienes que ir jugada por jugada, ver quién recibió el balón, cruzarlo con su posición ese día, y sumar contra la defensa rival. Eso solo lo permite el play-by-play.

**El hallazgo gratis:** el calendario de nflverse ya trae `spread_line`, `total_line` y los moneylines de cada partido. Es decir, **las líneas de Vegas vienen incluidas**. No necesitamos ninguna API de apuestas de pago.

### Sleeper — el pulso del mercado

Gratis, sin registro. Nos da las **altas y bajas en tendencia de las últimas 24 horas** de millones de ligas de fantasy.

No es un dato de rendimiento, es un dato de **comportamiento de la gente**. Y lo usamos al revés de como lo usa todo el mundo:

> Si un jugador ya está en tendencia, ya lo agarraron. **La señal valiosa es el cruce inverso**: jugadores cuyo uso real subió fuerte pero que *todavía no* están en tendencia. Eso es llegar antes que tu liga.

En el dashboard aparecen marcados como **"ANTES DE LA MANADA"**.

### ESPN — la señal rápida

API pública sin llave. nflverse actualiza por lotes; ESPN te da los **inactivos del domingo unos 90 minutos antes del kickoff**. Es el respaldo de última hora.

### Open-Meteo — el clima

Gratis, sin llave. Pronóstico de viento, lluvia y temperatura por estadio, solo para los 20 estadios al aire libre (los domos se ignoran).

**Por qué importa:** el viento arriba de 15 mph hunde el juego aéreo profundo y favorece el terrestre. Es de las variables más subestimadas del fantasy y no cuesta nada incluirla.

## ② Los cuatro motores

---

### MOTOR 1 — La matriz de permisividad defensiva

Este es el más pesado y el más valioso.

**Paso A: etiquetar cada jugada.** Tomamos las ~50,000 jugadas de una temporada y a cada una le pegamos la **posición del jugador que tocó el balón**, cruzando con el roster de esa semana exacta. Ahora podemos decir "esta recepción fue de un TE contra la defensa de Miami en la semana 7".

**Paso B: agregar en ~28 categorías.** Para cada una de las 32 defensas medimos:

*Contra la corrida*
- Yardas por acarreo permitidas a RB
- Yardas por acarreo permitidas a QB
- Carreras explosivas (10+ yardas) permitidas
- TDs de tierra permitidos
- EPA por corrida

*Contra el pase, separado por posición del receptor*
- A RB: objetivos, recepciones, yardas por objetivo, yardas por recepción, YAC
- A WR: objetivos, yardas — **y separado en ruta corta (menos de 10 yardas aéreas) vs profunda**
- A TE: objetivos, yardas, TDs — la categoría donde más se esconden las ventajas
- Pases bomba (20+ yardas aéreas)

*Contexto*
- Ritmo (jugadas por partido permitidas)
- EPA por intento de pase
- TDs por jugada en zona roja
- Presión generada (sacks, golpes al QB)

**Paso C: EL AJUSTE QUE HACE LA DIFERENCIA.**

Aquí está lo que separa esta herramienta de un ranking cualquiera.

Una defensa que enfrentó a Baltimore, Detroit y Buffalo se ve peor de lo que es. Una que enfrentó a Carolina, Cleveland y Tennessee se ve mejor. **Casi todas las herramientas gratuitas ignoran esto y por eso sus rankings mienten.**

Nosotros resolvemos esta ecuación:

```
yardas_permitidas  =  media  +  efecto_defensa  +  efecto_ofensa
```

con una técnica llamada **regresión ridge** (mínimos cuadrados regularizados). En términos simples: le pregunta a los datos "de todo lo que pasó en este partido, ¿cuánto fue mérito de la defensa y cuánto fue mérito del rival?", y separa las dos cosas.

Además, los partidos recientes pesan más que los viejos (decaimiento exponencial), porque las defensas cambian con lesiones y ajustes.

**Paso D: rankear.** Cada defensa recibe un lugar del 1 al 32 en cada categoría. **1 = la más dura, 32 = la más permisiva.** Y se calcula en tres ventanas de tiempo que después se mezclan:

- Temporada completa (45% del peso)
- Últimas 4 semanas (35%)
- Últimas 2 semanas (20%)

---

### MOTOR 2 — El perfil de cada jugador

Para cada jugador calculamos:

**Uso** — lo que más predice el futuro
- % de snaps que juega
- Target share (qué porción de los objetivos del equipo son suyos)
- Carry share
- Share de yardas aéreas
- **WOPR** (métrica estándar: 1.5 × target share + 0.7 × air yards share)
- Toques en zona roja

**Eficiencia**
- Yardas por objetivo, por acarreo
- EPA por objetivo
- YAC

**Suerte vs realidad** — la joya escondida

Comparamos sus **puntos reales** contra sus **puntos fantasy esperados (xFP)** dada la oportunidad que tuvo.

> Un WR con 4 TDs pero xFP bajo está por corregir a la baja: tuvo suerte.
> Un WR con xFP alto y pocos puntos es candidato de compra: el volumen está ahí, los puntos van a llegar.

**Tendencia**
Media móvil exponencial de sus últimas 3-4 semanas de uso, comparada contra su línea base de temporada. Esto capta el **ascenso de rol antes de que se refleje en la caja de resultados** — que es exactamente cuando quieres agarrarlo.

**Seguridad del rol**
Lugar en el depth chart, varianza de snaps, y quién está lesionado delante de él.

---

### MOTOR 3 — El cruce (aquí está la magia)

**Paso 1: descomponer al jugador en canales.**

Cada jugador se parte en cinco canales de producción, según su uso REAL:

| Canal | Qué es |
|---|---|
| `rush` | Acarreos |
| `rec_short` | Recepciones de menos de 10 yardas aéreas |
| `rec_deep` | Recepciones de 10+ yardas aéreas |
| `redzone` | Trabajo dentro de las 20 yardas |
| `pass` | Pases lanzados (solo QB) |

Ejemplo real: Christian McCaffrey queda en 45% tierra, 35% recepción corta, 3% profunda, 18% zona roja. Josh Allen queda en 76% pase, 18% corrida, 6% zona roja.

**Paso 2: medir la debilidad del rival en cada canal por separado.**

No en general — **en ese canal específico**. Cada canal tiene sus propias métricas defensivas asignadas según la posición del atacante.

**Paso 3: multiplicar y sumar.**

```
score_bruto = Σ (peso_del_canal × debilidad_del_rival_en_ese_canal)
```

Volviendo al ejemplo del corredor contra Denver:

```
Canal tierra:     peso 0.68 × debilidad(-0.4)  = -0.27   ← ligeramente en contra
Canal recepción:  peso 0.32 × debilidad(+1.9)  = +0.61   ← fuerte a favor
                                                  ─────
                                          total = +0.34  ← VERDE, por el aire
```

**Paso 4: aplicar modificadores.**

| Modificador | Cómo se calcula |
|---|---|
| **Entorno de anotación** | Total implícito del equipo, sacado del spread y el total de Vegas |
| **Guion de juego** | Favorito grande → más corridas. Underdog grande → más volumen de pase |
| **Ritmo** | Jugadas esperadas del partido |
| **Vacantes** | Si el WR1 está Out, sus objetivos se redistribuyen. Se calcula, no se adivina |
| **Clima** | Viento castiga canales profundos, favorece el terrestre |
| **Lesión propia** | Out = descartado. Doubtful = penalización fuerte. Questionable = leve |

**Paso 5: convertir a 0-100 y al semáforo.**

El score bruto se convierte a **percentil dentro de su propia posición** (un 70 significa cosas distintas para un TE que para un WR).

- 🟢 **Verde**: 70 o más
- 🟡 **Amarillo**: 45 a 69
- 🔴 **Rojo**: menos de 45

**Paso 6: escribir la explicación en español.**

Esto es importante que lo sepas: **la explicación NO la escribe una inteligencia artificial.** Se genera con reglas fijas a partir de los términos que más pesaron en el cálculo, citando el ranking real de la defensa en la métrica concreta.

¿Por qué así? Dos razones:
1. **Funciona sola** cada martes sin que nadie esté presente.
2. **Nunca inventa un dato.** Si dice "31º de 32 en yardas a WR profundo", ese número salió del cálculo, no de la imaginación de un modelo.

---

### MOTOR 4 — Predicción de partidos

Tres señales que se combinan:

**1. Elo con margen de victoria.** Un sistema de rating (como el de ajedrez) alimentado con todos los partidos desde 2010. Cada equipo tiene un número; ganar te sube, perder te baja, y ganar por paliza te sube más. Entre temporadas, todos regresan un tercio hacia la media.

**2. EPA ajustada por rival.** La misma técnica ridge del motor defensivo, aplicada a la eficiencia ofensiva y defensiva. Capta la forma actual.

**3. Contexto.** Localía, días de descanso, bye.

Todo eso produce un **margen esperado**, que se convierte en probabilidad con una regresión logística **calibrada con datos reales** — no asumida. (La localía, por ejemplo, salió medida en **2.0 puntos**, no la pusimos a mano.)

**Comparación con Vegas:** al moneyline se le quita el "vig" (la comisión de la casa) para obtener la probabilidad real del mercado, y se compara con la nuestra.

---

# PARTE 3 — QUÉ TAN BIEN FUNCIONA DE VERDAD

Aquí es donde hay que ser brutalmente honesto. Corrimos la **temporada 2025 completa, semana por semana**, con una regla estricta:

> Para predecir la semana W, solo se usan datos anteriores a W. Cada semana se reconstruye todo desde cero con la información que realmente existía ese martes. Nada de mirar el futuro.

## A) Predicción de partidos: **Vegas nos gana**

| Métrica | NFL EDGE | Vegas |
|---|---|---|
| Acierto del ganador (271 partidos) | **63.5%** | **65.7%** |
| Brier score *(más bajo es mejor)* | 0.2292 | **0.2121** |
| Contra el spread (ATS) | **46.1%** | — |

**Cuando el modelo y Vegas discrepan** (38 partidos, el 14% del total):
- Gana el modelo: **42.1%**
- Gana Vegas: **57.9%**

**Cuando coinciden** (233 partidos): aciertan el **67.0%**.

### Qué significa esto en la práctica

1. **El modelo predice ganadores bastante bien** — 63.5% es respetable, muy por encima del azar.
2. **Pero Vegas es mejor.** Y cuando no estamos de acuerdo, **Vegas tiene la razón casi 6 de cada 10 veces**.
3. **Contra el spread es peor que una moneda al aire** (46.1%; el punto de equilibrio para ganar dinero es 52.4%). **No sirve para apostar contra la línea. Punto.**

**La conclusión útil:** el valor del módulo de partidos no es "apostarle a lo que diga". Es (a) entender **por qué** un partido está como está, con récords, ratings y EPA a la vista, y (b) el **total implícito**, que sí alimenta las decisiones de fantasy. Cuando el modelo discrepe fuerte de Vegas, la lectura correcta es *"Vegas sabe algo que yo no — probablemente una lesión, descanso o clima"*, no *"encontré valor"*.

Los mercados de NFL son de los más eficientes del mundo. Que un modelo gratuito no les gane no es un defecto: es lo esperado.

## B) El semáforo de fantasy: **sí funciona** ✅

Esta es la prueba que importa, porque es el corazón del producto.

**Cómo se midió, controlando por calidad del jugador:** comparamos a cada jugador **contra sí mismo**. No contra otros. Sus puntos reales de esa semana, menos su propio promedio previo. Si el semáforo funciona, los verdes deben superar su promedio y los rojos quedarse cortos.

3,988 jugador-semanas de la temporada 2025:

| Semáforo | n | Puntos reales | Su promedio | **Diferencia** |
|---|---|---|---|---|
| 🟢 Verde | 1,216 | 8.97 | 8.42 | **+0.54** |
| 🟡 Amarillo | 1,032 | 8.18 | 8.12 | **+0.07** |
| 🔴 Rojo | 1,740 | 7.39 | 7.67 | **−0.29** |

**El orden es perfecto y monotónico:** verde arriba de su promedio, amarillo neutro, rojo abajo. Exactamente lo que debería pasar si la señal es real.

**Brecha verde − rojo: +0.83 puntos fantasy por partido.**
**Estadístico t = 3.51** → estadísticamente significativo. **No es suerte.**

### Pero no funciona igual para todos — y esto hay que saberlo

| Posición | Brecha verde − rojo | Veredicto |
|---|---|---|
| **QB** | **+2.48 pts** | Funciona muy bien |
| **RB** | **+1.60 pts** | Funciona bien |
| **TE** | **+0.93 pts** | Funciona |
| **WR** | **−0.14 pts** | ⚠️ **No funciona** |

**Los receptores abiertos son el punto débil de la herramienta.** Y tiene una explicación clara: una defensa puede ser mala "contra WRs" en promedio pero tener un esquinero estrella que sigue al receptor número 1 a todos lados. El dato a nivel equipo es demasiado grueso para capturar eso. Es un problema conocido en el análisis de fantasy y requiere datos de emparejamiento individual (quién cubrió a quién) que las fuentes gratuitas no publican bien.

**Cómo usarlo entonces:** confía en el semáforo para **QB, RB y TE**. Para **WR**, úsalo como un dato de apoyo más, no como el que decide.

### Sobre la magnitud

+0.83 puntos por partido suena poco, y hay que decirlo con honestidad: el fantasy es un juego de **muchísima varianza**. Un TD de 60 yardas de suerte vale 6 puntos y borra el efecto en un partido individual. La correlación general es +0.05 — débil, como corresponde.

**Pero se acumula.** A lo largo de una temporada, con 6-9 decisiones de alineación por semana durante 17 semanas, elegir sistemáticamente el lado correcto de una brecha de +0.83 son varios partidos ganados. Así se gana en fantasy: no con un acierto milagroso, sino inclinando la moneda un poquito, muchas veces.

---

# PARTE 4 — EL ARRANQUE EN FRÍO (lo que nadie te advierte)

Hoy, antes de la semana 1, **no existe un solo dato de juegos de 2026**. Cualquier herramienta que te muestre rankings defensivos "2026" en la semana 1 te está mintiendo o está usando datos de 2025 sin decírtelo.

Lo nuestro:

- **Semanas 1-4:** prior construido con 2025 (peso 0.7) y 2024 (peso 0.3), **regresado a la media**. Solo se conserva el 55% del rendimiento del año pasado, porque la correlación año-a-año real del EPA defensivo ronda 0.4-0.5. El resto es ruido, rotación de roster y regresión a la media. **No inflamos esto.**
- **Además**, los equipos de cada jugador se toman del **roster vigente de 2026**, no del de 2025 — así la agencia libre y el draft ya están reflejados, y los jugadores que ya no están en la liga se descartan.
- **La transición:** encogimiento bayesiano. El peso de los datos de 2026 crece como `n / (n + 4)`. Para la semana 8 el modelo es prácticamente puro 2026.
- **La transparencia:** cada corrida lleva un badge de **confianza de datos** (baja / media / alta). Hoy dice **baja**, y así debe ser.

---

# PARTE 5 — LA AUTOMATIZACIÓN

```
GitHub Actions (gratis e ilimitado en repos públicos)
  │
  ├── martes  7:00 am CDMX  ← corrida principal, ya incluye el Monday Night
  ├── viernes 7:00 am CDMX  ← correcciones de estadística de la NFL + lesiones finales
  ├── domingo 9:00 am CDMX  ← inactivos previos al kickoff
  └── botón manual          ← desde el celular, cuando quieras
```

**Por qué el martes a las 7 am funciona:** el Monday Night termina cerca de las 3-4 UTC del martes. nflverse actualiza el play-by-play cada noche después de día de juego (datos crudos ~15 minutos después del silbatazo final) y los snap counts a las 0, 6, 12 y 18 UTC. Corriendo a las 13:00 UTC ya pasó el lote de las 12:00. **Tienes todo, incluido el MNF, cuando abres el celular.**

**Por qué también el viernes:** la propia documentación de nflverse advierte que la NFL emite correcciones de estadística los miércoles y jueves, y que *"los datos del jueves son los más limpios que tenemos"*. Además el viernes ya está el reporte final de lesiones. **La corrida del viernes es la que refina tu decisión final.**

Cada corrida: descarga → recalcula → escribe el JSON → hace commit → GitHub Pages publica solo. Tarda menos de 2 minutos. **Sin servidor, sin base de datos, sin costo, sin mantenimiento.**

---

# PARTE 6 — LA PÁGINA, PESTAÑA POR PESTAÑA

| Pestaña | Qué encuentras |
|---|---|
| **Start / Sit** | Tarjetas con semáforo, score 0-100 y la explicación. Filtros por semana, posición, equipo y **formato de puntuación (PPR / Half / Standard — se recalcula todo al vuelo)** |
| **Waivers** | Ranking de pickups: 35% matchup + 35% tendencia de uso + 20% oportunidad + 10% mercado. Con la bandera **"Antes de la manada"** |
| **Partidos** | Favorito, porcentaje, mi margen contra el spread de Vegas, y el histórico real de aciertos |
| **Defensas** | Mapa de calor de 32 equipos × 15 categorías. Azul = defensa dura, rojo = permisiva. **Aquí puedes encontrar tus propias joyas** |
| **Mi lista** | Guardas jugadores con un toque en la ☆ y los sigues. Se guarda en tu navegador, con código para pasarla entre dispositivos |
| **Cómo funciona** | El resumen técnico y el estado de cada fuente en la última corrida |

**Arriba de todo, dos alertas:**
- Los mejores matchups verdes de la semana
- **Los jugadores cuyo semáforo cambió** contra la semana pasada — un rojo que se volvió verde el martes es información que tu liga no tiene

**Sobre los colores:** el mapa de calor usa una escala divergente (azul ↔ gris ↔ rojo) porque el dato tiene polaridad, no solo magnitud. Y cada semáforo lleva **color + ícono + etiqueta de texto**, nunca color solo — para que funcione también si eres daltónico o lo ves bajo el sol.

---

# PARTE 7 — CÓMO EXPLICARLO EN 30 SEGUNDOS

> "Es una herramienta que cada martes descarga todas las jugadas de la NFL, calcula en qué es mala exactamente cada defensa —no en general, sino por posición y por tipo de jugada, corrigiendo por qué tan buenos fueron los rivales que enfrentó— y lo cruza contra cómo produce exactamente cada jugador. Te da un semáforo con la explicación del porqué. Lo probamos contra la temporada 2025 completa: los jugadores en verde superan su propio promedio y los rojos se quedan cortos, con una brecha de casi un punto por partido. Funciona mejor para QB, RB y TE que para receptores. También predice partidos, aunque ahí Vegas nos gana y lo decimos abierto. Todo es gratis y se actualiza solo."

---

# LÍMITES CONOCIDOS (dilos tú antes de que te los digan)

1. **WR es el punto flaco.** Sin datos de emparejamiento individual (qué esquinero cubre a quién), el dato a nivel equipo no alcanza.
2. **No le gana a Vegas** en predicción de partidos, y contra el spread está por debajo del azar. No es una herramienta de apuestas.
3. **Las primeras 4 semanas son priors**, no realidad. Está etiquetado, pero hay que leerlo.
4. **El fantasy tiene muchísima varianza.** La herramienta inclina la moneda; no adivina el futuro.
5. **No modela lesiones intra-partido, cambios de coordinador ni sorpresas de esquema.**
6. **Depende de que nflverse siga publicando.** Es un proyecto abierto y estable, pero es una dependencia externa.

---

*Herramienta informativa construida con datos públicos y gratuitos. Las predicciones de partidos son estimaciones estadísticas, no consejo financiero.*
