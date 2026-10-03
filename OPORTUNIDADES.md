# Diagnóstico del repo: dónde está el valor sin usar

Medido el 2026-10-03, tick 849 (ronda 2, Saturday · Gran Vía, peso 1.0).
Team 15: puesto 11 de 18, score 24.91, negociación 17.41, mercado 7.50.

El repo no tiene un problema de falta de código. Tiene **4.603 líneas de lógica
construida, con tests, que nadie importa**, y una tubería de datos cuya primera
etapa está parada.

---

## 1. El grafo de imports

`coordinator.py` es la única autoridad que escribe en el juego. Importa 5 módulos
de los 23 que hay.

| módulo | líneas | tests | lo importa |
|---|---|---|---|
| `trading` | 588 | 5 | coordinator, campaigns, market_agent, market_intel |
| `negotiation` | 810 | 5 | coordinator, market_agent, sim, starter_agent |
| `market_intel` | 905 | 1 | coordinator, sim_day2 |
| `market_agent` | 704 | 4 | coordinator |
| `campaigns` | 354 | 1 | coordinator |
| `feed_oracle` | 750 | 0 | dashboard, feed_stream, rivals, feed_watch, playbook |
| **`signals`** | **1020** | 1 | **nadie** |
| **`rivals`** | **842** | 0 | **nadie** |
| **`duels`** | **765** | 1 | sólo `duels_run`, que nadie ejecuta |
| **`broker_engine`** | **750** | 54 | sólo `broker_run`, que nadie ejecuta |
| **`feed_stream`** | **720** | 1 | **nadie** |
| **`playbook`** | **506** | 0 | **nadie** |

`run.sh` cablea cuatro cosas: `coord`, `market`, `broker` (el `starter_broker`, un
emparejador de punto medio equivalente al puesto gratuito) y `agent`. No cablea
`broker_run.py`, ni `duels_run.py`, ni `feed_stream.py`, ni `playbook.py`.

---

## 2. La oportunidad más grande: `playbook.py` ya deriva la política que
`negotiation.py` tiene a mano

`playbook.py` lee el feed y devuelve, por vendedor y por carta, con qué precio
abrir, con qué paso ceder, cuántas rondas aguantar y cuándo levantarse — **y lo
backtestea**. Ejecutado sobre `data/feed_history.jsonl`:

```
dealer   carta    lado  techo abrir paso dejar rondas
pilar    LAT-08   bid     19    21    2    19    5
pilar    LAT-10   bid     50    54    2    50    5
pilar    SAL-10   bid     71    77    2    71    5
abuela   LAT-03   bid      6     7    1     6    6
chato    LAV-06   bid     14    15    4    14    5
chato    LAT-10   ask     81    75    4    81    5
```

Dos consecuencias.

**(a) El lado vendedor (`bid`) ya estaba derivado aquí** mientras
`negotiation.py` no tenía lado vendedor en absoluto: `item_of()` devolvía `None`
para `{"sell": {"assets": [...]}}` y el hilo se descartaba entero.

**(b) El ancla correcta es el techo observado, no un múltiplo de su puja.**
playbook abre en `techo + paso`, no en `2.2x su apertura`. Si pilar abre en 50
sobre una carta cuyo techo son 71, `2.2x` son 110: se gastan las rondas pidiendo
por encima de lo que paga. Corregido en este PR con `ceiling_seen`, dejando el
múltiplo sólo como respaldo sin observación, y con `sell_step` por vendedor
tomado de lo que playbook mide (abuela 1, pilar 2, chato 4).

**Lo que queda por hacer:** llamar a `playbook.plan_for()` desde el coordinador
para que la política salga medida en vez de escrita a mano. Los `DEALER_TRAITS`
de este PR son una mejora sobre el fallback de `0.90`, pero siguen siendo
constantes.

---

## 3. La tubería de datos está cortada en la primera etapa

```
data/feed_history.jsonl   574 eventos   ticks 640-670   (tramo de 30 ticks)
tick actual del juego     849           => 179 ticks por detrás
```

