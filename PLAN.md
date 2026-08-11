# NFL EDGE 2026 — Plan y Arquitectura

**Herramienta de decisión de fantasy, waivers y predicción de partidos. NFL 2026. Costo: $0.**

---

## 0. Lo que verifiqué antes de escribir este plan

No armé esto de memoria. Corrí pruebas reales en el entorno:

| Prueba | Resultado | Consecuencia para el diseño |
|---|---|---|
| `nfl_data_py` (el paquete que pediste) | **Deprecado y archivado desde sep-2025.** El README dice literalmente: *"nfl_data_py has been deprecated in favour of nflreadpy... no further maintenance or updates are planned"* | Uso **`nflreadpy`**, el sucesor oficial del mismo equipo (nflverse). Mismos datos, mismo origen, mantenido. Si usáramos el viejo, la herramienta se rompe a mitad de temporada. |
| Calendario 2026 en nflverse | ✅ 272 partidos ya cargados, con `spread_line`, `total_line`, `away_moneyline`, `home_moneyline` | **Las líneas de Vegas ya vienen gratis dentro de nflverse.** No necesito ninguna API de apuestas de pago. |
| Snap counts / injuries / depth charts / ff_opportunity | ✅ todos disponibles y completos | Puedo calcular snap share, xFP y seguridad de rol. |
| API de Sleeper desde este sandbox de Claude | ❌ **bloqueada** (la red de este entorno es lista blanca) | Por eso GitHub Actions es la decisión correcta: allá sí hay internet abierto y Sleeper funciona. |
| Tu cuenta de GitHub | ✅ detectada: `moypenhos1610` | Puedo empujar código directo, en cuanto conectes un repo. |
| Estado de la temporada 2026 | Week 1 arranca **9–13 de septiembre**. Hoy es 11 de agosto. | **Hay cero datos de juegos 2026.** Esto define el diseño del arranque en frío (sección 5). |

---

## 1. Tus decisiones, ya incorporadas

- **Hosting:** GitHub Actions + GitHub Pages.
- **Scoring:** los tres formatos (PPR, Half-PPR, Standard) con un switch en el dashboard. Todo se recalcula al vuelo.
- **Alcance:** análisis de **todos** los jugadores NFL, más una sección **"Mi lista"** para guardar y seguir a los que te interesan.
- **Apuestas:** predicción propia + comparación contra la línea de Vegas para marcar dónde hay valor.

---

## 2. Fuentes de datos y por qué cada una

### 2.1 nflverse vía `nflreadpy` — el esqueleto

Es la única fuente pública que tiene **play-by-play crudo**. Sin eso no existe la parte más valiosa de lo que pediste: no puedes calcular "cuántas yardas por recepción le permite esta defensa a los RB" desde una tabla de stats agregados. Tienes que ir jugada por jugada, ver quién recibió el balón, cruzarlo con su posición ese día, y sumar contra la defensa rival. Eso solo lo permite el pbp.

| Dataset | Qué me da | Por qué lo necesito |
|---|---|---|
| `load_pbp` | Cada jugada: receptor, corredor, yardas aéreas, EPA, situación, zona roja | **La materia prima de la matriz defensiva.** Todo el motor de matchups sale de aquí |
| `load_player_stats` | Stats semanales por jugador | Producción base y tendencia |
| `load_snap_counts` | `offense_pct` por jugador/semana | Snap share real, no estimado |
| `load_rosters_weekly` | Posición del jugador *en esa semana* | Crítico: sin esto no puedo asignar "esta recepción fue de un TE" en el pbp |
| `load_schedules` | Calendario + **spread, total y moneyline** | Doble uso: matchups futuros **y** la línea de Vegas gratis |
| `load_injuries` | Reporte oficial (Out/Doubtful/Questionable) + estatus de práctica | Un matchup verde con un jugador limitado en práctica no es verde |
| `load_depth_charts` | Rol y jerarquía por posición | Detecta ascensos de rol antes de que se reflejen en stats |
| `load_ff_opportunity` | **Puntos fantasy esperados (xFP)** dada la oportunidad | La joya escondida: separa suerte de uso real. Un WR con 4 TDs pero xFP bajo es una trampa |
| `load_nextgen_stats` | Separación, yardas aéreas, corrida sobre lo esperado | Calidad del jugador independiente del volumen |
| `load_pfr_advstats` | Presión, tacleadas rotas, YAC | Perfila el *estilo* del jugador y de la defensa |

### 2.2 Sleeper API — el pulso del mercado

Gratis, sin token, sin límite práctico. Me da los **trending adds/drops de 24h** de millones de ligas. No es un dato de rendimiento, es un dato de *mercado*: me dice qué está haciendo la manada.

