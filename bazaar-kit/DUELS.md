# Duelos · módulo complementario

Cubre el hueco que señala `ARCHITECTURE.md` («No existe módulo de duelos»). **No modifica ningún archivo existente**: son cinco archivos nuevos.

| Archivo | Papel |
|---|---|
| `duels.py` | Política pura, sin red: `duel_candidates(duels, tick)` devuelve candidatas `duel_accept` / `duel_say` ordenadas por `score`; `margin()`; `replay()` para medir con duelos terminados. |
| `duel_runner.py` | Proceso que **solo** juega duelos. Análisis por defecto; `--execute` para enviar. No reajusta parámetros automáticamente. |
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
5. Precio + días: se omiten y se registran como pendientes de verificar la fórmula de utilidad.

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
```
- El ejecutor comparte `data/agent.lock` con coordinador, mercado y agente de vendedores. **No ejecutarlos simultáneamente**: detener el anterior antes de iniciar duelos. No detiene otros procesos por su cuenta.
- `--execute` envía como máximo una aceptación por tick. El modo análisis no envía operaciones ni reajusta parámetros.
- Las peticiones se espacian 0,3 s; una respuesta ambigua a una escritura detiene el ejecutor para reconciliar antes de reiniciar.
- No hay comando `./run.sh duels`: cargar `.env` y utilizar `python3 duel_runner.py` como arriba.
- La repetición de 562/574 evalúa cada duelo aisladamente, con parámetros elegidos sobre la misma muestra. No reproduce el límite compartido por tick, la reacción contrafactual del rival ni acredita una rentabilidad futura. Por ello `retune()` y la carga de parámetros persistidos no se invocan automáticamente.
- Estas protecciones coordinan procesos de este ordenador; no procesos en otros equipos con la misma clave.

Registro de decisiones: `data/duels_log.jsonl` · parámetros experimentales offline: `data/duel_params.json`.
