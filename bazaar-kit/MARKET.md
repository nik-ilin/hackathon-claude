# Mercado entre equipos

`market_agent.py` analiza todas las cartas publicadas, el inventario privado y el libro de El Rastro. Se lanza con el mismo `.env` que el agente de Abuela Carmen.

```bash
bash run.sh market --dry-run
bash run.sh market --execute --cycles 8 --max-spend 60 --reserve 100 --per-card 30
```

El primer comando solo consulta la API y escribe `data/market_report.json`. El segundo **sí publica y acepta ofertas**: hasta ocho ciclos, como máximo una acción por ciclo, con una espera de tick entre ellos. No es un proceso permanente. Por defecto, sin `--cycles`, ejecuta un solo ciclo. Usa `--action buy`, `sell`, `list` o `bid` para limitar el tipo de acción.

## Política

- Informa de las cartas presentes, los IDs de copias sobrantes y las cartas que faltan en cada página publicada. Las páginas incluyen comunes, infrecuentes y raras; las épicas y legendarias se consideran adquisiciones adicionales, no requisitos de página.
- Conserva al menos una copia de cada referencia. Solo vende duplicados, y no compromete más copias de una referencia con otra oferta ya activa.
- Primero busca demanda existente para convertir duplicados en efectivo y ofertas de venta rentables para conseguir cartas ausentes.
- Calcula el coste de compra y el ingreso neto de venta con una cota conservadora de comisión: porcentaje redondeado hacia arriba más cargo por carta. La documentación del SDK indica que paga quien acepta. Si publicamos, reservamos igualmente un colchón en las pujas.
- Una compra debe dejar al menos `--margin` (2 P por defecto) de excedente frente al **valor marginal privado obtenido por API**. No añade bonos de colección cuya fórmula completa no se haya verificado. Prioriza el cierre de una página entre oportunidades del mismo tipo, sin inflar su precio máximo.
- Si no hay un trato inmediato conveniente, alterna ventas competitivas de duplicados y propuestas públicas por cartas ausentes. Una venta se anuncia una prima por debajo de la oferta comparable más barata, respetando el valor de la copia que perdemos y el margen. Sin comparable usa el valor de catálogo como referencia heurística.
- Las pujas parten de aproximadamente el 85 % del mejor precio de venta observado o del valor de catálogo, y tienen en cuenta las pujas competidoras. Se descartan referencias sin copias acuñadas salvo evidencia de una oferta real. No publica una puja muy alejada de la referencia únicamente para gastar todo el presupuesto disponible.
- Las publicaciones caducan en cuatro ticks. La ejecución posterior recalcula el mercado; no envía mensajes ni intenta identificar a equipos detrás de alias públicos.

## Límites y recuperación

Los valores por defecto son: 100 P de reserva, 40 P de coste máximo por carta y 100 P de compromisos acumulados de compra. `data/market_state.json` conserva estos compromisos monetarios entre reinicios. **El techo `--max-spend` se compara con lo ya comprometido; no es dinero adicional por lanzamiento.** Una puja publicada consume presupuesto aunque expire sin ejecutarse: es una limitación deliberadamente conservadora, no un registro exacto del gasto realizado. Las ventas no regeneran este presupuesto.

El bloqueo compartido `data/agent.lock` evita ejecutarlo simultáneamente con el comprador de vendedores. Además, no realiza escrituras si hay conversaciones abiertas o una compra del otro agente pendiente de liquidación. Termina primero ese trabajo; el módulo no cierra esas conversaciones por su cuenta. El análisis de solo lectura sí puede coexistir.

Revalida oferta, inventario, valor y comisiones antes de escribir. Excluye ofertas propias, lotes, trueques, cartas ocultas, condiciones adicionales y ofertas casi caducadas. No opera en mercados de equipos ni compra duplicados para especular. Un cambio de comisión anunciado bloquea nuevas acciones hasta que se aplique.