Lo uso al revés de como lo usa todo el mundo. La señal valiosa no es "está trending, agárralo" — para cuando está trending, ya lo agarraron. La señal valiosa es el **cruce inverso**: jugadores cuyo uso real subió fuerte pero que **todavía no** están trending. Eso es llegar antes que tu liga. El dashboard lo marca como **"Antes de la manada"**.

### 2.3 Extras que agrego (todas gratis, sin llave, propuesta mía)

| Fuente | Qué aporta | Justificación |
|---|---|---|
| **ESPN API oculta** (`site.api.espn.com`) | Récords, standings, noticias e inactivos casi en tiempo real | nflverse actualiza en batch; ESPN te da los inactivos del domingo 90 min antes del kickoff. Sin llave, sin registro |
| **Open-Meteo** | Clima por estadio (viento, lluvia, temperatura) | Viento >15 mph tumba juegos aéreos y totales. Es de las variables más subestimadas y es gratis sin llave |
| **`load_trades` + rosters de pretemporada** | Movimiento de agencia libre y traspasos | Sin esto, los priors de 2025 mienten en las semanas 1-4 (secciones 5) |

**Nada de esto requiere tarjeta, llave ni registro. Costo total: cero, permanente.**

---

## 3. El modelo numérico

### 3.1 Matriz de permisividad defensiva (el corazón)

Para cada una de las 32 defensas construyo una fila con ~20 categorías, calculadas desde el play-by-play:

**Contra la corrida**
- Yardas por acarreo permitidas a RB · Yardas por acarreo a QB · Tasa de acarreos explosivos (10+) · TDs de tierra en zona roja · Éxito de corrida permitido

**Contra el pase, por posición del receptor**
- A RB: targets permitidos, recepciones, yardas por recepción, YAC permitido
- A WR: targets, yardas por objetivo — y **separado en corto (<10 yardas aéreas) vs profundo (15+)**
- A TE: targets, yardas, TDs — la categoría donde más se esconden las ventajas

**Contexto**
- Ritmo (jugadas por juego permitidas) · Tasa de presión generada · EPA permitido por jugada · Puntos permitidos en zona roja

#### El ajuste que hace la diferencia

Una defensa que enfrentó a Baltimore, Detroit y Buffalo se ve peor de lo que es. Una que enfrentó a Carolina, Cleveland y Tennessee se ve mejor. Casi todas las herramientas gratuitas ignoran esto y por eso sus rankings mienten.

Yo resuelvo `yardas_permitidas ~ efecto_defensa + efecto_ofensa` con **mínimos cuadrados regularizados (ridge)**, lo que separa cuánto del resultado fue la defensa y cuánto fue el rival. Después de eso rankeo 1–32 por categoría, en tres ventanas: temporada completa, últimas 4 semanas, últimas 2 semanas (las defensas cambian con lesiones y ajustes).

### 3.2 Perfil de jugador

- **Uso:** % de snaps, % de rutas corridas, target share, **share de yardas aéreas**, share de acarreos, **toques en zona roja**
- **Eficiencia:** yardas por ruta corrida, yardas sobre lo esperado, EPA por objetivo
- **Suerte vs realidad:** puntos reales vs **xFP**. Un jugador muy por encima de su xFP está por corregir a la baja; muy por debajo, es un candidato de compra
- **Tendencia:** media móvil exponencial de 3 semanas contra su línea base de temporada — captura el ascenso de rol antes que la caja de resultados
- **Seguridad del rol:** posición en depth chart + varianza de snaps + lesiones de quien está delante

### 3.3 El cruce — exactamente el ejemplo que me diste

Este es el paso que distingue la herramienta de un ranking cualquiera. No comparo "jugador bueno vs defensa mala". Descompongo al jugador en **canales de producción** y evalúo cada canal contra la debilidad de la defensa **en ese canal específico**.

Tu ejemplo, resuelto paso a paso:

```
RB con 68% de su valor por tierra y 32% por recepción
Rival: #4 de la liga contra el pase (fuerte)  → parece matchup rojo
       #29 en yardas por recepción permitidas a RB (pésimo)

Canal tierra:     peso 0.68 × debilidad_z(-0.4)  = -0.27   ← ligeramente en contra
Canal recepción:  peso 0.32 × debilidad_z(+1.9)  = +0.61   ← fuerte a favor
Modificador: equipo es underdog por 6.5 → más pases, más check-downs al RB  = +0.15
                                                    ─────────
                                          Score bruto = +0.49  →  74/100  🟢 VERDE

Insight generado: "Ventaja de RECEPCIÓN, no de corrida. Denver es top-5 contra
el pase en general, pero es 29º dando yardas por recepción a corredores: sus
linebackers pierden en cobertura. Además tu equipo es underdog por 6.5, lo que
históricamente sube 22% los objetivos a RB. Arráncalo por el aire, no por tierra."
```

