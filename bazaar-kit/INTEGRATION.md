# Integración de ramas y memoria del agente

El coordinador existente sigue siendo la única autoridad operativa. La integración añade memoria persistente sin sustituir su valoración, protección de cartas, presupuesto ni bloqueo de procesos.

## Ramas revisadas

| Rama | Integración | Límites encontrados |
|---|---|---|
| `feed-oracle` (`9637423`) | Feed, rivales, señales, playbook, dashboard y herramientas auxiliares | Las cotizaciones no acreditan un suelo; cancelar no demuestra rechazo; las simulaciones retrospectivas no validan respuestas contrafactuales. |
| `t15-bazaar-bot-pr` (`fd9a793`) | Carpeta independiente `t15-bazaar-bot/`, auditorías y modelos comparables | El ejecutor alternativo abre mercados automáticamente, usa otra contabilidad y no comparte el bloqueo del coordinador. Sus entradas de agente quedan en análisis. |
| `t15-bazaar-bot` (`2e9839b`) | Mismo árbol que la carpeta de la rama portable: no se duplica | Historia Git independiente; no se mezclan dos copias del mismo código. |
| `duelos-modulo` | Se mantiene el módulo actual `duels.py` y su ejecutor protegido | La política distinta de feed-oracle queda como `oracle_duels.py`, con pruebas separadas; `duels_run.py` no permite ejecución real. |

No se adoptan automáticamente la contraoferta tras `final` basada en dos ejemplos, la apertura de mercado a una hora fija ni el veto permanente a ciertos equipos. Son hipótesis históricas, no mejoras demostradas para nuestro estado actual. Tampoco se instala ningún supervisor ni se inicia ningún bot al hacer el merge.

## Uso

Desde `bazaar-kit`:

```bash
./run.sh memory --intel 6 --show 8
```

Analiza y conserva evidencia. Usa los mismos argumentos que `coord`. Para operar, detener el proceso anterior y añadir `--execute` con los límites deseados. Comparte `data/agent.lock`; no ejecutar otro agente de equipo a la vez.

```bash
./run.sh memory --execute --ticks 30 --max-spend 0 --reserve 100 --margin 2
```

`--max-spend 0` limita nuevas compras; no es un filtro de solo ventas ni cancela compromisos anteriores. El coordinador puede generar trueques y otras acciones permitidas. Para publicar únicamente duplicados sigue disponible `./run.sh market --execute --action list ...`.

El recolector público es independiente y no envía operaciones:

```bash
python3 feed_watch.py --watch
python3 feed_watch.py --report
python3 agent_memory.py
```

El adaptador importa incrementalmente `data/feed_history.jsonl`, incluyendo lo recogido mientras no corría el coordinador. Un historial ausente no impide operar; eventos perdidos antes de empezar a recoger no se pueden reconstruir.

## Qué recuerda y cómo se utiliza

- `data/agent_memory.sqlite3`: eventos con ID único, transiciones de acciones propias y observaciones por tick; persiste entre reinicios. SQLite WAL y transacciones evitan duplicar observaciones al repetir consultas.
- `data/agent_memory_report.json`: comparables por dealer/carta/sentido, precios liquidados separados de cotizaciones, operaciones propias observadas y advertencias de cobertura.
- `memory_coordinator.py`: adaptador temporal de las funciones públicas de planificación/ciclo. No modifica `coordinator.py`, que puede mantenerse independientemente. Restaura las funciones al salir.
- El feed histórico reciente llega al análisis y al historial de ejecuciones ya utilizado por `market_intel`; el modelo existente vuelve a estimar precios con esas observaciones. La ventana predeterminada es 240 ticks, configurable con `--history-window`. Las ofertas disponibles, comisiones e inventario siempre proceden de la instantánea actual.
- La memoria anota comparables en candidatas de dealers, sin reemplazar sus límites económicos ni forzar un precio histórico.
- Las acciones enviadas siguen pendientes hasta reconciliación. Un ID repetido no es una muestra nueva. `settled` y `quoted` se presentan por separado; el informe no convierte efectivo bruto o una subida del ranking en beneficio atribuible.
- `signals.py` conserva cancelaciones como retiradas observadas y las excluye del denominador de rechazo. Sus ratios sobre liquidaciones inferidas siguen siendo descriptivos y condicionados; no son probabilidades calibradas de venta.
- No se cargan ni modifican parámetros automáticamente a partir de los resultados de simulación. Aprender aquí significa ampliar evidencia y actualizar estimaciones dentro de las protecciones existentes, no reescribir código ni relajar límites.

La memoria falla de forma degradada: un fallo de disco o un registro mal formado produce un aviso y el coordinador continúa con su instantánea actual. No se guardan claves de equipo/broker ni respuestas privadas completas en la memoria. `data/` permanece fuera de Git.

## Verificación y límites

Validación de la integración aislada de los cambios concurrentes: **518 pruebas unittest, una omitida por falta de archivo histórico local**, y **30 pruebas pytest del bot alternativo**.

Pruebas offline: regresiones existentes, suites del oráculo, deduplicación/reinicio, importación incremental y líneas parciales, causalidad temporal, separación cotización/liquidación, redacción de claves, conservación del estado vivo y recuperación al fallar la memoria. El bot alternativo tiene una suite separada (`BAZAAR_KEY=offline-test python3 -m pytest -q tests` desde su carpeta).

Prueba de lectura pública durante la integración: 500 eventos recogidos, ticks 291–318; importados a SQLite y reimportados con 0 duplicados añadidos. La comprobación autenticada del coordinador encontró `rate_limited`; no se ha validado ejecución real ni se han enviado operaciones.

Los cambios concurrentes de otro agente en negociación/capital/rendimiento están fuera de este commit y de esta revisión. Deben completar sus propias pruebas antes de publicar o ejecutar esa combinación. La revisión de estas ramas no demuestra que sea la política óptima ni garantiza más puntos; proporciona una base más observable y reproducible para compararla.