Guarda una aceptación como pendiente antes de enviarla. Confirma posteriormente con el cambio del activo y el efectivo. Si una escritura tiene respuesta ambigua, conserva el bloqueo lógico y no la reenvía. Una publicación confirmada **no** equivale a una venta o compra liquidada. Una publicación de respuesta perdida requiere auditoría del servidor y del registro; no se recupera automáticamente ni se debe borrar el estado a ciegas.

El informe distingue ingreso neto por venta, excedente de colección y propuestas sin aceptación. No calcula rentabilidad histórica si no conoce el coste de adquisición. No garantiza compradores ni ganancias.

## Verificación

```bash
python3 -m unittest test_market test_negotiation -q
```

Las pruebas usan escenarios locales. El dry-run valida lecturas y cálculos con el esquema real, pero no prueba liquidaciones reales ni publica ofertas.

## Motor v2 (por defecto desde market-2.0)

`market_agent.py` usa ahora el motor de `trading.py` salvo que se pida `--engine v1`. El motor v1 y sus pruebas se conservan sin cambios.

```bash
./run.sh market                                   # análisis completo, solo lecturas (por defecto)
./run.sh market --apply-corrections               # además anota en data/negotiations.jsonl las correcciones de precio
./run.sh market --execute --cycles 6 --max-spend 60 --reserve 100 --per-card 30 --margin 3
./run.sh market --execute --action sell           # solo aceptar demanda existente para duplicados
```

**Valoración (verificada con el servidor).** `collection_value` = Σ por referencia de `book × multiplicador × copy_marginals[k]` por cada copia. El agente lo comprueba en cada ciclo y no ejecuta nada si su modelo no coincide. El `your_value` de una copia poseída es el marginal de la última copia; `/me/value` es el de la siguiente. Cada operación se valora como ΔU = efectivo recibido − entregado − comisión propia + V(después) − V(antes), copia a copia (lotes y swaps incluidos). El bono de página no está documentado: no se suma al completar una página, y una operación que rompería una página completa se bloquea.

**Comisiones.** ⌈precio × 5 %⌉ + 1 P por carta movida, a cargo de quien acepta. Lo confirman cuatro liquidaciones reales (18→3, 40→4, 22→3, 9→2). Al publicar no pagamos comisión. Al aceptar, sí.

**Ofertas.** Considera el tablón, las ofertas dirigidas a nosotros y las propuestas en conversaciones entre equipos. Admite compra, venta, swaps y lotes de hasta 4 cartas. Valida estado, caducidad, destinatario, mercado propio, extras y tipos no admitidos (sobres). Comprueba también que las copias que nos piden estén libres, y entrega siempre la copia de menor valor marginal. Por defecto conserva una copia de cada referencia; las excepciones se activan con `--allow-last-copy LAT-05,...`. Si hay una puja rival que no podemos superar, rebaja la probabilidad de ejecución de nuestra puja.

**Capital.** Las reservas se calculan con las ofertas abiertas que devuelve el servidor en cada ciclo, así que una oferta caducada o cancelada deja de consumir presupuesto (el `committed` de v1 no la liberaba nunca). El gasto confirmado de la sesión (`--max-spend`) se cuenta aparte, solo con liquidaciones observadas. Una publicación como máximo por activo y por referencia.

**Recuperación.** `data/market_ledger.json` guarda cada acción con una clave de idempotencia **antes** de enviarla. Los estados posibles son `intent`, `submitted`, `rejected`, `ambiguous`, `settled` y `released`. Una respuesta de red perdida queda `ambiguous` y bloquea nuevas escrituras hasta que las liquidaciones públicas o las ofertas abiertas la resuelvan. Una publicación de respuesta perdida que aparece en el servidor no se vuelve a publicar.

**Actividad concurrente.** El bloqueo local no ve otros ordenadores. Por eso cada ciclo compara las liquidaciones públicas y las conversaciones con dealers con lo que registraron los agentes de este ordenador (v2, v1 y el de vendedores). Si hay actividad no registrada desde el primer `--execute`, la ejecución se para salvo `--allow-concurrent`. No escribe mientras haya conversaciones con dealers abiertas o aceptaciones pendientes de otro agente, y no las cierra.

