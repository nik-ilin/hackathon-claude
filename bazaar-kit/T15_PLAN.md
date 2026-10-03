# Plan de t15 (decisiones del 3 de octubre, tick ~713)

Todo lo de esta rama es opt-in salvo el preset `./run.sh t15`. Quien lance el bot debe ser **una sola terminal**:
dos coordinadores con la misma clave duplican acciones.

## Arranque (en este orden)

```bash
cd bazaar-kit
./run.sh t15                                  # 1. análisis: comprobar que la primera acción es «abrir con chato card:RET-10»
./run.sh t15 --execute --ticks 120            # 2. operar (repetir al terminar)
./run.sh celestina --execute --loop           # 3. otra terminal (STARTER_BROKER_KEY en .env): anuncios de parejas en v15
python3 new_levels.py --watch 60              # 4. otra terminal: avisa cuando Pícaros / Taller se activen
python3 flagger.py                            # 5. dry run; --execute solo si los candidatos son claros
```

## Decisiones y por qué

| Decisión | Motivo (datos de market.db / dry run en vivo) |
|---|---|
| `--reserve 5` (antes 100) | Con 97–103 P de caja y reserva 100, el bot no podía pagar nada: caja libre negativa. |
| `--per-card 95` (antes 60) | RET-10 vale 218 P para nosotros (cierra la página RET) y el Chato la vende a 77–91 P. El gasto sigue limitado por el valor privado y el margen. |
| Comprar RET-10 al Chato | Es la primera acción que elige el coordinador en el dry run: ΔU +141 P y un trato más en la escalera del Chato (1/3 → 2/3). |
| No perseguir MAL-09/MAL-10 | Valen 77 P y se venden a 65–98 P: margen casi nulo. |
| No pujar al 80 % del valor en el Rastro | Por debajo del mercado para MAL; para RET-10 supera la caja y revela nuestro valor. |
| `--pilar-sell SAL,LAV:1.25 --allow-last-copy SAL-07` | Llena la escalera de Pilar (peso 3) vendiendo LAV-06/08/10 y SAL-07 al Chato por encima de su valor privado; LAV y SAL están lejos de completar página. |
| `--deny-teams t14,t12,t10,t18`, `--no-rival-venues` | No regalar valor ni market-making a los cuatro primeros. |
| No abrir venue `board` (`venue_switch.py`) | Faltan ~185 P, el puesto ya da 0,5 de bench y abrir hace perder el puesto para siempre. |
| No vender cartas RET | Es nuestro set de mayor afinidad (1,6) y el que más buscan t12, t10 y t05. |
| Rechazar los cambios de t13/t08/t05 que piden RET-02/03/04 | Romperían la página RET justo cuando RET-10 la cierra. |

## Puntuación (recordatorio de RULES.md)

Negociación 30 = duelos + escalera (mejores 3 tratos por dealer y nivel, niveles altos pesan más) + valor ganado
en tratos con **otros equipos** a valores privados. Las compras a dealers solo puntúan por la escalera; el valor
privado de RET-10 cuenta como protección de página y para futuros tratos.

## Noticias y nuevos niveles

Todo es solo lectura o dry run salvo `--execute`; sin flags nada cambia.

```bash
python3 news_watch.py --calibrate --db ../../intel/market.db   # fiabilidad por fuente con datos pasados (offline)
python3 news_watch.py --json noticias.json                     # en vivo: noticias clasificadas + ventas sugeridas
./run.sh t15 --news-sell --news-db ../../intel/market.db       # coordinador: abre venta al vendedor con demanda viva
python3 new_levels.py                                          # taller: 3 sobrantes de UNA rareza + EV frente a pérdida
python3 new_levels.py --workshop --workshop-margin 2           # dry run del envío al taller (solo si EV > pérdida + 2)
```

| Hallazgo (market.db hasta t746) | Consecuencia |
|---|---|
| Radio t403 «El Chato busca raras de Malasaña»: su menú ganó `buys rare ["MAL"]` (visto t467/t508, sin él en t265/t590) | Las noticias ciertas dejan huella en `/api/dealers`: `--news-sell` actúa si la fila está o si la fuente es fiable y estamos en su ventana. |
| Tablón t499 «el Chato regala una legendaria»: ningún `gift.given` suyo | El Tablón es rumor (fiabilidad 0,33): nunca actúa sin confirmación en el menú. |
| Boletín t643 «sobres de la Abuela en una hora»: pendiente hasta t763 | Revisar `sobres sin abrir` en `new_levels.py`. |
| «One hour» = 120 ticks (30 s/tick; el domingo 15 s → 240) | La ventana se calcula con `tick_seconds` del reloj. |
| Taller: t05, t12, t18, t16, t08, t10 ya convierten comunes en poco comunes | Solo si la media del valor privado de las poco comunes publicadas supera lo que perdemos + margen. |

### Chato v3 y fiebre de Salamanca (preset `./run.sh t15` ya incluye `--chato-mirror on --fever-priority`)

| Hallazgo | Ajuste |
|---|---|
| Chato v3 (persona.updated t583) refleja nuestra concesión: hilos 989/998/1005 abrió a 97, repitió 97 mientras seguíamos en 87, cedió 1 («You moved little, so did I») y la compra a 96 no movió ladder_points. | `--chato-mirror on\|auto\|off` (con `--ladder-calibrated`): cada ronda un paso real = máx(paso, ⌈hueco/4⌉), nunca pasos de 1 ni repetir precio; el techo económico sigue mandando. `auto` detecta v≥3 por `/api/dealers`, `persona.updated` del feed (se recuerda en el ledger) o la frase «so did I» en el hilo. |
| `/api/schedule`: «Salamanca fever: Doña Pilar pays 25 % over book for Salamanca» a las 9,15 h de juego (≈18:03 hora local), «The fever breaks» a las 11,15 h (≈20:03). | `--fever-priority`: mientras dura, las ventas SAL a Pilar pasan delante; en la hora de juego previa (`--fever-wait 1`) se esperan. `news_watch.py` muestra la fiebre y, activa, propone qué SAL vender y a cuánto (book × 1,25 frente a valor privado). |
