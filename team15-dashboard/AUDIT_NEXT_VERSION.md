# Auditoría extrema y plan de la siguiente versión

Fecha: 2026-10-03  
Producto auditado: dashboard de estrategia y catálogo de Team 15  
Objetivo de negocio: subir posiciones en el ranking sin destruir valor de colección.

## 1. Diagnóstico ejecutivo

El dashboard actual ya resuelve bien la primera pregunta operativa: **qué puedo vender, qué me falta, cuánto se ha pagado y qué equipos aparecen alrededor de una carta**. La interfaz también hace visible la incertidumbre en varias zonas y mantiene una separación razonable entre ventas liquidadas, ofertas y rumores.

El principal límite es temporal y estratégico: la aplicación muestra muy bien una foto enriquecida, pero todavía no mide suficientemente **cómo está cambiando el mercado**, qué señales son recientes, cuál es la tasa de conversión de una demanda en venta, ni cuántos puntos de ranking se esperan por cada acción. Para tomar decisiones repetidas cada tick, la siguiente versión necesita convertirse en un sistema de seguimiento de oportunidades y no sólo en una vista de estado.

## 2. Lo que funciona

### Datos y decisiones

- Diferencia ventas liquidadas de pujas, lotes y rumores.
- Muestra mediana, rango, número de ventas y precios recientes.
- Separa `sell_floor`, `buy_ceiling`, caja disponible y reserva.
- Expone equipos que tienen y que piden cada carta.
- Marca explícitamente que una ausencia en el feed no prueba ausencia de inventario.
- Ordena oportunidades de venta y canje por valor neto privado.
- Desactiva señales privadas cuando faltan credenciales o hay inconsistencia de valoración.

### UX/UI

- Jerarquía clara entre resumen, estrategia, radio, ranking y catálogo.
- KPIs y tarjetas de acción arriba de la página.
- Barras fáciles de leer para ventas, cobertura y posesión observada.
- Filtros de catálogo por situación, rareza, colección y orden.
- Navegación por anclas y pausa de refresco.
- Tipografía sans serif, buen contraste, foco visible y responsive básico.

### Integración

- `GET /api/strategy` ofrece un contrato consumible por agentes.
- `team15.strategy.v1` documenta campos y fórmulas.
- La clave permanece en el servidor y la interfaz es de sólo lectura.

## 3. Riesgos y defectos a corregir

### P0 — Riesgos de decisión

1. **Frescura mezclada.** El usuario ve en una misma fila una venta histórica, una posesión observada y una demanda actual sin edad o fecha comparable. Cada señal necesita `observed_at`, `age_ticks` y un badge de frescura.
2. **Confianza insuficiente.** Una mediana de una venta y otra de veinte ventas se visualizan casi igual. Añadir tamaño de muestra, intervalo robusto y nivel de confianza.
3. **Posesión observada sin decaimiento.** `holder_count_observed` es útil, pero un equipo visto hace muchos ticks pesa igual que uno visto recientemente. Aplicar recencia y conservar el conteo bruto por separado.
4. **Sin puente al ranking.** El panel ordena por `ΔU`, pero no ofrece escenario de puntos, elasticidad ni impacto esperado sobre la posición. Hay que distinguir valor de colección, efectivo, probabilidad de aceptación y puntos.
5. **Caja agregada.** `buy_capacity_after_reserve` no está asignada por plan de compras; varias decisiones pueden competir por la misma caja. Falta un presupuesto y una frontera de oportunidades.
6. **Liquidez no medida.** El precio vendido no indica cuántos días/ticks se tarda en vender. Una referencia cara con una sola venta puede ser menos útil que una común con ventas frecuentes.

### P1 — Analítica ausente

