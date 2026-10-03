# Team 15 · Arquitectura del agente

> **Fuente estratégica del evento:** aplicar las precisiones de [`PAYDAY_LEARNINGS.md`](PAYDAY_LEARNINGS.md) junto con las reglas actuales de [`RULES.md`](RULES.md). En particular, puntúa el valor generado por cada trato y el canal que lo produjo; el inventario y el efectivo por sí solos no puntúan. Los horarios/asignaciones de la presentación son históricos y siempre se vuelven a consultar en el servidor.

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

## EJECUCIÓN, CAPITAL Y CIERRE DE TRATOS

La valoración privada (`trading.Valuation`) sigue siendo la única fuente de verdad económica. Esta capa no la cambia: hace que el agente **cierre** los buenos tratos, **libere** capital de compromisos débiles y **priorice** las ventanas de ejecución escasas. No baja ningún estándar económico.

```
OBSERVE → RECONCILE → VALUE → IDENTIFY → ALLOCATE (efectivo y activos, global) → FREE (cancelar lo débil si algo
superior lo necesita) → CLOSE → VERIFY (liquidación) → LEARN → REPEAT
```

**Escalera de vendedores: SECURE / OPTIMIZE** (`negotiation.py`).
- `qualifying_deals(dealer)` cuenta, de forma determinista, los tratos **liquidados** con precio de cierre **por debajo de la apertura**. Las fuentes son los hilos `deal` del servidor (precio realmente liquidado) y el diario; un hilo nunca se cuenta dos veces.
- **SECURE** (< 3 tratos que puntúan): en cuanto el vendedor concede y su precio vigente es menor que su apertura, no supera el máximo económico y la oferta estructurada es válida, **se acepta**. No se arriesga un hueco de la escalera por ahorrar 1-3 P.
- **OPTIMIZE** (≥ 3 tratos): se usa la política de regateo de siempre, para mejorar los tres mejores.
- **Lo que no cambia:** nunca por encima del máximo económico, y en modo score nunca a precio de apertura.
- **La escalera es prioridad, no valor.** No hay un equivalente en primas inventado. `dealer_accept_priority` ordena así:

| Orden | Acción | Puntuación |
|---|---|---|
| 1 | Seguridad | ≥ 10⁶ |
| 2 | Cierre válido de vendedor en SECURE | 3·10⁵ |
| 2a | … tercer trato | +10⁵ |
| 2b | … oferta que caduca en ≤ 1 tick | +5·10⁴ |
| 2c | … oferta que caduca en ≤ 2 ticks | +2·10⁴ |
| 3 | Aceptación de campaña | 10⁵ + 10³ |
| 4 | Aceptación de mercado | 10⁴ + ΔU |

  Sigue habiendo una sola aceptación por tick.
- **Oferta caducada.** Una oferta del vendedor con `expires_tick` pasado ya no se considera vigente.

**Capital en tres compartimentos** (`capital.py`).

| Compartimento | Regla |
|---|---|
| Reserva dura | Intocable (`--reserve`). |
| Liquidez de vendedores | Objetivo = el mayor máximo económico entre nuestras negociaciones activas (como se acepta una oferta por tick, basta con poder pagar la mayor). Sin negociaciones activas: `min(--dealer-liquidity, mejor máximo de una apertura viable)`. |
| Capital de mercado | Lo único que pueden inmovilizar las pujas. El planificador de mercado ve `reserva dura + liquidez de vendedores aún no expuesta` como reserva, así que una puja no puede consumir esa liquidez. |

Cada tick se imprime `CAPITAL efectivo · reserva dura · liquidez vendedores (objetivo) · pujas de mercado · expuesto con vendedores · pendiente · libre mercado · libre vendedores · presupuesto`.

**Rebalanceo por cancelación.**
- Las pujas abiertas son obligaciones reales: nunca se descuentan por su baja probabilidad de ejecución.
- `score_open_bids` evalúa cada puja con el estado actual:
  - efectivo inmovilizado;
  - ΔU esperado = P × (ganancia − precio);
  - eficiencia (ΔU esperado ÷ efectivo);
  - confianza y edad;
  - vida restante;
  - si sigue siendo la mejor puja;
  - si completa página.
