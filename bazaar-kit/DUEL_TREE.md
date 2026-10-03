# Árbol de decisión para el agente de duelos

Este árbol expone en JSON la política **vigente de `main`**. La función
`duel_tree.plan(live, tick, events)` usa `duels.duel_candidates` para elegir
acciones y explica también cuándo esperar. `duel_runner.py` la usa en modo
análisis y ejecución; conserva el bloqueo compartido, la cadencia de llamadas,
la gestión de escrituras ambiguas y el máximo de una aceptación por tick.

```text
¿Duelo vigente y queda tiempo?
  no → esperar / ninguna escritura
¿Negocia días?
  sí → esperar: utilidad de días sin verificar con el servidor
¿Oferta del rival dentro del límite propio?
  sí → ¿margen alto, oferta empeora, rival se planta o cierre próximo?
        sí → aceptar (una aceptación por tick; las demás se difieren)
        no → esperar: el rival puede mejorar sin que hablemos
  no → ¿quedan ≤5 ticks y aún no ofertamos?
        sí → abrir una oferta propia según `duels.PARAMS`
        no → esperar
```

El árbol usa el historial de **24 duelos propios de práctica** incluido en
`duels_fixture_practice.json`: en varios, el rival mejoró su oferta mientras
nuestro equipo callaba y `rounds` siguió en cero. Los umbrales de `duels.py`
se eligieron sobre esa misma muestra; la repetición de 562/574 P es
retrospectiva y no demuestra rendimiento futuro. Por eso el árbol no aprende
automáticamente nuevos parámetros ni acepta una primera oferta positiva sólo
por ser positiva.

## Valor del feed público

Se cuentan eventos `duel.closed` deduplicados: cierres, `no_deal` y sesiones.
Ese resumen acompaña cada decisión como **contexto descriptivo**. El feed
público no expone las conversaciones privadas de un rival con alias ni su
límite; sus precios de mercado y negociaciones con dealers tampoco revelan el
valor privado del escenario de duelo. `no_deal` puede deberse a más de una
causa. Por ello una tasa agregada no altera decisiones individuales.

El feed guarda sólo una ventana reciente. Para tener contexto histórico, dejar
`feed_watch.py` o `feed_stream.py` recogiendo en
`data/feed_history.jsonl`. `duel_runner.py` lee ese archivo en cada tick y
lo ignora si no existe. El reloj del API aporta el tick actual y el límite
`accepts_per_team_per_tick`; el ejecutor limita a una aceptación, tal como
figura ahora en `/api/clock`.

## Uso por otro agente

```bash
python3 duel_runner.py                # análisis: JSON por decisión, sin escribir
python3 duel_runner.py --execute      # sólo con clave y bloqueo exclusivo
```

Cada registro tiene `action` (`accept`, `offer`, `wait` o `defer`), `path`,
`reason` y `feed`. `wait` y `defer` nunca se envían al API. También puede
importarse `duel_tree.plan` directamente en otro agente. `--feed-file RUTA`
permite leer un historial JSONL distinto.

La API autenticada sigue siendo la fuente de los duelos vivos. El árbol no
usa el `duels.py` alternativo del PR #2: ese módulo se escribió antes de que
los duelos de práctica y el ejecutor actual se fusionaran en `main`.