1. **Tendencia de precio:** mediana móvil, cambio 1/5/20 ticks, volatilidad y dispersión.
2. **Velocidad de mercado:** ventas por tick, tiempo entre ventas, profundidad de pujas y ratio de cancelación/caducidad.
3. **Conversión de demanda:** equipos que pidieron → equipos que ofertaron → operaciones liquidadas.
4. **Presión temporal:** demanda nueva, demanda repetida y demanda que desaparece.
5. **Valor esperado:** `ganancia_neta × probabilidad_de_aceptación`, con intervalo y escenario conservador.
6. **Coste de oportunidad:** pérdida de completar página, rareza, precio de reposición y valor de conservar la última copia.
7. **Rival intelligence:** especialización por colección/rareza, caja estimada, cartas que repite en demanda y fiabilidad histórica.
8. **Concentración:** porcentaje de cartas en manos de pocos equipos; alerta de competencia por la misma carta.
9. **Frontera eficiente:** conjunto de acciones no dominadas por riesgo, caja, valor neto y puntos esperados.
10. **Calidad de datos:** cobertura de observaciones por equipo, edad del último evento y discrepancias entre fuentes.

### P1 — UX/UI

1. La portada sigue siendo larga. Añadir modo **Resumen / Mercado / Ranking / Colección / Agentes**.
2. El catálogo requiere scroll horizontal en desktop y móvil. Añadir vista de tarjetas compactas y detalle lateral de una carta.
3. Los gráficos son barras estáticas. Añadir tooltip, leyenda de fuente, fecha de corte, denominador y selección cruzada.
4. Los filtros viven en `sessionStorage`; no son enlaces compartibles. Sincronizar estado importante en query params.
5. Falta una bandeja de alertas: nueva puja, caída de precio, demanda nueva, oportunidad caducada y cambio de posición.
6. No hay comparación “antes/después del último tick”. Añadir un resumen de cambios desde la última actualización.
7. El usuario no puede exportar una vista filtrada. Añadir CSV/JSON de catálogo, ranking y oportunidades.
8. Los estados de error están agregados. Mostrar qué fuente está degradada y qué decisiones quedan invalidadas.

### P1 — Plataforma y agentes

1. El endpoint JSON debería incluir `last_success_at` por fuente y un `snapshot_id` estable.
2. El endpoint local necesita una política explícita si se expone fuera de localhost: autenticación o modo de datos públicos sin campos privados.
3. Añadir paginación o endpoints especializados para evitar que cada agente descargue 72 cartas completas si sólo necesita oportunidades.
4. Añadir `schema_version`, `source`, `confidence`, `freshness_ticks` y `evidence_ids` a cada señal.
5. Registrar snapshots históricos para backtesting y evaluación de estrategias.

## 4. Analítica que debe entrar en V2

### A. Mercado

- Serie temporal por carta: `sold_median`, p25, p75, última venta, número de ventas y ticks desde la última venta.
- Liquidez: `sales_per_tick`, `active_bid_count`, `bid_to_sale_rate`.
- Spread: diferencia entre mejor puja, precio sugerido, `sell_floor` y venta ejecutada.
- Indicador de régimen: subiendo, estable, bajando o sin muestra.

### B. Ranking

- Posición actual y anterior.
- Puntos ganados/perdidos desde el último snapshot.
- Brecha al siguiente puesto, al líder y al puesto de riesgo.
- Simulación de 3 escenarios: conservador, base y agresivo.
- Lista de acciones que maximizan puntos esperados por unidad de caja.
- Medición de eficiencia: `puntos_esperados / P_comprometidas` y `ΔU / P_comprometidas`.

### C. Inventario

- Valor total, valor líquido y valor bloqueado en última copia.
- Duplicados por liquidez, margen y coste de reposición.
- Cobertura de páginas y valor marginal de completar cada página.
- Riesgo de venta: rareza, escasez, demanda y número de copias propias.

### D. Rivales

- Matriz carta × equipo con recencia y confianza.
- Fiabilidad por rival: demandas que acabaron en oferta o liquidación.
- Segmentación: comprador agresivo, coleccionista, proveedor de canjes, rival oportunista.
- Predicción de aceptación basada sólo en historial observado y marcada como estimación.

### E. Operación

- Edad de cada fuente y tasa de fallos.
- Diferencia entre snapshot actual y anterior.
- Eventos nuevos priorizados por impacto.
- Registro de decisiones tomadas y resultado posterior para aprender qué señales funcionan.

## 5. Plan de V2 por fases

### Fase 0 — Contrato y medición (1 bloque)