- Si una oportunidad superior necesita efectivo (cierre de vendedor, compra inmediata bloqueada solo por capital), `rebalance` cancela las pujas más débiles hasta liberar lo necesario. Condición: que la oportunidad valga más que el ΔU esperado de lo cancelado + `--min-cancel-gain`. La aceptación espera al tick siguiente, cuando el servidor ha confirmado las cancelaciones. Un contraoferta al vendedor sí puede salir en el mismo tick, porque él solo puede aceptarla después.

**Pujas obsoletas.**
- **Razones duras (siempre):** ya tenemos la carta, o la puja ya no compensa.
- **Razones blandas** (puja vieja `--stale-age`, sin oferta enfrente, P < 5 %): solo cuando el capital de mercado libre baja de `--rebalance-threshold`.
- **Sin churn:** se respetan la edad mínima, el delta mínimo de reprecio y el máximo de reprecios.

**Evaluador canónico de ofertas propias.** `trading.evaluate_own_open_offer` reconstruye el efectivo cobrado y pagado, las cartas entregadas (`give`), las cartas pedidas (`want`), la comisión (0 como maker) y V(después) − V(antes).
- Lo usan la seguridad (`unsafe_own_offers`) y el reprecio del planificador.
- Antes, un trueque rentable (dar LAV-03, recibir SAL-07, ΔU +10,75) salía como "venta por 0 P". Ahora publicación, seguridad y cancelación dan el mismo ΔU.

**Auditoría de exposición de activos.** Cada tick se construye `asset_id → obligaciones` (ofertas abiertas y aceptaciones pendientes).
- Un activo en dos obligaciones se resuelve de forma conservadora: se conserva la más antigua (o la aceptación pendiente) y se retiran las demás, sin esperar a `--cancel-unsafe`.
- `send` vuelve a comprobarlo antes de la red (`ASSET_EXPOSURE_BLOCK`).
- La protección de páginas completas sigue intacta.

**Límite de peticiones.**
- `Bazaar(..., wait_on_tick=False, retries=3)`. El SDK solo reintenta lo seguro: `rate_limited` (la petición se rechazó sin ejecutarse) y fallos de red en lecturas. Una escritura con fallo de red queda ambigua hasta reconciliar; nunca se repite a ciegas.
- Las escrituras se espacian 0,6 s.
- `SlowCache` guarda por ticks el catálogo, los niveles, el leaderboard, los metadatos de vendedores y los venues, y se invalida con eventos del feed de lanzamiento, nivel, vendedor, venue o comisión. `me`, ofertas, tablones, hilos, reloj y feed se leen siempre.

**Proceso vivo.**
- `coordinator.py` (`run_loop`) es la única autoridad de escritura y recorre todos los ticks pedidos. Un tick sin oportunidades no implica nada sobre el siguiente, y un `rate_limited` o un fallo de red transitorio no lo detienen.
- `market_agent.py` se usa solo para diagnóstico, simulación y pruebas heredadas: su bucle de varios ciclos se para cuando un ciclo no actúa.

**Probabilidad de ejecución.**
- Bandas de confianza por tamaño de muestra propia: 0 = HEURISTIC; 1-4 = EARLY DATA / LOW CONFIDENCE (el prior pesa el doble); 5-14 = LEARNING; 15 o más = LEARNED.
- Cada publicación lleva `p_fill`, `sample_count` y `confidence_label`.

**Rendimiento** (`performance.py`).
- **REALIZADO:** solo operaciones liquidadas; efectivo atribuible + Δvalor verificado. Como maker no restamos la comisión que paga el otro.
- **ABIERTO:** ofertas abiertas, que no son beneficio.
- **ESTIMADO:** ΔU esperado de lo abierto.
- **Métricas:** mediana de ticks hasta llenarse, tasa de llenado por venue y tipo, tratos y conversaciones con vendedores, abandonos, escalera por vendedor.

**Diagnóstico por vendedor.** Cada tick, una línea por vendedor:
- escalera n/3 y estrategia;
- hilo, apertura, nuestra última oferta, su precio, máximo;
- si hubo concesión y los ticks restantes;
- la acción recomendada;
- si no hay trato, el motivo: precio por encima del máximo, sin concesión, capital no disponible, oferta caducada, límite de contraofertas o de ticks, cupo, conversación cerrada.

