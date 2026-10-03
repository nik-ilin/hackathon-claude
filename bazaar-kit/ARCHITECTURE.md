# Team 15 · Arquitectura del agente

Un único coordinador (`coordinator.py`) observa el juego una vez por tick, reconcilia lo enviado con la evidencia del servidor, genera candidatas de todos los módulos y envía como mucho una acción por clase de límite. Es la única autoridad que escribe y comparte un solo presupuesto.

```
observar ─► reconciliar ─► candidatas ─► seleccionar ─► enviar ─► liquidada ─► resultado observado
 (snapshot)   (feed, hilos,   (mercado,      (1 por clase:    (registro       (evidencia     (métricas privadas
              ofertas)        vendedores,    aceptar/mensaje/  antes de        del servidor)  antes/después,
                              seguridad)     publicar/abrir)   escribir)                      atribución incierta)
```

| Módulo | Papel |
|---|---|
| `trading.py` | Valoración marginal (con bono de página), comisiones, lectura de ofertas (venta, compra, swap, lote ≤ 4), recursos comprometidos, valor de sobres, caducidad, ofertas propias inseguras, reconciliación. Sin red. |
| `campaigns.py` | Campañas de negociación directa con equipos: descubrimiento de contrapartes con ID publicado, oportunidades (completar, vender duplicados, trueque), propuestas estructuradas, máquina de estados por negociación. Sin red. |
| `negotiation.py` | Estado de una conversación reconstruido desde el hilo; política por vendedor (`dealer_policy`, `decide_dealer`); validación estructural; registro y bloqueo de instancia. Sin red. |
| `coordinator.py` | Bucle único, presupuesto compartido, idempotencia, etapas visibles en consola. |
| `market_agent.py` | Motor de mercado independiente (v2 usa `trading.py`; v1 se conserva). |
| `starter_agent.py` | Compras a vendedores en solitario (`--first-purchase`). Su política rápida sigue disponible. |

## Lo que puntúa (Kickoff, pág. 9) y cómo lo usamos

- **Negociación (30):** duelos, la escalera de vendedores (parte del rango de precio capturada) y el valor ganado comerciando con equipos a nuestros valores. El coordinador:
  - compra y vende entre equipos solo con excedente ΔU ≥ margen;
  - con los vendedores, en modo `score`, **nunca acepta su precio de apertura** («a deal at the opening price does not count»).
- **Market-making (30):** Market Test y valor creado entre otros equipos en nuestro mercado. Ver la decisión más abajo.
- **No puntúa:** número de operaciones, comisiones, suerte de sobres, regalos. Por eso los sobres nunca se compran por rutina y la actividad no es un objetivo.
- **La conversión de excedente o de métricas privadas a puntos no está documentada.** El orden entre clases (seguridad > conversaciones abiertas > aceptar con excedente > abrir conversación > publicar) es una heurística declarada.

## Hechos verificados con el servidor

| Hecho | Evidencia |
|---|---|
| `collection_value` = Σ `book × multiplicador × copy_marginals[k]` por copia, más el bono de cada página completa | modelo igual al servidor en los ticks 94, 102, 104, 126, 129 y 137 |
| `/me/value` incluye el bono de página: LAT-10 = 177,1 = 91,0 + 0,25 × 344,5 | tick 129, La Latina 9/10 |
| `your_value` de una copia poseída = marginal de la última copia | 23 referencias, tick 94 |
| Comisión de El Rastro = ⌈5 % × precio⌉ + 1 P por carta, la paga quien acepta | liquidaciones 130, 140, 142 y 146; el efectivo cuadra exactamente (400 → 195 P) |
| Las ofertas liquidadas aparecen con `status: "settled"` en el hilo y como evento `settlement` en `/api/feed` | hilos 123, 130, 135, 139 y 143 |
| `expires_in_ticks` se divide por 4 a 60 s/tick (4→1, 8→2, 20→5); el valor por defecto del SDK (40) da 10 en el tablón | ofertas 1458, 2092 y 1688; 19 ofertas a 10 ticks en el tablón |
| `my_offers` incluye ofertas de otros equipos dirigidas a nosotros: hay que filtrar por `maker` | ofertas 1648 (t18) y 1853 (t05), tick 138 |
| `collection_value` incluye los sobres sin abrir con su `your_value` | tick 158: 661,7 = 622,9 de cartas + 38,8 de un sobre |
| Entre equipos, la propuesta va en `offer` = {give, want} de un mensaje en un hilo `kind: team` sobre un mercado; la acepta la otra parte | hilo #135 (t04 → t15, oferta 1092 liquidada) |
| El feed publica `offer.listed` con el ID real del equipo; el tablón muestra alias | tick 145; los alias nunca se usan para contactar |

