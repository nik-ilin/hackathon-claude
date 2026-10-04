# Datos y ratios para agentes

El dashboard expone `GET /api/strategy`. La respuesta tiene el esquema `team15.strategy.v2` y está pensada para que un agente pueda ordenar oportunidades sin leer el HTML.

## Estado operativo del día 3

`operations` es aditivo al esquema v2. Se calcula sobre el mismo snapshot de solo lectura:

- `duels.status`, `live`, `safe`, `urgent`, `rows`: ofertas estructuradas de `/api/duels`, margen de precio y margen con día, ticks restantes y cola de deadlines. `safe_to_accept` es una comprobación de margen con el snapshot, **no autoriza una escritura**: releer la oferta al ejecutar. Si falta clave o utilidad del día, el panel no marca la oferta como segura.
- `capital`: caja, P comprometidas en ofertas abiertas, reserva configurada y umbral de caja para estudiar un `board`. El umbral no verifica red, broker ni calendario.
- `market`: mecanismo del venue propio. `broker_health_verified` permanece `false` porque la API pública del venue no prueba el latido del proceso local.
- `queue`: alertas ordenadas por riesgo operativo. Un agente puede usarlas para decidir qué inspeccionar primero; nunca sustituyen a sus límites privados o al lock de ejecución.
- `source_tick` y `read_only`: tick de la observación y naturaleza del contrato. Si `/api/duels` falla, el estado es `unavailable`, no una lista vacía de duelos.

## Señales por carta

- `holder_count_observed`: equipos distintos que aparecen con la carta en el feed. Es una observación parcial; cero no prueba ausencia.
- `demand_count_observed`: equipos distintos que la han pedido.
- `demand_to_holder_ratio`: `demand_count_observed / max(holder_count_observed, 1)`. Úsalo para comparar presión relativa, nunca como probabilidad.
- `scarcity_ratio_minted_to_print_run`: `minted / print_run`. Un valor bajo indica una emisión pequeña respecto de su tirada publicada. No mide el stock actual del mercado.
- `sold_median`, `sold_count`, `sold_prices`: liquidaciones de una sola carta por efectivo. Los lotes y ofertas sin liquidar quedan fuera.
- `sell_floor`: mínimo rentable para una copia libre.
- `buy_ceiling`: valor privado máximo antes de aplicar caja y comisiones.
- `decision`: `sell_candidate`, `hold`, `buy_candidate` o `unreleased`.

## Ratios globales

- `demand_pressure`: suma de demandas observadas / suma de posesiones observadas.
- `collection_coverage`: referencias publicadas que tenemos / referencias publicadas.
- `free_duplicate_rate`: referencias con copias libres / referencias publicadas.
- `confirmed_sale_rate`: referencias con al menos una venta individual / referencias publicadas.

## Estrategia recomendada

1. **Vender**: filtrar `decision=sell_candidate`, exigir `demand_count_observed > 0` y contrastar `sell_floor` con `sold_median`.
2. **Comprar**: filtrar `decision=buy_candidate`, ordenar por `buy_ceiling`, y limitar por `buy_capacity_after_reserve`.
3. **Priorizar**: combinar presión (`demand_to_holder_ratio`), valor (`buy_ceiling`) y escasez (`scarcity_ratio_minted_to_print_run`). No sustituir una oferta vigente por rumores del feed.
4. **Proteger**: conservar referencias `hold` con una sola copia, especialmente si tienen mediana vendida alta o rareza elevada.

El endpoint y la interfaz son de sólo lectura. La clave del equipo nunca se envía al navegador.

## Objetivo: subir posiciones

`ranking` devuelve posición, puntuación, distancia a la siguiente posición y distancia al líder. Para priorizar acciones, ordena primero trades con `ΔU` positivo y aceptación verificable, después ventas de duplicados con comprador y mediana ejecutada. Las propuestas basadas sólo en demanda observada deben quedar detrás de las ofertas activas porque su aceptación es incierta.

La interfaz se reconstruye automáticamente cada 15 segundos. El endpoint JSON usa la misma instantánea y el mismo `tick`, de modo que un agente puede guardar `tick` y comparar cambios entre lecturas sin duplicar acciones.