El almacén cubre **30 ticks y se detuvo hace ~90 minutos**. Todo lo que cuelga de
`feed_oracle` —precios justos, manos rivales, playbook, y la inteligencia de
mercado del panel— razona sobre esa ventana. No falla: devuelve cifras de otro
momento del mercado, que se leen igual de bien que las buenas.

`feed_stream.py --collect` es el recolector, con backfill y detección de huecos.
Nadie lo está ejecutando. Este PR añade el bloque `feed_health` al panel para que
el silencio deje de ser silencioso.

Y `.gitignore` contiene `data/`, así que el repo guarda los instrumentos y no las
lecturas: un clon nuevo arranca sin oráculo.

---

## 4. `signals.py`: 53 eventos de precio rechazado sin usar

`feed_oracle` consume tres tipos de evento (`settlement`, `offer.listed`,
`thread.message`). El feed trae doce más. En los 574 eventos recogidos:

| evento | n | para qué sirve |
|---|---|---|
| `offer.cancelled` | 53 | una oferta retirada sin liquidar es un precio que el mercado **rechazó**: cruzada con `settlement` da la curva de demanda observada |
| `thread.closed` | 2 | por qué fracasan las conversaciones con cada vendedor |
| `pack.opened` | 1 | si comprar sobres sale a cuenta |
| `venue.announcement` | 9 | la comisión vigente y quién anuncia qué |
| `duel.closed` / `duels.finished` | 2 | **los duelos son observables en el feed** |

`signals.py` (1020 líneas) interpreta exactamente esto. Nadie lo importa. Hoy el
panel sabe a qué precio se cerró una carta, pero no a qué precio se dejó de
intentar, que es la mitad de la información para fijar el precio de publicación.

---

## 5. `rivals.py`: a quién vender, no sólo a cuánto

842 líneas que reconstruyen las manos rivales del feed y estiman *cuánto de más
pagaría cada equipo por cerrar una página*. Nadie lo importa. Es la diferencia
entre publicar una carta al precio medio y ofrecérsela al equipo al que le falta
para cerrar.

La regla que el propio módulo documenta —«el feed sólo muestra lo que se
publica»— es la razón de que separe tres estados y no afirme inventario.

---

## 6. `duels.py`: un tercio de la negociación, sin coste y sin tocar

765 líneas de política de duelos, con la matemática del pastel que se encoge
(`pie_factor`, `share_of_pie`, `min_worthwhile_gain`, `preferred_day`) y 54 tests.
`duels_run.py` está parado **a propósito**, y bien: otra persona juega en vivo con
la clave del equipo, y exige `--live` junto con `--yes-i-have-the-key`.

El coste de un duelo es cero primas. Una sesión no jugada cuenta cero. Está
en el feed (`duel.closed`), así que la asistencia es verificable sin la clave.

**Acción:** `duels_run.py --dump` es de sólo lectura. Confirma que se están
jugando antes de dar por perdido un tercio de los 30 de negociación.

---

## 7. `broker_engine.py`: el emparejador bueno no está conectado al que corre

`broker_engine.py` son 750 líneas de programación dinámica exacta sobre la
estructura del libro, con `possible_surplus`, `realized_surplus` y `efficiency`,
y **54 tests**. Sólo lo alcanza `broker_run.py`, que `run.sh` no cablea. Lo que
`run.sh broker` arranca es `starter_broker.py`: 65 líneas de punto medio,
equivalente al puesto gratuito.

Medido sobre los 24 venues del leaderboard:

| venue | dueño | mecanismo | trades | volumen | traders | mercado |
|---|---|---|---|---|---|---|
| v07 | t10 | board | 8 | 74 | 7 | **12.50** |
| v02 | t12 | board | 8 | 69 | 6 | **12.12** |
| v01 | t06 | board | 3 | 101 | 3 | **11.48** |
| v21 | t09 | board | 5 | 21 | 6 | 9.38 |
| v14 | t14 | auto | 1 | 19 | 2 | 9.26 |
| v15 | **t15** | auto | **0** | 0 | 0 | **7.50** |
| v05 | t04 | board | 0 | 0 | 0 | 7.50 |

