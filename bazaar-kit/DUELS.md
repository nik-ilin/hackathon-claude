# Duelos · módulo complementario

> Para la operación del domingo, usar [DAY3.md](DAY3.md). Este documento conserva la evidencia y los experimentos
> históricos; el preset actual es `./run.sh duels --day3` (análisis) o `./run.sh duels --day3 --execute`.

Cubre el hueco que señala `ARCHITECTURE.md` («No existe módulo de duelos»). **No modifica ningún archivo existente**: son cinco archivos nuevos.

| Archivo | Papel |
|---|---|
| `duels.py` | Política pura, sin red: `duel_candidates(duels, tick)` devuelve candidatas `duel_accept` / `duel_say` ordenadas por `score`; `margin()`; `replay()` para medir con duelos terminados. |
| `duel_runner.py` | Proceso que **solo** juega duelos. Análisis por defecto; `--execute` para enviar. No reajusta parámetros automáticamente. |
| `test_duels.py` | 7 pruebas offline (`python3 -m unittest test_duels`). |
| `duel_sim.py` | Simulador offline de rivales (cedente, firme, mudo, espejo, impaciente) en oleadas de 6 con una aceptación por tick. |
| `test_duel_sim.py` | Pruebas del simulador, de las correcciones del ejecutor y de los modos opt-in. |
| `duels_fixture_practice.json` | Los 24 duelos reales de Team 15 en la sesión de práctica del viernes. |

## Qué puntúa y qué muestran los datos
- Puntúa la **parte de la tarta capturada**; fuera de límite resta; sin trato, 0; la tarta encoge un 6–10 % por ronda de conversación.
- En la práctica, la mayoría de rivales **cede cada tick sin que les contestemos** (122 → 82 en 12 ticks con nuestro límite en 158) y `rounds` quedó en 0 aunque el rival hablara: **callar no encoge la tarta**.
- Hubo 2 ofertas «explosivas» (la primera era la mejor) y un 25 % de rivales que nunca habló.

## Política
1. Dentro de límite y margen ≥ 60 % del límite, o el rival empeora, o se planta 3 ticks ⇒ **aceptar**.
2. El rival sigue cediendo ⇒ **silencio**.
3. Sin oferta rival a 5 ticks del final ⇒ **una** oferta propia (comprador 0,70·L, vendedor 1,30·C).
4. Final de la oleada ⇒ aceptar la mejor oferta dentro de límite, **escalonando** (una aceptación por tick y equipo; un tick de margen más por cada duelo con el mismo deadline).
5. Precio + días: por defecto se omiten (0 puntos). Con `--days` se juegan con la misma política: utilidad = margen de precio + utilidad del día (`your_days_weight` en lista, dict por día, escalar por día o `{"per_day": x}`); el precio debe estar SIEMPRE dentro de límite, el umbral GOOD_SHARE solo cuenta el precio y cada oferta propia lleva `days` (nuestro día preferido; si nos da igual, el del rival).

