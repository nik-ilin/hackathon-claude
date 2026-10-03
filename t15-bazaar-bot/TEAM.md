# Equipo 15 — cómo trabajamos dos personas (y sus agentes)

## Regla nº 1: una clave, un proceso que escribe
Las reglas dicen *one team, one key*. Con la misma clave, dos bots se pisan entre sí: la única aceptación por tick, las conversaciones con los dealers (una por dealer) y la caja. El viernes perdimos puntos así (Antigravity operaba a la vez).
- **Solo `com.team15.agent` (en este Mac) hace POST con la clave de equipo.** Cualquier otro código, incluido el del repo del compañero, corre en modo lectura o `--dry-run`.
- Una idea del compañero entra en el agente como estrategia (función pura `estado → Intents`) que pasa por el ejecutor: el guardarraíl y el árbitro la cubren.
- El detector de segundo agente activa `state/STOP_TRADING` si aparece un hilo nuestro que no abrió el agente.

## Reparto de roles
| | Persona A — operación (este Mac) | Persona B — compañero |
|---|---|---|
| Sábado 09–13 | Vigilar la apertura, duelos I (11:30), apertura del mercado (~11:50) | Revisar su código frente al nuestro; proponer qué portar |
| Sábado 13–18 | Ajustes con datos reales (Claude revisa cada 10 min) | **Jueces (40 %)**: demo, pitch, gráficas |
| Sábado 18–21 | Duelos II (precio + días), Market Test difícil (21:00) | Diario de decisiones legible, panel en vivo |
| Domingo 09–13 | Market Tests de 19 y 21 h (valen ~4× uno del sábado): **sin despliegues** | Ensayo del pitch; post-mortem del viernes |
| Domingo 13–15 | Final de duelos (14:00), congelación | Presentación |

## Flujo con el repositorio
1. Repo común en GitHub (el del compañero o este). `state/`, `logs/` e `intel/*.db` nunca se suben (`.gitignore`); la clave solo en `state/.env`.
2. Cambios en la estrategia: rama → tests (`python3 tests/test_guard.py`, `python3 tests/test_broker.py`) → revisión → merge → `launchctl kickstart -k gui/$(id -u)/com.team15.agent`.
3. Ningún despliegue en los 10 min previos a un Market Test ni durante una oleada de duelos.

## Señales de alarma (cualquiera de los dos puede parar el bot)
`touch state/STOP` (todo) · `touch state/STOP_TRADING` (dealers y P2P) · `python3 ops/report.py` (estado)