**Informe.** `data/trading_report.json` y la salida de consola incluyen:

- liquidaciones con su origen (propia o desconocida), comisión y sentido del efectivo;
- correcciones del registro;
- inventario, páginas y pérdida por copia extra;
- capital libre, reservado y pendiente;
- oportunidades con precio, comisión, ΔV, excedente, probabilidad (un supuesto si no hay datos), caducidad y bloqueos;
- operaciones propuestas, publicadas, pendientes y liquidadas;
- la puntuación, con aviso de retraso del leaderboard y de operaciones de otros clientes.

El excedente no es una conversión a puntos.

## Day 2 (coordinador con `--engine intel`)

El mercado entre equipos ya lo opera el **coordinador** (`./run.sh coord`) con la inteligencia multi-venue de `market_intel.py`. `market_agent.py` sigue funcionando por sí solo para El Rastro, pero no debe ejecutarse a la vez que el coordinador (comparten bloqueo).

```bash
./run.sh coord --intel 8 --show 20                                   # análisis Day 2, solo lecturas
./run.sh coord --execute --ticks 120 --max-posts 4 --max-spend 200 --reserve 100 --per-card 60 --margin 2
python3 sim_day2.py                                                  # simulación local de los casos de Day 2
```

Novedades:
- Lee el libro de **todos** los venues abiertos y las ofertas dirigidas a nosotros.
- Elige el venue por coste total del comprador o neto del vendedor, con las comisiones leídas de la API.
- Publica en El Duende (v02) con 120 ticks, rebaja sin bajar del suelo y para las guerras de precios.
- Repuja en escalera, publica trueques sin efectivo y pujas dirigidas con evidencia, y reprecia sus propias ofertas.
- Detalles completos en ARCHITECTURE.md → «DAY 2 STRATEGY».

## COMPLETED PAGE PROTECTION

> Once a page is completed, the agent treats the minimum set of cards required to preserve that page as non-tradeable inventory. Only duplicate copies beyond the protected requirement may be sold or swapped.

- **Restricción dura, prioridad 1**, por encima de cualquier ΔU, precio, liquidez o puntuación estratégica.
- **Acciones cubiertas:** venta, publicación, trueque, lote, oferta dirigida, propuesta a equipos, vendedores, arbitraje y reprecio.
- **Solo salen duplicados por encima del mínimo:** `tradeable_surplus = max(copias − comprometidas − 1, 0)`.
- **Una acción que rompería una página es inviable:**
  - el planificador y el coordinador la marcan con `BLOCKED: card belongs to completed page` antes de ordenar;
  - `send` la vuelve a comprobar antes de la red.
- **Ofertas abiertas que se vuelven inseguras** al completar la página se cancelan con prioridad máxima, sin `--cancel-unsafe`.
- **Cada bloqueo se registra como `PROTECTED_PAGE_BLOCK`**, con página, carta, activo, acción y motivo.
- **Cambiar la regla** requiere editar `page_guard.py` (`PROTECTION_ENABLED`, `PROTECTED_REQUIRED_COPIES`). Detalles en ARCHITECTURE.md → «COMPLETED PAGE PROTECTION».

## Ejecución y capital (proceso vivo recomendado: `coordinator.py`)

- **El proceso vivo es `./run.sh coord --execute`.** `market_agent.py` queda para diagnóstico, simulación y pruebas heredadas, porque su bucle se para cuando un ciclo no actúa.
- **Las pujas solo pueden usar el capital de mercado.** La liquidez de vendedores (el mayor máximo económico entre las negociaciones activas, o `--dealer-liquidity` sin ninguna) queda apartada.
- **Rebalanceo:** si un cierre de vendedor o una compra inmediata superior necesita efectivo, se cancelan las pujas más débiles. Después se espera la confirmación del servidor y se ejecuta.
- **Pujas obsoletas:**
  - se retiran siempre si ya tenemos la carta o si ya no compensan;
  - por edad o baja probabilidad, solo si el capital de mercado escasea (`--rebalance-threshold`, `--stale-age`).