### Opt-in (desactivados por defecto)
| Flag | Qué hace |
|---|---|
| `--days` | (Introducido en PR #6; aquí se completa la valoración de días.) Juega duelos de precio + días (Duelos II). Registra una vez por duelo el `your_days_weight` crudo y cómo se ha interpretado. |
| `--ladder` | Rival mudo: tras la oferta 0,70·L, escalera 0,80 → 0,88 → 0,94 (un peldaño por tick, nunca fuera de límite). Se detiene si el rival habla. |
| `--probe` | Rival plantado con tiempo: una contraoferta a mitad de camino antes de aceptar. Neutro en el simulador; no recomendado. |
| `--reconcile` | Un timeout en una escritura ya no detiene el ejecutor (antes: SystemExit y se perdían los duelos restantes). |
| `--verify-accept` | Relee el duelo justo antes de aceptar; no acepta si la oferta rival cambió a peor. |

## Simulador (`python3 duel_sim.py`, `--days`)
Captura media de la tarta, 300 oleadas × 6 duelos, semilla 1 (modelo sintético, no el servidor):

| Variante | cedente | firme | mudo | espejo | impaciente | TOTAL |
|---|---|---|---|---|---|---|
| por defecto | 73 % | 36 % | 34 % (64 % trato) | 25 % | 35 % | 41 % |
| `--ladder` | 75 % | 36 % | **53 % (100 % trato)** | 25 % | 35 % | **45 %** |
| `--probe` | 73 % | 32 % | 34 % | 31 % | 36 % | 41 % |
| aceptar ≥ 30 % | 62 % | 36 % | 34 % | 25 % | 38 % | 39 % |
| precio + días sin `--days` | 0 % | 0 % | 0 % | 0 % | 0 % | **0 %** |
| precio + días con `--days --ladder` | 65 % | 35 % | 58 % | 27 % | 34 % | 44 % |

Ninguna variante cierra fuera de límite.

## Resultado en repetición individual retrospectiva (datos de práctica)
| Política | Captura | Fuera de límite |
|---|---|---|
| Sin módulo (lo que ocurrió) | 0 P | 0 |
| Aceptar con 30 % de margen | 443 P (77 %) | 0 |
| **Esta política** | **562 de 574 P (98 %)** | **0** |

## Uso y convivencia con el coordinador
```bash
set -a; source .env; set +a
python3 duel_runner.py              # análisis: muestra lo que haría
python3 duel_runner.py --execute    # juega los duelos vivos
# Duelos I (solo precio):
python3 duel_runner.py --execute --ladder --reconcile --verify-accept
# Duelos II (precio + días):
python3 duel_runner.py --execute --days --ladder --reconcile --verify-accept
```
- El ejecutor comparte `data/agent.lock` con coordinador, mercado y agente de vendedores. **No ejecutarlos simultáneamente**: detener el anterior antes de iniciar duelos. No detiene otros procesos por su cuenta.
- `--execute` envía como máximo una aceptación por tick. El modo análisis no envía operaciones ni reajusta parámetros.
- Las peticiones se espacian 0,3 s; una respuesta ambigua a una escritura detiene el ejecutor para reconciliar antes de reiniciar (salvo con `--reconcile`).
- Robustez (por defecto): un fallo al leer no hace perder el tick; los GET se reintentan (`retries=2`, las escrituras con fallo de red nunca se repiten); el timeout de petición es ≤ medio tick; un duelo ya aceptado no vuelve a gastar la aceptación del tick siguiente; un dato inesperado se registra y no detiene el ejecutor.
- Escalonado: urgentes primero por deadline más próximo; las oleadas con deadlines solapados reservan un tick por cada duelo con oferta aceptable y deadline ≤ el propio.
- `./run.sh duels` carga `.env` y ejecuta `duel_runner.py`; también se puede invocar el script directamente con
  `BAZAAR_KEY` ya cargada.
- La repetición de 562/574 evalúa cada duelo aisladamente, con parámetros elegidos sobre la misma muestra. No reproduce el límite compartido por tick, la reacción contrafactual del rival ni acredita una rentabilidad futura. Por ello `retune()` y la carga de parámetros persistidos no se invocan automáticamente.
- Estas protecciones coordinan procesos de este ordenador; no procesos en otros equipos con la misma clave.

Registro de decisiones: `data/duels_log.jsonl` · parámetros experimentales offline: `data/duel_params.json`.

## Perfiles de bots de la casa (`--profiles`) y logrolling de días (`--logroll`) — OPT-IN

Conviven con `--days`, `--ladder`, `--probe`, `--reconcile` y `--verify-accept`; sin ellos la política no cambia.

| Flag | Qué hace |
|---|---|
| `--profiles` | Perfil por alias (Duelos I): **Rojo** empeora ⇒ aceptar en cuanto la oferta entra en límite; **Noche** ⇒ al primer empeoramiento; **Oro/Luna** ⇒ tras el salto (≥ 12 P en un tick); **Plata/Verde** ⇒ sus mesetas no son plantón, esperar al cierre escalonado. **Mudo** (cualquier alias, 3 ticks sin hablar) ⇒ oferta propia pronto, 0,30 → 0,20 → 0,10 cada 3 ticks (máx. 3; sustituye a la escalera de `--ladder` para mudos). Alias desconocido ⇒ política original. |
| `--logroll` | Con `--days`: infiere el día del rival (el más repetido en sus ofertas), estima su utilidad como −s·distancia y concede días que nos cuestan poco a cambio de precio (o al revés), solo si la ganancia supera 2 P + una ronda de decay. Usa `days_table` (formato desconocido ⇒ solo precio). El precio **nunca** sale del límite. |

Simulador: `python3 duel_sim.py --house --days --session II` (bots con alias y comportamiento de Duelos I, 20 % mudos;
captura media de la tarta, 0 tratos fuera de límite en todas las variantes):

| Variante | II precio + días | II solo precio | III precio + días |
|---|---|---|---|
| por defecto (`--days`) | 40 % | 39 % | 36 % |
| `--ladder` | 43 % | 43 % | 40 % |
| `--profiles` / `--ladder --profiles` | **47 %** | **48 %** | **43 %** |
| `--ladder --profiles --logroll` | 47 % | 48 % | 43 % |

Robustez: con los alias barajados (la casa reasigna perfiles) `--ladder --profiles` sigue ≥ `--ladder` (43–46 % frente
a 42–43 %, 3 semillas); con los rivales genéricos de #7, 41 % → 45 % (solo cambian los mudos). El logroll es neutro: con
días lineales («urgencia de la fecha de entrega») y el rival proponiendo su día, apenas hay tarta extra que compense una
ronda de decay.

## Precio + días: convención de signos (corregida)

`your_days_weight` es un **peso positivo por día**; el signo lo da el rol (`days_meaning`): comprador «each delivery day *costs*
you this much cash» → −peso·días; vendedor «each delivery day *adds* this much cash to your side» → +peso·días.
`excedente total = margen de precio ∓ peso·días` y `result = total · (1 − decay)^rondas` (verificado con los 37 duelos
liquidados, `duels_fixture_days.json`). Dos restricciones independientes: precio dentro del límite **y** total > 0.
Antes `margin()` sumaba los días al comprador y se aceptaron tratos con total negativo (5842: +122,8 calculado, −32,8 real).

## Aprendizaje en línea (`duel_runner.py --learn`, `duel_learning.py`, `duel_lab.py`)

Resultado = lo que dice el servidor (`done=true`: status, result, price, days, rounds, mensajes); lo inferido (quién cerró) va marcado como
hipótesis. El modelo (por rol · rival · fase, con decaimiento, agrupación y un duelo = una observación) solo puede **refinar** la política
base: esperar en vez de aceptar si la mejora esperada tiene cota inferior > 0, o cerrar ya si el rival no mejora; con evidencia
insuficiente devuelve la base y lo dice en la razón. Nunca toca límites, la regla total > 0, una aceptación por tick ni el mercado.
Laboratorio: `python3 duel_lab.py audit|summary|replay|sim|all|fetch` (separación temporal, semillas disjuntas, sin enviar nada).