- Congelar `team15.strategy.v1` y definir `team15.strategy.v2`.
- Añadir `snapshot_id`, `tick`, timestamps, fuente, edad y confianza.
- Persistir snapshots compactos en JSONL local.
- Criterio de salida: dos lecturas consecutivas se pueden comparar automáticamente sin interpretar HTML.

### Fase 1 — Tiempo real fiable (1–2 bloques)

- Reemplazar reload completo por refresco incremental con indicador `Actualizado hace…`.
- Mostrar cambios desde el tick anterior.
- Añadir estado por fuente: OK, retrasada, degradada, no disponible.
- Backoff cuando falle el feed y cachear la última lectura válida.
- Criterio de salida: una caída parcial no borra una señal válida ni presenta datos viejos como actuales.

### Fase 2 — Motor de ranking (2 bloques)

- Construir simulador de escenarios de ventas, compras y canjes.
- Calcular puntos esperados como rango, nunca como cifra exacta si el servidor no publica la fórmula.
- Optimizar por `puntos_esperados / caja`, `ΔU / caja` y riesgo.
- Mostrar top 5 acciones con explicación y sensibilidad.
- Criterio de salida: cada recomendación explica qué cambia en caja, colección, riesgo y posición.

### Fase 3 — Mercado histórico (2 bloques)

- Añadir series de precio y liquidez.
- Añadir señales de tendencia y volatilidad.
- Crear detalle lateral de carta con timeline, equipos y evidencia.
- Criterio de salida: una carta tiene contexto de precio y velocidad, no sólo una mediana.

### Fase 4 — UX de trabajo diario (1–2 bloques)

- Navegación por vistas y URL compartible.
- Alertas de eventos nuevos y cambios de ranking.
- Exportación CSV/JSON.
- Atajos: “vender hoy”, “comprar con caja”, “proteger núcleo”, “cartas competidas”.
- Criterio de salida: una decisión frecuente requiere como máximo tres interacciones.

### Fase 5 — Agentes y evaluación (2 bloques)

- Endpoints especializados: `/api/strategy/opportunities`, `/api/strategy/card/:ref`, `/api/strategy/ranking`.
- Ejemplos de consumo para agentes.
- Dataset de snapshots anonimizados para backtesting.
- Evaluar precisión de señales: demanda → oferta, venta sugerida → venta, predicción de tendencia.
- Criterio de salida: cada mejora de estrategia tiene métrica offline y métrica online.

## 6. Backlog priorizado

| Prioridad | Entregable | Impacto | Riesgo | Métrica |
|---|---|---:|---:|---|
| P0 | Frescura y confianza por señal | Muy alto | Bajo | % señales con edad y fuente |
| P0 | Brecha y simulador de ranking | Muy alto | Medio | puntos/posición esperados por acción |
| P0 | Presupuesto de caja y frontera eficiente | Muy alto | Medio | ΔU y puntos por P comprometida |
| P1 | Histórico de precio y liquidez | Alto | Medio | ventas/tick, error de tendencia |
| P1 | Alertas y diff entre ticks | Alto | Bajo | tiempo hasta detectar oportunidad |
| P1 | Detalle lateral de carta | Alto | Bajo | tiempo de decisión por carta |
| P1 | Rival scoring con recencia | Medio | Medio | conversión demanda → oferta |
| P2 | Exportaciones y endpoints especializados | Medio | Bajo | latencia y payload por agente |
| P2 | Backtesting y evaluación | Alto | Alto | mejora contra baseline |

## 7. Definición de “lista para V2”

La versión siguiente estará lista cuando un usuario pueda abrir el panel y responder en menos de un minuto:

1. ¿Qué acción aumenta más la probabilidad de subir de puesto?
2. ¿Cuánta caja compromete y qué valor de colección arriesga?
3. ¿Qué evidencia reciente sustenta la recomendación?
4. ¿Qué pasa si el rival no acepta?
5. ¿Cómo cambió la oportunidad desde el último tick?

Si alguna respuesta depende de mezclar manualmente varias tablas o de adivinar la frescura de una señal, la V2 todavía no está terminada.
