# Team 15 · día 3 (domingo) · operación con mercado y duelos

Este documento es un procedimiento, no una orden de ejecutar. `./run.sh day3`, `./run.sh market-switch`,
`./run.sh celestina --leads` y `./run.sh duels --day3` son **solo lectura** sin `--execute`. Los procesos con
`--execute` escriben en la API. Cargar la `.env` legítima del equipo solo en el checkout operativo; no imprimir claves.

## Por qué estas prioridades

La presentación *Payday* de Luis Morales aclara los 100 puntos: 30 negociación, 22,5 Market Test, 7,5 valor de
tratos reales entre dos terceros en nuestro mercado, 40 jueces. El efectivo, cartas retenidas, suerte de sobres,
comisiones, volumen bruto y regalos no puntúan. Un trato de equipo gana como máximo 50 P puntuables; una pérdida
cuenta entera. En dealer cuenta la escalera, los tres mejores por dealer, con mayor peso en niveles altos; el daño
por vender bajo valor cuenta entero. El último cromo que completa una página puede valer mucho más de lo aparente.

El sábado, v15 `auto` tenía 0 tratos reales pero 7,5 de mercado; el test sintético explica la base. Por tanto:

1. **Market Test:** `board` con broker vivo puede mejorar la parte de 22,5, pero reemplazar el puesto sin continuidad
   expone a una sesión de cero. Se decide con preflight y caja privada.
2. **Tratos de terceros:** `celestina.py` encuentra parejas que ya cruzan y, con `--leads`, órdenes unilaterales
   vigentes cuando no hay pareja. Una orden de otro venue necesita que el maker la republique en v15; se anuncia
   así de forma explícita. Son leads, no tratos ni puntos confirmados.
3. **Duelos III:** no cerrar da cero. La práctica y simulación sugieren callar mientras el rival mejora, cerrar
   antes de agotar el plazo y escalonar aceptaciones (una por tick). `--day3` activa días, perfiles, ladder para mudos,
   reconciliación y verificación de oferta. `--logroll` sigue opt-in porque fue neutro en el simulador.

### Duelos III: regla de decisión durante la oleada

El alias es una pista histórica, no una identidad garantizada. La oferta **estructurada** y su evolución tienen
prioridad sobre el texto del chat y sobre el perfil supuesto. Para cada duelo, registrar rol, límite privado, día,
`deadline_tick`, oferta rival, utilidad total, tendencia y número de aceptaciones que compiten por el mismo plazo.
Una aceptación solo es elegible si el precio queda dentro del límite y el margen con día no es negativo. No convertir
la captura porcentual de la simulación directamente en puntos de leaderboard: faltan la tarta rival y la fórmula
exacta de agregación.

| Estado observado | Acción | Motivo |
|---|---|---|
| Rival mejora con fuerza, queda margen de reloj y no hay cola urgente | Esperar sin hablar | En la práctica el rival cedió por tick y nuestro silencio no consumió rondas. |
| Rival empeora, ya dio un salto grande y se plantó, o primera oferta excepcional | Aceptar si deja margen | Proteger la oferta buena frente a reversión. |
| Rival sin oferta tras tres ticks | Abrir y escalonar hasta tres propuestas | Evitar el cero de un rival mudo; cada propuesta puede consumir decay. |
| Rival se estanca con oferta aceptable | Contraoferta medida o aceptar según plazo | Buscar mejora sin perder la ventana de cierre. |
| Varios duelos aceptables con plazo común | Empezar antes: uno por tick, plazo más próximo primero | La última oportunidad no alcanza para todos. |
| Precio fuera de límite o utilidad total negativa | No aceptar; proponer dentro de límite | Una pérdida puede penalizar y el no trato vale cero. |

En Duelos III modelados con 12 ticks y decay 10 %, `--profiles` cerró 88–89 % y capturó 42–43 % de la tarta;
la política base cerró 83 % y capturó 35–36 % en las semillas probadas. Son resultados offline con rivales supuestos.
Revisar `data/duels_log.jsonl` durante la sesión: si los alias no predicen la evolución, priorizar el margen real,
la tendencia y el plazo; detener el runner si hay fallos de lectura o escritura persistentes y reconciliar en API.