## MARKET & COUNTERPARTY INTELLIGENCE · capital global · venta táctica

```
BAZAAR API → snapshot() del coordinador (única ola de lecturas; SlowCache para catálogo, vendedores, niveles y venues)
   → intelligence.ingest(snapshot) → data/market.db (SQLite) → update_models()
   → MARKET MODEL (market_intel) + COUNTERPARTY MODEL (intelligence) → señales
   → ALLOCATOR GLOBAL (coordinator + capital) → page_guard / exposición → EJECUCIÓN (solo coordinator.send)
```

**Reparto de responsabilidades.**

| Responsabilidad | Módulo |
|---|---|
| Valor privado | `trading.Valuation` |
| Valor de mercado | `market_intel` |
| Creencias sobre contrapartes | `intelligence` (no ejecuta nada) |
| Seguridad de páginas y activos | `page_guard` |
| Capital y asignación global | `coordinator` + `capital` |
| Ejecución | `coordinator.send` |

**`data/market.db`.**
- **Tablas:** teams, cards, venues, offers, offer_snapshots, settlements, market_snapshots, inventory_evidence, team_card_interest, team_set_interest, counterparty_profiles, reservation_estimates, interactions, dealer_interactions y model_metadata.
- **Índices:** por tick, equipo, carta, venue y oferta.
- **Ingesta idempotente:** claves primarias + UPSERT. Se guardan ofertas, no solo liquidaciones (primera y última vez vistas, estado, cancelaciones), a partir de tablones, `/api/me/offers`, ofertas en pie de nuestros hilos y el feed.
- **Fallo degradado:** si la base de datos falla, el coordinador sigue con la instantánea.

**Modelos.** Todos llevan confianza UNKNOWN, LOW, MEDIUM o HIGH, evidencia, y decaimiento con vida media de 120 ticks.
- **`P_owns(team, carta)`:** solo con evidencia publicada (ask, carta ofrecida en trueque, oferta dirigida, liquidación). Sin evidencia vale 0 con confianza UNKNOWN, que no significa "no la tiene". Si después la vendió, baja.
- **`P_wants(team, carta)` y `P_interest(team, colección)`:** suben con pujas, escaladas, trueques pedidos, compras y persistencia. Bajan si la vende, y la colección pesa menos si la está liquidando.
- **`ReservationEstimate`:** cota inferior = su mayor puja; el techo queda en `None` (no se conoce). Solo con al menos 2 escaladas hay una estimación central débil (un paso más).
- **Estrategia:** PAGE_COMPLETION, RARE_ACCUMULATION, CASH_ACCUMULATION, LIQUIDATION, ARBITRAGE, MARKET_MAKING, BROAD_COLLECTION o UNKNOWN, con probabilidades, confianza y evidencia.
- **Otras consultas:**
  - `counterparty_value` (creencia, no valor privado) y `trade_compatibility`;
  - `best_counterparties(carta)` (quién la tiene) y `best_buyers(carta)` (quién la quiere);
  - `context(team)` → `CounterpartyContext`, que pueden consumir las campañas.

**Capital en cuatro compartimentos** (`capital.capital_view`). Las pujas abiertas cuentan enteras, nunca multiplicadas por P(ejecución).

| Compartimento | Regla |
|---|---|
| Reserva dura | Intocable. |
| Liquidez de vendedores | `--dealer-buffer-mode active_max` (por defecto): el mayor máximo económico activo. |
| Colchón táctico | `--tactical-buffer` (30 P): oportunidades inmediatas o dirigidas. |
| Capital pasivo | Límite = min(invertible − vendedores − táctico, `--max-passive-frac` × invertible). |

- Las pujas pasivas solo usan `free_market_cash`: el planificador recibe `passive_cap`, mientras las compras inmediatas ven el colchón táctico.
- **Exceso de capital pasivo:** se cancelan las peores pujas, con histéresis y edad mínima.
- **Rebalanceo por cancelación:** como antes, la oportunidad superior se ejecuta cuando el servidor confirma las cancelaciones.