Lo que **no** está verificado: la base del bono master; la comisión de un swap sin efectivo (se asume 1 P por carta); la equivalencia en ticks de 30 s y 15 s (hipótesis: ticks de 15 s); qué tratos con vendedores cuentan exactamente para la escalera más allá de «no a precio de apertura».

## Errores encontrados y corregidos

1. **Precio de cierre anotado como nuestra última contraoferta.** LAT-08 se anotó a 13 P y se liquidó a 17 P; un sobre se anotó a 15 P y se liquidó a 30 P. Ahora se lee de la oferta liquidada, y las correcciones se anotan sin borrar el original.
2. **Compra confirmada contando sobres en el inventario.** Si el sobre ya se había abierto, la compra quedaba pendiente para siempre. Ahora la confirma la oferta liquidada.
3. **Bono de página omitido.** LAT-10 aparecía con un valor de 91 P cuando vale 177 P.
4. **Caducidad:** pedíamos 4 ticks y obteníamos 1. Ahora se calibra y se registra la vigencia efectiva que devuelve el servidor.
5. **`committed` acumulativo en el motor v1.** Las pujas caducadas consumían presupuesto para siempre. Ahora las reservas se calculan desde las ofertas abiertas en el servidor.
6. **Una compra propia marcada como externa:** cuando el vendedor aceptaba nuestra contraoferta, se clasificaba como actividad desconocida.
7. **Dos procesos independientes** (vendedores y mercado) con presupuestos separados. Ahora hay un único coordinador.

## Mercado propio y Market Test: decisión

- **Ahora no se abre.** Requiere nivel 2 y 270 P (250 de fianza reembolsable + 20). Tenemos nivel 1 y unos 187 P, y ese efectivo vale más usado para completar La Latina (LAT-10, +86 P de bono).
- **Puesto gratuito:** los equipos sin mercado reciben un puesto gratuito con mecanismo `auto` cuando abren los mercados de equipo. Según RULES.md, igualar al puesto gratuito da la mitad de los puntos del Market Test; los puntos completos se fijan con la media de los tres mejores. Es decir, el puesto gratuito ya da la mitad sin inmovilizar fianza.
- **Para superar al puesto** hace falta un broker en un mercado `board` que estime los límites ocultos de los operadores sintéticos y su paciencia. `starter_broker.py` solo cruza por cotización, que es lo mismo que hace el puesto gratuito. **Eso falta por implementar.** Solo entonces tiene sentido pagar la fianza (sábado: +150 P de asignación y nivel 2 al abrirse El Chato a todos).
- **Duelos:** hay sesiones programadas (la primera es de práctica y no puntúa). No existe módulo de duelos; queda fuera de esta entrega y no se mezcla con la negociación con vendedores.

## Resultados

Hay 118 pruebas offline en verde (`python3 -m unittest test_campaigns test_coordinator test_trading test_market test_negotiation`). El coordinador sin campañas se ha ejecutado en real (tick 155: score 10,35, `neg_points` +37, La Latina completa), pero esa variación coincide con operaciones de otros clientes y con el refresco del leaderboard, así que su atribución es incierta. Las campañas entre equipos no se han ejecutado en real. Las simulaciones de `sim.py` usan vendedores sintéticos y no son una medida del juego real. Cada operación real queda en `data/coordinator_ledger.json` con sus métricas privadas antes y después, para evaluarla después sin ajustar políticas a una sola observación.

## Campañas de negociación entre equipos

Las campañas son un módulo más del coordinador, no un proceso aparte: comparten efectivo, reserva, activos comprometidos y límites de la API.

- **Contrapartes:** solo IDs que publica el servidor (leaderboard; `maker`/`actor` del feed; ofertas dirigidas a nosotros). Lo que es aceptable tal cual lo acepta el módulo de mercado sin conversar.
- **Tipos:** completar colección (comprar una carta ausente a quien la ofrece), monetizar duplicados (a quien la pide) y trueque en una sola oferta estructurada (dar un duplicado y recibir una ausente, con efectivo si hace falta).
- **Estados:** detectada → contactada → propuesta → esperando → contraoferta → aceptada → liquidada, además de abandonada y ambigua. Se reconstruyen desde el hilo al reiniciar.
- **Política:**
  - Apertura defendible: 85 % de su precio al comprar, 115 % de su puja al vender.
  - Concesiones del 50 % de la distancia restante, sin cruzar nunca la reserva privada.
  - Como máximo 3 propuestas y 6 ticks por negociación, y 2 negociaciones activas.
  - Un solo contacto por contraparte y objetivo.
  - El silencio no es un rechazo.
  - No se contacta si nuestra reserva queda por debajo del 60 % de su precio, ni si no hay efectivo libre para la primera propuesta.