- **Trueques propios:** se valoran con la carta que recibimos (`trading.evaluate_own_open_offer`), el mismo evaluador en publicación, seguridad y cancelación.
- **Un activo físico, una obligación:** las ofertas duplicadas se retiran automáticamente.
- **Probabilidad de ejecución con banda de confianza:** HEURISTIC, EARLY DATA, LEARNING o LEARNED.
- **Rendimiento separado en tres:** REALIZADO, ABIERTO y ESTIMADO.
- Detalles en ARCHITECTURE.md → «EJECUCIÓN, CAPITAL Y CIERRE DE TRATOS».

## Inteligencia de contrapartes y venta táctica

- **`data/market.db`** (SQLite) se alimenta del MISMO snapshot del coordinador, sin llamadas extra. Contiene ofertas, liquidaciones, evidencia de posesión, interés por carta y colección, cotas de reserva y perfiles de estrategia, todos con confianza. Consultas: `./run.sh coord --intel-card LAT-10 --intel-team t14 --intel-db-stats`.
- **Capital.** Las pujas pasivas no pueden usar ni la liquidez de vendedores ni el colchón táctico (`--tactical-buffer`), ni superar `--max-passive-frac`. El exceso se libera cancelando las peores pujas. `--capital-report` y `--open-bid-audit` lo muestran.
- **Venta táctica** de un duplicado a una contraparte real: `--sale-target LAT-10=86` (por defecto).
  - Nunca vende la copia que mantiene una página completa: con una sola copia, NO VENDER.
  - Ancla por encima del objetivo, concede de forma decreciente, no baja del suelo económico y cierra cerca del objetivo.
- **Una sola vía por carta buscada:** los trueques duplicados que piden la misma carta se retiran automáticamente.

## Market Test: broker propio (`market_broker.py`) y banco de pruebas (`sim_bench.py`)

**Cómo puntúa (RULES.md).** Cada ~2 h todos los venues reciben el mismo libro sintético (`bench_offers`, ids `b<run>-<n>`). La nota es la fracción del excedente posible (entre los límites ocultos) que se realiza. Igualar al puesto gratuito da la mitad de los puntos y la media de los tres mejores, los puntos completos. Cada sesión cuenta el mejor venue abierto durante ella, y la ronda promedia sus sesiones. Un broker solo actúa en un venue `board`: en uno `auto` (el puesto incluido) el motor cruza antes. En un `board` sin broker no se cruza nada, así que el proceso debe estar vivo toda la sesión.

**Qué hace el broker nuevo** (`./run.sh market-broker`; `--mode stall` = puesto exacto; `--probe 0` sin sondeo):
1. **Suelo:** toda oferta que el puesto cruzaría (`starter_broker.bench_plan`) queda cruzada, quizá con otro socio.
2. **Excedente máximo:** entre los emparejamientos que cumplen el suelo y cruzan por cotización, elige el de mayor excedente estimado (algoritmo húngaro). Así cruza pares que el emparejamiento ordenado del puesto deja fuera (pujas 10 y 8 contra asks 7 y 9: el puesto cruza 1, este 2) cuando el excedente estimado del par extra es positivo.
3. **Límites estimados:** desde la primera cotización vista y un sombreado aprendido en la sesión. La relajación de las ofertas que se van sin cruzar mide el sombreado; mientras hay pocas, se usa el prior del 20 %.
4. **Precio:** punto medio de las cotizaciones, como el puesto. Envía primero lo que tiene más prisa.
5. **Sondeo** (3 por tick, activado por defecto): pares sobrantes que no cruzan por cotización pero sí por límites estimados. Solo funciona si el servidor valida contra límites reales, cosa que no está documentada. Si no, el servidor los rechaza sin coste y el sondeo se apaga solo tras 3 rechazos sin ningún acierto.
6. **Vigilante:** vuelve al plan del puesto el resto de la sesión ante un error del planificador, un plan que no cubre los cruces del puesto, ≥ 3 rechazos propios (> 25 %) o un sombreado observado < 5 %.