**Higiene de obligaciones.**
- Una sola puja y un solo trueque por carta buscada (`duplicate_pursuit_cancels`). El planificador cuenta los trueques que piden una carta como "ya buscada".
- Un activo físico, una obligación.
- **Protección por COPIA FÍSICA:** `protected_assets` elige la copia de menor id no comprometida; entregarla explícitamente se bloquea, salvo que la misma operación devuelva otra copia de esa carta.

**Venta táctica** (`team_sale.py`, `--sale-target REF=PRECIO`, por defecto `LAT-10=86`).
1. Verifica las copias físicas: con 1 copia no hay venta.
2. Protege la copia de página y elige solo una excedente no comprometida.
3. Identifica al comprador mediante la oferta estructurada.
4. Fija el suelo económico = pérdida privada + margen; el objetivo es táctico y el suelo manda.
5. Valida la estructura: maker, destinatario, venue, estado, caducidad, solo efectivo y exactamente una copia, sin extras.
6. Compara la utilidad esperada de aceptar con la de publicar.
7. Decide:
   - **ACEPTAR** cerca del objetivo (no se pierde la venta por 1-3 P);
   - **CONTRAOFERTA dirigida**: ancla ≈ objetivo × 1,12 y concesiones decrecientes (p. ej. 97 → 91 → 88 → 87), nunca por debajo del objetivo ni del suelo.

   Mientras dura la negociación se suspende la venta pública de esa carta. Su aceptación tiene prioridad 2,8·10⁵, por debajo de la seguridad.

**Corrección de reconciliación.** Un `dealer_accept` liquidado ya no queda pendiente para siempre porque el `dealer_open` del mismo hilo esté marcado `settled`. Antes contaba dos veces como obligación y no sumaba al gasto confirmado.

**Sobres.** `tr.pack_analysis` da RAW_COLLECTION_EV, STRATEGIC_EV (con la reventa de duplicados × P(venta) HEURISTIC), P(página nueva) y P(duplicado). Sigue sin comprarse por rutina.

**CLI** (solo lectura): `--capital-report`, `--open-bid-audit`, `--intel-db-stats`, `--intel-team tXX`, `--intel-card REF` y `--intel-counterparties REF`.

## ESCALERA DE VENDEDORES · opt-in (`--dealer-ladder`, `--dealer-sell-dups`, `--dedupe-bids`)

Parámetros tomados de `t15-bazaar-bot/intel/AUDITORIA_LIDERES.md` (tick ~280) y `PLAN_PRIMER_PUESTO.md`. Salen de pocas conversaciones, así que son hipótesis de trabajo y no fórmulas del servidor. **Sin estos flags el comportamiento no cambia** (`test_dealer_ladder.Bids.test_default_flags_leave_candidates_unchanged`). La caja para vendedores y las pujas duplicadas ya las cubre `capital.py` (`--dealer-liquidity`, `--dealer-buffer-mode`, `--max-passive-frac`, `--stale-age`, `duplicate_pursuit_cancels`), al igual que el arreglo de reconciliación de `dealer_accept`.

| Flag | Qué hace |
|---|---|
| `--dealer-ladder` | Sustituye `decide_dealer` por `neg.decide_ladder` en nuestras conversaciones de compra, también en SECURE. El máximo económico, el capital y la validación estructural de la oferta no cambian. **Chato:** apertura al 0,70 (t18 abre a 0,72); pasos fijos de +3 en poco común y +4 en rara, porque copia nuestro paso y con +1 no se mueve; acepta a 1 P de su precio (él acepta cuando estamos a 1-2 P). Tras su final, una sola contraoferta de final-1 (funcionó 2/2); nunca en el mismo tick se acepta su final, sino en el siguiente si no responde. **Abuela:** apertura al 0,60 (t18 abre una común de 12 a 7), pasos de 1 y hasta 15 contraofertas, porque su final baja con la paciencia. **Enrutado:** las poco comunes se compran a la Abuela (21-22 P frente a 27-31 P de Chato). **Vendedores nuevos de nivel 3 (Pilar…):** apertura al 0,80, 35 % de la brecha por paso, 3 contraofertas y nunca más del 95 % de su apertura; se ajusta con `--ladder-new-open/-step/-counters/-ticks/-max-frac`. En modo score nunca se paga su apertura. |
| `--dealer-sell-dups` | Vende duplicados comunes a la Abuela (abre en 5 P, final 6 P abras como abras). Como t02, pide 10 P y baja de 1 en 1. El suelo es el valor perdido más `--ladder-sell-margin`. En modo score no se acepta su apertura. Solo se ofrecen copias de `page_guard.tradeable_surplus`, y las tres barreras de páginas completas siguen activas (la candidata lleva `asset`). Nuestra petición anterior en el mismo hilo no cuenta como segundo compromiso de la copia. |
| `--dedupe-bids` | Bloquea la puja nueva y cancela la puja pasiva abierta por una carta que ya negociamos con un vendedor (dos cierres = un duplicado). |

