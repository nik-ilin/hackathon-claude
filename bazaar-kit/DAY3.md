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
   reconciliación y verificación de oferta. `--learn` y `--logroll` siguen opt-in.

## Auditoría del cierre del sábado (tick 1445, API pausada)

Lectura privada de Team 15: **485 P** de caja, **23,91** puntos de servidor, puesto **13/18**,
negociación **16,41/30**, mercado **7,50/30**, `mm_points=0` y **0 tratos en v15**. La caja
observada concuerda aproximadamente con los «~480 P» del equipo; el número operativo es el de
`/api/me` al reabrir, después de ofertas y la asignación del domingo. `duel_points=28,18` es
un acumulado de excedente, no puntos directos de leaderboard. La escalera aporta sólo `0,27`
en el desglose privado. El juego está cerrado hasta el **domingo 4 de octubre a las 09:00 de
Madrid** según `/api/clock`; la API del domingo manda sobre cualquier hora escrita aquí.

Se descargaron **136 duelos terminados** de `/api/duels?done=true`: 34 de práctica sin puntos,
34 de sesión 2 (18 acuerdos) y 68 de sesión 3 (48 acuerdos). Entre los cierres puntuables hay
11 con `result` negativo. El log local del ejecutor no cubre ninguno de esos duelos, por lo que
no se puede atribuir con seguridad qué persona o política los cerró. En replay temporal, entrenando
con 52 y validando/probando con 18+18, `duel_learning` cambió **0 decisiones** frente a la política
base. Esto no prueba que el módulo esté roto: prueba que todavía no hay evidencia de una mejora
de política. El runner conserva toda la cohorte al actualizarse tras cada liquidación y detecta
correcciones del servidor aunque el número de duelos no cambie. `--learn` continúa opt-in hasta
que el laboratorio muestre una mejora fuera de la muestra de entrenamiento.

### Qué memoria usa realmente cada agente

`./run.sh day3` ejecuta `memory_coordinator.py`: guarda el feed y las acciones en
`data/agent_memory.sqlite3`, enriquece el feed que lee el coordinador y añade
comparables a las candidatas. La política de ventas aprendida por `learning.py`
requiere además una campaña `--fast-sales` y una versión de política con
`use_learned_price`; el preset `day3` no activa ninguna de las dos. Por ello,
una base SQLite con eventos no significa que los precios del coordinador se
estén reajustando automáticamente.

`./run.sh duels --day3` ejecuta otro proceso, `duel_runner.py`. Al operar lee
`/api/duels?done=true` y la mediana de acuerdos comparables puede limitar su
oferta inicial (`HISTORY_MIN`). La memoria SQLite anterior no entra en esa
decisión. `--learn` conecta el modelo que puede cambiar aceptar/esperar, pero
queda desactivado en `--day3`. La última prueba temporal real cambió 0
decisiones tanto en validación como en test; activarlo por defecto no tiene
evidencia de mejora. El dashboard distingue ahora un informe guardado de un
proceso en ejecución. En la comprobación del domingo a las 09:14 de Madrid
no estaba ejecutándose ninguno de los dos agentes; la API seguía pausada
en tick 1445, sin duelos vivos.

La auditoría detectó que `--profiles` enviaba las aperturas ante rivales mudos
por una ruta que ignoraba esa mediana histórica. Se corrigió: ahora esa ruta
aplica el límite de los acuerdos comparables y anota `memory.applied` en la
acción. También se excluyen los duelos de práctica y los cierres con resultado
no positivo. Con el histórico disponible hay 10/15 acuerdos útiles como comprador y 8/22 como vendedor para
precio solo/precio+días, respectivamente; la mediana limita la apertura solo
si es menos exigente que el ancla base.

Para volver a comprobarlo antes de Duelos III: refrescar el histórico con
`python3 duel_lab.py fetch`, correr `python3 duel_lab.py replay`, verificar el
proceso operativo en el dashboard y observar en `data/duels_log.jsonl` las
decisiones `learned` y las liquidaciones `reconciled`. Si se prueba `--learn`,
comparar la tasa de cierre, los resultados negativos y las decisiones que
cambió contra la política base antes de mantenerlo activo.