- **Exclusión:** la misma carta no se persigue por dos vías (campaña, puja pública, vendedor) y la misma copia no se compromete dos veces. Las propuestas antiguas que siguen abiertas se cancelan explícitamente.
- **Fin de campaña:** deja de abrir conversaciones, retira las propuestas abiertas, cierra los hilos e informa de las obligaciones que siguen activas.

## DAY 2 STRATEGY · inteligencia de mercado multi-venue

`market_intel.py` (lógica pura) se apoya en la valoración verificada de `trading.py`, sin sustituirla. El coordinador la usa por defecto (`--engine intel`) y sigue siendo la única autoridad de escritura y presupuesto.

```
CLOCK → SNAPSHOT (me, catálogo, /api/venues, /api/venues/{id}/offers de CADA venue, /api/me/offers, feed, hilos)
      → RECONCILE → MARKET INTELLIGENCE (libros, estados por carta, estimaciones, historial)
      → CANDIDATAS (aceptar, publicar venta, puja pública/dirigida, trueque, reprecio, vendedores, campañas)
      → SELECT (1 aceptación, 1 mensaje por conversación, hasta --max-posts publicaciones por tick) → ACT → wait_tick()
```

**Cinco valores por carta, separados.**

| Valor | Origen | Etiqueta |
|---|---|---|
| Privado | `Valuation` | VERIFIED |
| De colección (ganancia o pérdida marginal, con bono de página una sola vez) | `Valuation` | VERIFIED |
| De mercado | ejecuciones > microprecio bid/ask > percentil 25 de los asks (limitado a 1,5 × catálogo: **un ask no es un valor**) > percentil 75 de los bids > catálogo | ESTIMATE, con confianza HIGH/MEDIUM/LOW/UNKNOWN |
| De trading | ΔU exacto de una operación en un venue | VERIFIED; solo la probabilidad de ejecución es HEURISTIC |
| De liquidez (demanda, oferta, escasez, spread) | señales del libro | HEURISTIC |

**Regla dura.** Ninguna puntuación estratégica justifica una operación con ΔU < margen. La puntuación para ordenar es la siguiente: para lo inmediato, ΔU más un bono estratégico; para publicaciones, P(ejecución) × excedente − coste de oportunidad − capital inmovilizado − activo bloqueado.

**Venues y comisiones.** Se leen de `/api/venues` en cada ciclo, junto con la comisión anunciada (`pending_fee`) si es mayor. Nunca se fijan en el código. El enrutado compara el **coste total para el comprador** y el **neto para el vendedor**, no el precio nominal. Comisiones a cargo de quien acepta:

| Venue | Comisión verificada (Day 2, tick 162) |
|---|---|
| El Rastro | 5 % + 1 P por carta |
| v03 | 1 % |
| v01, v02, v04, v05, v06 | 0 |

**El Duende (v02).** Publicaciones con `expires_in_ticks = 120` (`--duende-expiry`, recomendación oficial, configurable). A igual utilidad se prefiere v02, pero nunca por ser v02: si otro venue deja más ΔU esperado, gana el otro.

**Ventas competitivas.** El suelo es la pérdida de colección más el margen. El precio objetivo es el coste del rival más barato para el comprador menos un paso adaptativo (3-10 % del precio, mínimo 1 P, acotado por el spread). Sin comisión podemos cobrar más que el precio nominal de un rival de El Rastro y seguir siendo más baratos para el comprador. No se compite bajo el suelo. Para evitar guerras de precios: como máximo 2 reprecios por carta, edad mínima de 3 ticks, mejora mínima de 2 P y parada si el precio queda a menos del 15 % sobre el suelo.

**Pujas.** Reserva = mín(ganancia − margen, máximo por carta, capital libre). La puja abre al 60 % del ancla de mercado (o un paso sobre la puja rival) y sube en escalera un 15 % de la distancia restante, más deprisa cerca del cierre según `/api/clock`. Normalmente queda por debajo de la reserva, y no se puja si la reserva no llega al 50 % del ancla.
- **Puja dirigida (`to`)** solo con evidencia publicada de que el equipo tiene la carta: `offer.listed` del feed u ofertas dirigidas. La propiedad se etiqueta OBSERVED.

**Trueques.**
- Se aceptan los del tablón cuando ΔU ≥ margen. La comisión de un trueque sin efectivo es 0 en v02 y v03, y 2 P (1 P por carta) en El Rastro.
- Se publican (`give: {assets: [id]}, want: {cards: [ref]}`) con el coste de oportunidad del duplicado: la mejor venta inmediata o esperada que se pierde.