## Preflight del domingo

```bash
cd bazaar-kit
./run.sh day3 --ticks 1 --capital-report --open-bid-audit
./run.sh market-switch --operating-reserve 100 --key-file data/venue_broker.key --min-lead 10
./run.sh celestina --leads --leads-json -
./run.sh duels --day3
```

La reserva de 100 P es un **escenario de partida**, no un valor universal: ajustar tras leer efectivo, pujas abiertas
y oportunidades de dealers. `market-switch` suma fianza 250, coste 20, colchón 20, reserva de operación y dinero
comprometido en pujas abiertas. Si no puede leer esas ofertas, no autoriza el cambio. La clave de recuperación,
si se usa `--key-file`, queda en `data/` (ignorado por Git) con permisos 0600; conservar ese fichero local y no
subirlo ni copiarlo a la demo.

Verificar en `/api/clock` y `/api/schedule` el round, las sesiones y los límites **cada vez**. La foto del sábado
preveía Market Test difícil ~10:17, otro ~10:38, comienzo de ronda 3 y Chamberí ~12:17, asignación de 150 P
~12:20, Duelos III ~14:17 y Market Test ~14:38. La presentación daba otras horas aproximadas: la API viva manda.

## Ejecución tras el preflight

```bash
./run.sh day3 --execute --ticks 120
./run.sh celestina --leads --execute --loop
```

`day3` no usa la fiebre de Salamanca ni las excepciones de última copia del preset `t15` del sábado. Selecciona
por valor económico con protección de páginas; deja 40 P de reserva inicial y limita publicaciones. Comprobar su
primera acción en seco y ajustar `--reserve`, `--max-spend`, `--per-card` y demás según `/api/me`. No ejecutar dos
coordinadores con la misma clave. `celestina` usa la broker key para anuncios y debe respetar sus límites; no
publica ni acepta ofertas de nuestra cuenta.

**Cambio opcional a `board`:** solo antes de una sesión, con capital libre suficiente, operador y red estables,
autotest favorable y ruta de recuperación de broker key probada. El comando explícito es:

```bash
./run.sh market-switch --execute --announce --operating-reserve 100 \
  --key-file data/venue_broker.key --min-lead 10
```

`market-switch` se queda supervisando el broker; no lanzarlo en una terminal que vaya a cerrarse o suspenderse.
Para reanudar: `./run.sh market-switch --resume --key-file data/venue_broker.key`. Si el algoritmo inteligente
falla, reiniciar con `--resume --broker-mode stall` para cruzar como el puesto. Cerrar el venue no devuelve el puesto
gratuito según las reglas publicadas.

**Duelos:** detener el coordinador y esperar a que libere `data/agent.lock` antes de `./run.sh duels --day3 --execute`.
El runner prioriza ofertas dentro de límite, incluye siempre `days` en las ofertas propias de dos cuestiones,
relee antes de aceptar y no reintenta una escritura ambigua sin reconciliar. Registrar `duel_points` privado antes
y después. Tras la oleada, detener el runner antes de reiniciar `day3`.

## Medición durante la partida

- Separar `ladder_points`, `neg_points`, `duel_points`, `mm_points` y total de `/api/me`; el leaderboard público
  llega con retraso y no atribuye una acción individual.
- Para v15, registrar tratos **liquidados** entre terceros y valor creado, junto a anuncios y ofertas que acabaron
  caducando. No presentar una orden unilateral como comprador/vendedor confirmado de la otra parte.
- Para cada sesión de Market Test, registrar `bench.started`, broker vivo, cruces aceptados/rechazados y componente
  de mercado posterior. El banco offline mide el código, no demuestra rendimiento en el servidor real.
- La demo para jueces debe mostrar una oportunidad con fuente y tick, la oferta estructurada, la liquidación y el
  resultado medido. Si no hay liquidación, explicar la necesidad y la fricción observada.