El preset `./run.sh day3` pasa ahora por `memory_coordinator.py`. Esto importa el feed histórico
en SQLite, registra decisiones y entrega comparables al coordinador en cada ciclo, manteniendo
sus límites, valoraciones, protección de páginas y bloqueo. La memoria no reescribe por sí sola
la política: si el dashboard marca memoria atrasada o sin integrar, verificar el proceso antes
de confiar en sus comparables.

### Acuerdo con t05 y grupo de siete

El acuerdo relatado es una **intención del equipo**, no un contrato ni una oferta confirmada en
la API. Preparar listas de necesidades y duplicados para los siete, pero evaluar cada oferta
con el valor marginal privado actual, comisión, pérdida de página, capital reservado y resultado
del servidor. Una recomendación del algoritmo de otro equipo es una pista; no es nuestra
valoración. El preset del domingo no veta a t05, de modo que puede considerar tratos rentables.
t05 estaba tercero con 30,49 puntos al cierre: al elegir entre dos tratos propios de valor
similar, preferir el que no regale más mejora al rival directo. No forzar un intercambio sólo
por pertenecer al grupo.

Para el **market-making** de v15, buscar de forma especial parejas de dos equipos del grupo
que tengan duplicados y faltantes complementarios. Darles un motivo concreto para usar v15:
precio/trueque acordado, cero comisión y una carta que complete página. Las órdenes vistas en
otro venue se tienen que volver a publicar en v15; un anuncio o una coincidencia potencial no
puntúan. Medir `mm_points`, liquidaciones entre terceros y los dos valores netos positivos
confirmados, no el número de mensajes ni de publicaciones. `celestina --leads` genera
invitaciones verificables sin afirmar que ya existe la otra parte.

### Decisión de infraestructura de mercado

Con 485 P y 0 comprometidos, la caja supera el umbral de preflight de `board` (390 P con
reserva de 100 P), pero el cambio requiere broker vivo, autotest, recuperación de clave y
ventana suficiente antes del Market Test. `board` busca mejorar la fracción de 22,5 puntos
del test; la casamentera busca la parte de 7,5 de tratos reales y funciona con el puesto
`auto`. Son dos palancas independientes. No abrir `board` sólo para atraer usuarios: un
broker caído pierde cruces sintéticos y ningún mecanismo crea demanda orgánica por sí solo.

El nuevo fixture de **23 duelos de Duelos II** registra 19 tratos, de ellos **7 con `result` negativo**, y 4 sin
trato. No es una tasa general de la competición: es la muestra guardada de Team 15. El error de signo de días ya se
corrigió en `duels.py` y se comprobó contra resultados del servidor. Para compradores, cada día resta `peso·día`;
para vendedores, suma. Exigir **precio dentro del límite y utilidad total estrictamente positiva**. Un buen precio
puede ser un mal trato al sumar días. `--learn` sólo mejora la política si existen duelos terminados y evidencia
suficiente; el laboratorio local no tenía historia descargada, por lo que no se activa a ciegas.

### Duelos III: regla de decisión durante la oleada

El alias es una pista histórica, no una identidad garantizada. La oferta **estructurada** y su evolución tienen
prioridad sobre el texto del chat y sobre el perfil supuesto. Para cada duelo, registrar rol, límite privado, día,
`deadline_tick`, oferta rival, utilidad total, tendencia y número de aceptaciones que compiten por el mismo plazo.
Una aceptación solo es elegible si el precio queda dentro del límite y el margen con día es estrictamente positivo. No convertir
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
relee antes de aceptar y no reintenta una escritura ambigua sin reconciliar. Leer `duels_fixture_days.json` y correr
`python3 duel_lab.py --history duels_fixture_days.json audit` como revisión previa. Registrar `duel_points` privado antes
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