Los cuatro mejores mercados del juego son `board` con tráfico real. El techo de
cualquier `auto` es 9.26 y el suelo de `board`-con-broker es 9.38: **los rangos no
se solapan.** Y `board` con 0 trades puntúa 7.50, igual que `auto` con 0 trades,
así que abrirlo sin broker no penaliza — sólo no suma.

Lo que pagan no es el mecanismo: es **valor creado entre otros equipos en tu
venue**. v15 es `auto` con 0 trades porque nadie publica ahí. El problema es
**liquidez, no mecanismo**, y v07 atrajo 7 equipos.

**Bloqueo real:** 270 P de fianza (250 + 20) contra 74 P de caja. No es decisión,
es presupuesto. Antes de abrirlo: `python3 sim_day2.py` para verificar que
`broker_run.py` cruza.

---

## 8. El panel tenía el desglose del score y lo tiraba

`app.py` ya pedía `GET /api/me` con la clave y guardaba `me["score"]` en
`data["score"]`, pero `strategy_export()` sacaba de ahí **sólo el total**.
`coordinator.py:71` y `market_agent.py:508` sí leen el desglose completo:
`negotiating, market, neg_points, ladder_points, duel_points, mm_points, deals, rank`.

Durante toda la sesión no se pudo responder a «¿está vacía la escalera o no
jugamos duelos?», que piden acciones opuestas, teniendo el dato a un campo de
distancia. Corregido en este PR.

---

## 9. Lo que de verdad separa a los de cabeza

Correlación con `score` sobre los 17 equipos activos (t11, con 0.0 de
negociación, queda fuera: no ha jugado):

| variable | r |
|---|---|
| `negotiating` | **+0.849** |
| `deals` | **−0.394** |
| `luck` | −0.328 |
| `market` | +0.136 |
| `album_filled` | +0.074 |
| `pages_complete` | −0.048 |
| `level` | +0.034 |

Puntos de negociación por trato:

```
t01  0.939  (23 tratos, 1 página, álbum 31)  -> rank 3
t03  0.938  (25 tratos, 1 página, álbum 32)  -> rank 6
t14  0.643  (32 tratos, 2 páginas)           -> rank 1
t15  0.363  (48 tratos, 3 páginas, álbum 42) -> puesto 11 de 17
```

Tenemos el álbum más grande del juego y la eficiencia por trato en el puesto 11.
`album_filled` con r ≈ 0 y `pages_complete` con r negativa dicen que acumular no
puntúa. Confirmado por medición directa: completar El Retiro añadió +197.5 P de
valor de colección y el score bajó de 22.00 a 21.71.

**t14 es el único equipo en los dos arquetipos** (negociación ≥ 20 y mercado ≥ 9.2).
Es primero no por ser el mejor en ninguno, sino por no estar en el suelo de
ninguno. t15 está en el suelo de los dos.

---

## 10. Orden de ataque

1. **Llamar a `playbook.plan_for()` desde el coordinador.** La política medida en
   lugar de constantes. El instrumento existe y está backtesteado.
2. **Arrancar `feed_stream.py --collect`.** Sin esto, lo anterior y la mitad del
   panel razonan sobre una ventana de 30 ticks de hace hora y media.
3. **`duels_run.py --dump`.** Sólo lectura. Un tercio de la negociación, coste
   cero, asistencia sin confirmar.
4. **Conectar `rivals.py` al guion de ventas.** A quién vender, no sólo a cuánto.
5. **Conectar `signals.py`** para que `offer.cancelled` entre en el precio de
   publicación.
6. **`sim_day2.py` y luego el venue `board`**, cuando la caja llegue a 270 P.
   Subida potencial +5 en mercado; bajada 0.