```bash
./run.sh coord --dealer-ladder --dealer-sell-dups --dedupe-bids                                   # análisis
./run.sh coord --execute --ticks 120 --dealer-ladder --dealer-sell-dups --dedupe-bids --duende-venue rastro
```

Pruebas: `test_dealer_ladder.py`.
## CAMPAÑA DE PÁGINA · objetivo actual: Malasaña (`page_campaign.py`, `--page-campaign MAL`)

- **Objetivos:** las cartas de página que FALTAN según el inventario actual. `need = 1` si no la tenemos y 0 si ya la tenemos; nunca se persigue una segunda copia.
- **Estado:** 3_MISSING → 2_MISSING → 1_MISSING → COMPLETE, con prioridad de ordenación HIGH → VERY HIGH → CRITICAL. La prioridad solo ordena: nunca autoriza ΔU < margen.
- **Valor no lineal:** `gain_if_bought` se recalcula cada tick con `Valuation.delta` sobre el inventario actual; la última carta incluye el bono de página. La nota de secuencia muestra el valor de cada carta si fuera la última, porque conviene dejar para el final la más disponible.
- **Rutas** (se elige la de mayor utilidad esperada entre las que tienen ΔU ≥ margen):
  - ask existente, público o dirigido, por coste total con comisión: la última carta o una muy rentable se compran ya, sin regatear;
  - trueque existente;
  - trueque dirigido con un duplicado que el dueño quiere (nunca una copia protegida ni comprometida, nunca la última copia);
  - puja dirigida que abre por debajo del techo, sin revelarlo.
- **Dueños:** `owners_ranked(ref)` usa solo equipos identificables (`tNN`) y evidencia publicada o liquidada.
  - **Alias:** una oferta que un tablón muestra bajo un alias nunca se atribuye a un equipo, aunque el feed exponga otro maker. En venues que anonimizan, lo que solo vemos por el feed tampoco.
  - **Dueño coleccionista:** su probabilidad de respuesta se multiplica por (1 − P(también la quiere)).
- **Higiene:**
  - se cancela toda oferta propia que pida una carta que ya tenemos;
  - con una ruta inmediata se cancelan las búsquedas pasivas de esa carta;
  - una vía por carta;
  - mientras la campaña esté activa no hay pujas públicas para cartas ajenas a ella (foco de capital);
  - si un cierre de campaña necesita efectivo, rebalanceo por cancelación.
- **Vendedores:** las aperturas por cartas de la campaña tienen prioridad, y una conversación por una carta de la campaña se negocia en SECURE aunque el vendedor ya esté en OPTIMIZE.
- **Al completarse:** la campaña termina y `page_guard` protege una copia de cada carta de la página; solo los duplicados son negociables.
- **Evidencia:** las rutas propuestas se guardan en `market.db` (`interactions`, `campaign:*`).

## FASES HACIA EL CIERRE (`phases.py`, `--phases`)

Opt-in: sin `--phases` el coordinador se comporta como antes. El cierre es el oficial del servidor (`clock.closes`, con su zona horaria); los umbrales son parámetros, no constantes.