**Modificadores que se aplican encima del cruce base:**
- **Entorno de juego:** total implícito del equipo desde la línea de Vegas (spread + total → puntos esperados)
- **Guion de juego:** favorito grande → más corridas; underdog grande → más volumen de pase
- **Ritmo:** jugadas esperadas del partido
- **Vacantes:** si el WR1 está Out, sus objetivos se redistribuyen — lo calculo, no lo adivino
- **Clima:** viento y lluvia castigan canales profundos y favorecen tierra

**Semáforo:** 🟢 ≥70 · 🟡 45–69 · 🔴 <45, con percentil dentro de la posición (un score de 70 significa cosas distintas para un TE que para un WR).

**La explicación en español es determinista** — se genera con reglas a partir de los términos que más pesaron en el score, no con un modelo de lenguaje. Eso significa que funciona sola cada martes sin que yo esté presente, y que nunca inventa un dato.

### 3.4 Ranking de waivers

```
score_waiver = 0.35 × matchup      (¿tiene buen rival esta semana?)
             + 0.35 × tendencia_uso (¿su rol está creciendo?)
             + 0.20 × oportunidad   (vacantes, xFP subestimado, ascenso en depth chart)
             + 0.10 × mercado       (trending de Sleeper)
```

Y la bandera **"Antes de la manada"**: tendencia de uso alta + trending de Sleeper bajo. Ahí está el valor real.

### 3.5 Modelo de partidos

1. **Ratings de equipo:** EPA por jugada ofensiva y defensiva, ajustada por rival con el mismo método ridge, con regresión a la media
2. **Elo con margen de victoria**, actualizado semana a semana
3. **Ajustes:** localía (~1.7 pts, medida en datos 2015-2025, no asumida), días de descanso, bye, distancia de viaje y cambio de huso horario
4. **Récord y fuerza de calendario**
5. Todo eso → un **spread propio** → probabilidad de victoria vía regresión logística **calibrada con 2015–2025**
6. **Comparación con Vegas:** quito el vig del moneyline para obtener la probabilidad real del mercado. `edge = mi_probabilidad − probabilidad_mercado`

Salida por partido: favorito, % de confianza, spread propio vs spread de Vegas, y el edge. Solo marco un partido como **alta confianza** cuando el edge supera el umbral **y** los fundamentales concuerdan.

> **Honestidad sobre esto:** los mercados de NFL son de los más eficientes que existen. Voy a publicar en el dashboard el backtest real (accuracy, Brier score, calibración) sobre 2015–2025, no una promesa. Si el modelo no le gana al mercado, lo vas a ver en números. Esto es información para decidir, no consejo financiero.

---

## 4. El dashboard

Un solo archivo HTML, mobile-first (lo vas a abrir del celular), servido por GitHub Pages.

**Pestañas**

1. **Start / Sit** — tarjetas con semáforo, score 0-100, insight en español. Filtros: semana · posición · equipo · **formato de scoring (PPR / Half / Standard)**
2. **Waivers** — ranking de pickups con verde, ordenado por score, con la bandera "Antes de la manada"
3. **Partidos** — favorito, %, mi spread vs Vegas, edge, y el histórico de aciertos del modelo
4. **Defensas** — mapa de calor 32 equipos × categorías. Aquí es donde encuentras las joyas escondidas tú mismo
5. **Mi lista** — guardas jugadores con un tap y los sigues semana a semana. Persiste en tu navegador, con código de exportación para pasarla entre celular y compu

**Alertas arriba de todo:** los mejores verdes de la semana, y **los jugadores cuyo semáforo cambió** contra la semana pasada (comparando el JSON de esta semana contra el anterior). Ese segundo panel es el que te va a hacer ganar partidos — un rojo que se volvió verde el martes es información que tu liga no tiene.

---

## 5. El arranque en frío (importante, y nadie te lo va a advertir)

**No existe un solo dato de juegos de 2026.** Week 1 es el 9 de septiembre. Cualquier herramienta que te dé rankings defensivos "2026" en Week 1 te está mintiendo o está usando datos de 2025 sin decírtelo.

Mi manejo:

- **Semanas 1–4:** prior construido con 2025 (peso 0.7) + 2024 (peso 0.3), regresado a la media, y **ajustado por rotación de roster** (agencia libre, draft, traspasos vía `load_trades`). Más las **líneas de temporada completa de Vegas** como prior de mercado, que es el mejor estimador disponible antes del kickoff.
- **Transición:** encogimiento bayesiano empírico. El peso de 2026 crece como `n/(n+k)` con k≈4 para defensas y k≈5 para uso de jugadores. Para la semana 8 el modelo ya es casi puro 2026.
- **Transparencia:** cada score lleva un badge de **"confianza de datos"** (Baja / Media / Alta) para que sepas cuándo estás viendo un prior y cuándo estás viendo realidad.

---

## 6. Automatización

```
Repo GitHub (público → Actions y Pages ilimitados y gratis)

.github/workflows/refresh.yml
  ├── cron  martes 13:00 UTC  →  7:00 am CDMX   ← corrida principal
  ├── cron  viernes 13:00 UTC →  7:00 am CDMX   ← correcciones de stats + reporte final de lesiones + TNF
  ├── cron  domingo 15:00 UTC →  9:00 am CDMX   ← inactivos previos al kickoff (opcional)
  └── workflow_dispatch                          ← botón manual desde tu celular
```

**Por qué el martes 7 am funciona:** el MNF termina cerca de las 3-4 UTC del martes. nflverse actualiza el play-by-play cada noche después de día de juego (datos crudos disponibles ~15 min después del silbatazo final) y los snap counts a las 0, 6, 12 y 18 UTC. Corriendo a las 13:00 UTC ya pasó el batch de las 12:00 → **tienes todo, incluido el MNF, cuando abres el celular el martes.**

**Por qué agrego el viernes:** la documentación de nflverse advierte que la NFL emite correcciones de estadística el miércoles y jueves, y que *"los datos del jueves son los más limpios que tenemos"*. Además el viernes ya está el reporte final de lesiones. La corrida del viernes es la que refina tu decisión final.

Cada corrida: descarga → recalcula → escribe `docs/data/week_N.json` → hace commit → Pages publica solo. Sin servidor, sin base de datos, sin costo, sin mantenimiento.

---

## 7. Lo que necesito de ti

| # | Paso | Tiempo | Estado |
|---|---|---|---|
| 1 | **Crear un repo vacío en GitHub** con tu cuenta `moypenhos1610`. Sugerencia: `nfl-edge-2026`, **público** (público = Actions gratis ilimitado; privado tiene tope de minutos) | 2 min | ⛔ **Bloqueante** |
| 2 | **Conectarlo a esta sesión.** Probé el acceso: este entorno solo puede escribir en repos previamente autorizados. Necesito que lo autorices desde la app para poder empujar el código | 1 min | ⛔ **Bloqueante** |
| 3 | **Activar GitHub Pages:** Settings → Pages → Source: `main`, carpeta `/docs`. Si el permiso alcanza, lo hago yo por API | 1 min | Después del 2 |
| 4 | **Confirmar tu zona horaria.** Asumí **CDMX (UTC-6)**. Si estás en otra, lo cambio en una línea | 10 seg | Asumido |
| 5 | *(Opcional, después)* Tu **league ID de ESPN**, si más adelante quieres que filtre agentes libres reales de tu liga en vez de toda la NFL | — | Opcional |

**Nada más.** No hay tarjetas, ni llaves de API, ni servidores, ni suscripciones. Todo lo demás lo hago yo.

### Si prefieres no usar GitHub

Plan B: te entrego el proyecto completo en un ZIP y programo una tarea de Claude que corre cada martes y te manda el dashboard al chat. Pierdes el trending de Sleeper (bloqueado en este entorno) — lo sustituyo con saltos de snap share y target share derivados de nflverse, que honestamente es una señal *mejor*, solo que sin el componente de mercado. Dime y cambio de rumbo.

---

## 8. Orden de construcción

1. Repo + esqueleto + workflow que corre en vacío (verificar que la tubería vive)
2. Ingesta y cacheo de nflverse + Sleeper + ESPN + clima
3. Motor de permisividad defensiva con ajuste por rival ← el pedazo más pesado
4. Perfiles de jugador + xFP + tendencias
5. El cruce, el score 0-100, el semáforo y el generador de insights en español
6. Modelo de partidos + comparación vs Vegas
7. Dashboard
8. **Backtest sobre 2015–2025** y publicación de las métricas reales
9. Prender los crons y probar con una corrida en seco antes de Week 1

Entre hoy y el 9 de septiembre hay margen de sobra para tenerla lista, probada y con backtest antes del primer kickoff.

---

*Herramienta informativa construida con datos públicos. Las predicciones de partidos son estimaciones estadísticas, no consejo financiero.*
