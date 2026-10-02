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
