# Duelos · módulo complementario

Cubre el hueco que señala `ARCHITECTURE.md` («No existe módulo de duelos»). **No modifica ningún archivo existente**: son cuatro archivos nuevos.

| Archivo | Papel |
|---|---|
| `duels.py` | Política pura, sin red: `duel_candidates(duels, tick)` devuelve candidatas `duel_accept` / `duel_say` ordenadas por `score`; `margin()`; `replay()` para medir con duelos terminados. |
| `duel_runner.py` | Proceso que **solo** juega duelos. Análisis por defecto; `--execute` para enviar. Reajusta sus parámetros al terminar cada oleada. |
| `test_duels.py` | 7 pruebas offline (`python3 -m unittest test_duels`). |
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
5. Precio + días: los días suman utilidad con `your_days_weight`, pero **nunca** justifican un precio fuera de límite.

## Resultado en repetición (datos reales)
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
```
- `other_processes()` del coordinador no lo detecta como conflicto (solo busca sus propios nombres).
- Solo hay una aceptación por tick y equipo: si el coordinador ya la usó, el servidor responde `wait_for_tick` (no cuesta nada) y se reintenta al tick siguiente. Durante una oleada conviene que el coordinador no acepte.
- Integración opcional en el coordinador (decisión vuestra): llamar a `duels.duel_candidates(s["duels"], tick)` desde `candidates()` y mapear `duel_accept` a la clase «aceptar».

Registro de decisiones: `data/duels_log.jsonl` · parámetros aprendidos: `data/duel_params.json`.