| Fase | Cuándo (por defecto) | Efecto |
|---|---|---|
| A · operación activa | hasta cierre − 90 min | Comportamiento normal: compras, ventas y trueques, con la concurrencia que permite el servidor. |
| B · transición | cierre − 90 a − 30 min | Una compra que inmoviliza efectivo debe dejar al cierre al menos `w × meta` libre (w sube de 0 a 1) o tener una salida de reventa **respaldada**. El capital pasivo, las caducidades y la exposición se reducen. Las ventas rentables suben en la ordenación. |
| C · tesorería | últimos 30 min | Sin compras ordinarias. Se cancelan las ofertas con efectivo que otro podría aceptar y gastar el capital de mañana (la reserva se libera cuando el servidor confirma la cancelación). Los últimos 2 ticks no se publica nada nuevo. |

- **Meta:** 150 P libres como mínimo, 200 P deseable (`--cash-target-min`, `--cash-target-stretch`). Es una meta, no una garantía: las ventas siguen exigiendo el margen económico, y las últimas copias de páginas completas siguen protegidas por `page_guard`.
- **Efectivo libre** = caja − efectivo reservado en ofertas abiertas (pueden aceptarse todas a la vez) − exposición con vendedores − aceptaciones pendientes. No incluye ventas futuras ni comisiones prometidas.
- **Escenarios al cierre, sin probabilidades:** confirmado · si se llenan las ventas ya publicadas · máximo observado con las ventas ejecutables hoy.
- **Adelanto gradual:** si incluso el máximo observado no alcanza la meta, B y C empiezan antes (hasta `--phase-accelerate-max` min según el déficit) y el resumen lo explica.
- **Reventa:** exige una puja viva de otro equipo por esa carta, descontada por `--exit-haircut` (un parámetro, no una probabilidad) y tiempo para las dos operaciones. Que un vendedor venda más caro no es una salida.
- **Concurrencia:** un mensaje por lado y conversación y tick, una aceptación por tick y hasta `--max-posts` publicaciones por tick (acotado por `offers_per_team_per_tick`).
- **Negociación sin spam:** `--cooldown-ticks` (6 con `--profile fast-close`) impide repetir una propuesta dirigida sin éxito al mismo equipo por la misma carta; una propuesta pendiente no es un rechazo.
- **Observabilidad por tick:** fase y minutos al cierre, efectivo libre y comprometido, déficit a 150/200 P, compras y ventas pendientes, operaciones cerradas y beneficio neto reconciliado, siguiente acción, bloqueos y los escenarios de efectivo al cierre.
- `sim_phases.py` compara A, B y C sobre el mismo estado.

## OPORTUNIDADES COMPARTIDAS: dashboard ↔ memoria ↔ ejecutor (`opportunities.py`)