**Un resultado de construcción:** un par extra respecto al puesto siempre tiene excedente negativo *por cotización* (bid_extra ≤ bid_k < ask_k ≤ ask_extra). Validando por cotización, la única mejora posible es apostar a que el sombreado cubre ese hueco. Por eso la ganancia sin sondeo es pequeña.

**Banco de pruebas:** `python3 sim_bench.py [--server quote|limit|libre] [--scenario dificil] [--sessions N]`. Reproduce el libro sintético con compradores y vendedores de límite oculto, sombreado, paciencia, relajación cuadrática y firmes, en 6 escenarios (`dificil` = 75 % firmes y 70 % impacientes, como el test de las ~21:30). Ejecuta el puesto, starter_broker, el broker nuevo y sus variantes, y un oráculo miope de referencia. Son resultados de un modelo, no de la API: sirven para comparar entre mecanismos, no para predecir la nota.

**Comisión.** `market_broker` cotiza el punto medio bajado lo justo para que el comprador pague la comisión. Con `fee_bps > 0`, el punto medio de `starter_broker` puede ser ilegal y el motor lo rechaza. En el banco, 300 bps cuestan un 4–11 % de eficiencia solo por los cruces que dejan de ser legales, y un 13–18 % si además la comisión se descuenta del excedente (`--fee-bps 300 --leak`). Las comisiones no puntúan: **el venue debe ir a 0 bps y 0 P por carta.**

**Frente a `broker_engine.py` / `broker_run.py`** (`sim_bench` los ejecuta tal cual):
- Sin sondeo, a 0 bps, la eficiencia media es la misma.
- `market_broker` queda por debajo del puesto en menos sesiones gracias al suelo: 8 % frente a 14 % en el escenario normal.
- `broker_engine` con `defer` rinde menos que el puesto.
- El tope de 10 cruces por tick de `broker_run.py` pierde un ~4 % en libros densos.

Pruebas: `python3 -m unittest test_market_broker`.

## Cambio a venue `board` propio (`venue_switch.py`)

`BAZAAR_KEY=tk_... python3 venue_switch.py` (en seco, por defecto) comprueba con GET la caja (≥ 270 P + colchón), el nivel (≥ 2), que no haya un venue propio, el horario (sin sesión del Market Test en curso según el feed y al menos `--min-lead` min hasta la próxima según `/api/schedule`; si no se puede leer, `--next-bench-in MIN`) y el autotest de `market_broker`. Imprime el plan y el déficit de caja.

Con `--execute [--announce]` abre `board` a 0 bps y 0 P por carta, toma la broker key de la respuesta solo en memoria y la pasa al entorno de `market_broker.py` (sin `BAZAAR_KEY`). Un supervisor relanza el broker si muere o si se para su latido (`data/venue_broker.heartbeat`, solo tick y hora). Tras 3 caídas en 10 min pasa a `starter_broker.py`, y si la clave es rechazada para. Cada incidencia lanza una ALERTA (`--alert-cmd` para notificar fuera). El anuncio solo se envía cuando el broker ya late. `--resume` relanza el supervisor con `BROKER_KEY` o con la clave de `/api/me`, si el servidor la expone.

**Riesgo:** si el proceso cae, el venue no cruza nada. Además, la broker key solo se devuelve una vez. **Vuelta atrás:** cerrar el venue devuelve la fianza tras un cooldown, pero RULES.md no promete que vuelva el puesto, y sin venue la sesión cuenta 0. La vuelta atrás real es `--broker-mode stall`, que cruza como el puesto. Detalle en el docstring de `venue_switch.py`.

Pruebas: `python3 -m unittest test_venue_switch test_market_broker`.