**Exclusión.** La misma carta no se persigue por dos vías en un tick, y la misma copia no se compromete dos veces. Las reservas salen de las ofertas abiertas en el servidor.

**Historial.** `data/market_history.jsonl` guarda resumen del libro por venue y carta, y liquidaciones deduplicadas, con ventana de 240 ticks y poda automática. La tendencia (UP/DOWN/STABLE) solo se calcula con al menos 6 puntos en 5 o más ticks; con menos, UNKNOWN.

**Arbitraje entre venues.** Se informa, no se ejecuta: las dos patas no son atómicas.

## COMPLETED PAGE PROTECTION · prioridad 1

> Once a page is completed, the agent treats the minimum set of cards required to preserve that page as non-tradeable inventory. Only duplicate copies beyond the protected requirement may be sold or swapped.

`page_guard.py` es la capa base de esta regla. La usan el planificador (`market_intel.plan`), el coordinador (candidatas y `send`) y `market_agent.py`, y ninguna estrategia ni flag de línea de comandos puede desactivarla. Solo un humano puede cambiarla, editando `PROTECTION_ENABLED` o `PROTECTED_REQUIRED_COPIES` en el código.

**Orden de seguridad.**
1. Nunca romper una página completa.
2. Nunca comprometer dos veces un activo bloqueado.
3. Nunca superar el efectivo libre ni la reserva.
4. Optimización económica (ΔU).
5. Estrategia de mercado.

**Funciones.**

| Función | Qué hace |
|---|---|
| `protected_page_cards(counts, catalog)` | Devuelve todas las referencias necesarias para mantener completas las páginas completas actuales. |
| `protected_required_count(ref)` | Copias protegidas de la referencia: 1 en una página estándar. |
| `tradeable_surplus(ref) = max(inventario − comprometidas − protegidas, 0)` | Copias que pueden salir. LAT-03 ×1 → 0, ×2 → 1, ×3 → 2. |
| `validate_protected_assets(...)` | Simula el inventario tras la acción, descontando también las copias ya comprometidas en otras ofertas abiertas (se asume que se llenan). Si una página completa antes deja de estarlo después, la acción es **inviable**: devuelve `BLOCKED: card belongs to completed page — would break completed page <PAGE>`. No es un valor muy negativo que un optimizador pueda compensar. |
| `delivery_of(candidata)` | Lee qué entregaría cualquier tipo de acción, a partir de sus campos (`deliver`, `assets`, `asset`, `give_ref`, `offer.give`, `want` de la oferta aceptada, `opp.deliver`). Cubre venta, publicación, trueque, lote, oferta dirigida, propuesta a un equipo, apertura de campaña de venta, vendedores, arbitraje, reprecio y tipos futuros que usen los mismos campos. |

**Dónde se aplica (tres barreras).**
1. **Planificador.** Cada oportunidad pasa por la protección al crearse. Si rompería una página, sale con el bloqueo y no se selecciona.
2. **Coordinador, antes de ordenar.** `apply_guard` revisa todas las candidatas: mercado, campañas, vendedores y motor básico. Las que romperían una página quedan con `blockers` y fuera de la selección, no con una puntuación baja.
3. **`send`, última barrera antes de la red.** Si una candidata llega sin bloqueo por cualquier vía, se recalcula la protección con las copias ya comprometidas en este tick. Si rompe una página, no se envía ni se registra la intención. `market_agent.py` aplica la misma barrera en sus dos rutas de escritura.

**Ofertas abiertas.** Si una oferta propia publicada con la página incompleta pasa a ser necesaria para conservarla, `unsafe_open_offers` la retira como acción de seguridad. Las ofertas se acumulan por id y se cancela la que cruza el mínimo protegido.
- La cancelación tiene puntuación 10⁷.
- No requiere `--cancel-unsafe`.
- No depende de que la valoración esté verificada.

**Independiente del bono.** La protección no usa el valor estimado de `page_bonus`: si el modelo cambia o es incierto, la página sigue protegida.

**Fallo cerrado.** Sin catálogo no se puede verificar la protección, y cualquier acción que entregue cartas se bloquea.

**Registro.** Cada bloqueo emite `PROTECTED_PAGE_BLOCK page=<P> card=<REF> asset=<id> action=<tipo> reason=<motivo>`, una vez por combinación y proceso, y queda en `page_guard.EVENTS`.

**Pruebas.** `test_page_guard.py`: 14 casos. Con la protección desactivada fallan 13; el que sigue pasando es el de página incompleta, como debe.