Antes había dos cálculos que no podían coincidir. El dashboard compraba la «oportunidad» comparando `min(asks históricos)` con el precio de un dealer (`Oracle.arbitrage`), sin id de oferta ni vigencia, y atribuía el ask al equipo que alguna vez lo publicó. El coordinador decidía sobre las ofertas vivas. Ninguna señal del dashboard llegaba a una candidata (la traza real: oferta #4483, t01, SAL-10 a 76 P, evento 16638 del tick 307, caducada en el 327; el dashboard la seguía mostrando como «ahorro +91» seis horas después, y el agente solo tenía candidatas `dealer_open`).

- **Una sola fuente de decisión:** las candidatas del coordinador. `export_shared` las escribe cada tick en `data/opportunities.json` (`--execute`) o `data/opportunities_analysis.json` (análisis: nunca pisa al agente real); el dashboard solo las lee.
- **Cada oportunidad** lleva fuente, tick, `offer_id`, contraparte, rol, vigencia (`valid_until`, `verified_live`), comisión, valor marginal, excedente neto, capital necesario, estado (SELECCIONADA / EJECUTABLE / BLOQUEADA) y motivo.
- **Roles:** vendedor confirmado · comprador confirmado (oferta abierta verificada en el servidor este tick) · poseedor histórico · buscador histórico · anónimo (alias, nunca atribuido).
- **Memoria = evidencia, no orden:** las pistas históricas (feed, `market.db`) se contrastan con las ofertas vivas y quedan como VIGENTE / CADUCADA / RETIRADA / NO VIGENTE / SIN VERIFICAR. Solo una VIGENTE se evalúa, y la evalúa el coordinador con su valoración.
- **Referencia ≠ arbitraje:** dos precios de venta son «referencia de precios»; «arbitraje» exige las dos patas vivas, netas de comisión y de equipos distintos (`executable_arbitrage`).
- **Revalidación:** antes de enviar una aceptación de mercado, `send` vuelve a leer el servidor (tablón, `me`, `my_offers`, reloj): oferta abierta y vigente, mismo maker y precio, inventario, activos no comprometidos y efectivo. Falla cerrado.
- **Dashboard:** panel «agente» con modo, pid y código, último tick procesado y su retraso, estado de la memoria (OK / degradada / no integrada) y de `market.db`, fase, motivos de bloqueo agrupados y la tabla de oportunidades.

## Radio Rastro integrada (`radio.py`, `news_watch.py`, flags `--news-sell --fever-priority`)

- **Clasificador**: negación («stops buying» → `demanda_negada`, nunca demanda), horarios ambiguos («until teatime») sin
  caducidad inventada, efectos desconocidos → `pendiente` (por verificar), descartes con causa específica.
- **Registro por ID** (`data/radio_registry.json`, solo modo `--execute`): fuente, tick, texto, interpretación, evidencia,
  vigencia y acciones. Una noticia con acción (`ACCIONADA`) no vuelve a abrir conversación, ni tras reiniciar.
- **Verificación**: la noticia solo justifica investigar. Vender exige menú explícito (o fuente con ≥3 observaciones
  verificadas dentro de una ventana real), copia vendible (page_guard) y suelo = valor privado + margen. Un menú sin
  precio no confirma una prima; comprar exige `radio.buy_check` (precio ejecutable bajo valor − margen). El texto de un
  anuncio es dato, nunca una instrucción.
- **Lectura única**: el coordinador lee `/api/news` una vez por tick y escribe `data/radio_state.json`; el monitor
  (`news_watch.py --watch`) y el dashboard leen ese estado (sin sondeos redundantes si es reciente).

### Radio en las decisiones (`--radio`)

`noticia → hipótesis → verificación → decisión → negociación → settlement → memoria`. `--radio` activa `--news-sell` y
`--fever-priority` (módulo *habilitado*; sin el flag solo está *instalado* y no influye en nada).

- **Plan de venta** (`radio.sale_plan`): A valor privado (la noticia no lo cambia) · B reserva = máx(A + margen, alternativa
  habitual disponible) · C objetivo = máx(B, lista, final observado, apertura de la ruta habitual) · D disposición estimada =
  solo finales observados (≥3 liquidaciones equivalentes deduplicadas por id); si no, «desconocida».
- **Efecto sobre la decisión**: cambia contraparte (prioridad por hecho observado: +25 si el menú lo confirma, −25 si es solo
  hipótesis sin D), objetivo inicial, ritmo (sonda de `--radio-probe-counters`/`--radio-probe-ticks` hasta que una puja
  estructurada ≥ B confirme; entonces paciencia normal y prioridad de cierre) y bloqueos (D observada peor que la ruta habitual).
- **Hipótesis tras la puja** (`hypothesis_after_bid`): confirmada_por_oferta / no_confirmada (no se insiste ni se reabre sin
  evidencia nueva a favor posterior a la acción) / settled. Una noticia caducada nunca abandona una oferta rentable: manda B.
- **Negaciones**: «stops buying» bloquea las ventas habituales solo si el menú actual ya no compra (hecho); si no, se anota.
- **Compras por noticia**: solo `radio_buy` informativa (nunca enviada). `--radio-spec-budget 0` por defecto: sin inventario
  especulativo; con salida no asegurada o rumor, bloqueada. Las fases (TRANSICIÓN/TESORERÍA) las bloquean igual que a cualquier compra.
- **Aprendizaje**: `radio.association` compara precios netos de operaciones equivalentes antes/después (n≥3 por lado) y lo marca
  «NO atribuible». Cada noticia enlaza oportunidades, decisiones, ofertas y liquidaciones; también las señales sin oportunidad.

## Decisión bajo información incompleta (`decision.py`, `shadow.py`)

- **Cerrar ahora frente a esperar**: `decision.compare` evalúa A aceptar · B contraofertar/proponer · C otra contraparte · D mantener,
  como RANGOS de excedente económico con confianza baja/media/alta; sin probabilidades inventadas. Una oferta ejecutable válida
  no se penaliza por una alternativa hipotética: solo esperan las alternativas con evidencia (puja vigente, o ≥3 cierres comparables
  con cota inferior > oferta + `min_step`) y con tiempo; urgencia de caja (w), plazo agotado o caducidad inminente cierran.
- **Alternativas por activo** (`decision.alternatives`, informe de ventas rápidas): pujas vigentes, rango de cierres comparables,
  compradores con puja reciente, antigüedad y siguiente alternativa. Una sola oferta de salida por `asset_id`.
- **Concesiones**: sin reprecio por menos de `min_step` (2 P) ni por silencio, cancelación o caducidad.
- **Selector** (`--selector economic`, opt-in): nivel 0 seguridad · 1 compromisos y vencimientos · 2 oportunidades por valor económico
  (la urgencia de caja de las fases es un campo explícito, `cash_urgent`; los bonos de `score` quedan solo como desempate).
- **Exclusión estratégica** (`--deny-soft`, `--deny-soft-cost`): evalúa nuestro excedente sacrificado y las alternativas visibles del rival;
  `--deny-teams` (acuerdos expresos) sigue siendo duro.
- **Tesorería**: si faltan fondos y las ventas observadas son lentas, la fase B dura al menos la mediana de llenado (adelanto gradual,
  tope `accelerate_max`); los escenarios siguen sin probabilidades y no se gasta contra ingresos hipotéticos.
- **Team 5**: el resumen v10 separa comisión debida, cobrada confirmada y pendiente (1 P prometido no es efectivo).
- **Sombra**: `python3 shadow.py` compara política anterior y propuesta sin enviar nada.

- **Ventanas de publicación** (`fast_sales.stage`/`revised_price`): las propuestas se agrupan en ventanas de `--fast-sales-ticks`.
  La caducidad natural abre otra ventana sin enfriamiento (el enfriamiento queda para liberaciones explícitas). Una ventana
  sin respuesta no es un rechazo; `--fast-sales-dry-windows` (2) ventanas secas seguidas al MISMO precio sí son evidencia:
  se baja un paso = máx(2 P, 25 % de la distancia al mínimo), nunca bajo el mínimo; tras rebajar, la racha empieza de cero.

## Aprendizaje desde evidencia pública (`learning.py`, `lab.py`)

fuentes (feed público vía instantánea del coordinador y `feed_history.jsonl`) → almacenamiento (`agent_memory.sqlite3`
`events`, ahora con `event_seen`: tick en que lo vimos; `market.db` de `intelligence`) → características (experiencias
normalizadas, ciclos de vida de ofertas con liquidación INFERIDA, precios comparables con decaimiento, perfiles por
publicador y lado) → modelos (tasas con IC90 y agrupación si n_eff < 3, mediana ponderada, detección de cambio) →
decisiones (solo vía una versión de política acotada: plazo, ventanas secas, paso mínimo, ajuste de objetivo ±10 %, orden
de contacto, uso del precio aprendido). Nunca toca límites, protección de activos, contabilidad ni validación.
`python3 lab.py replay|eval|sim|shadow|propose`; `--policy-version vN` aplica una versión (sin él, configuración actual).

## Última hora (`--profile last-hour`)

Perfil sobre el coordinador existente (sin agente paralelo): `--final-floor 150` (suelo inviolable de saldo LIBRE tras compromisos,
cálculo acumulado por tick; compras bloqueadas si el saldo no es fiable o el barrio ya tiene página completa), `--max-rounds 2`
(tope de contraofertas nuestras por conversación con vendedores, también en la escalera), fases B desde cierre−60 min (cerrar y
comprar a vendedores) y C desde cierre−25 min (liquidar duplicados y cancelar lo que no cierre), sin publicaciones nuevas en los
últimos 10 ticks (confirmar liquidaciones). Sin campañas con otros equipos ni de página. La radio no se activa (`--radio` actúa en ventas).
