# Team 15 · The Bazaar — agente de negociación y mercado

> *Scores come only from value created, never from activity.*
> Nuestro agente está construido alrededor de esa frase: **ninguna operación sale sin demostrar que crea valor**.

## Arquitectura

```
                 ┌──────────── intel/collector.py (sin clave) ────────────┐
                 │  feed (500 ev.) · leaderboard · procedencia de cartas  │
                 └──────────────────────────┬─────────────────────────────┘
                                            ▼ intel/market.db
 clock/me/duels/threads/offers ──► bazaar/agent.py ──► estrategias (funciones puras: estado → Intents)
                                     │                  ├─ strategy/duels.py   silencio estratégico + aceptación escalonada
                                     │                  ├─ strategy/dealers.py ladder: regateo hasta `final:true`
                                     │                  └─ strategy/p2p.py     comercio a valores privados
                                     ▼
                              bazaar/values.py   valor privado: marginales 1/0,25/0,10 + bonus de página (25 %)
                                     ▼
                              bazaar/executor.py ÚNICO punto que escribe en el servidor:
                                 · guardarraíl de valor (P2P Δ ≥ +2 P, dealers Δ ≥ 0; falla cerrado)
                                 · árbitro de la única aceptación por tick (duelos > finales de dealer > P2P)
                                 · límites de `clock.limits` · kill switches · dry-run · diario SQLite
 bazaar/broker.py ── mercado propio `board`, 0 %: suelo = cruce del puesto gratuito; v2 estima límites ocultos
 ops/ ── launchd (KeepAlive) para agent/broker/collector + caffeinate
```

## Ideas clave
1. **Guardarraíl de valor privado.** Cada intención lleva su Δ de valor (ganancia − pérdida − caja − comisión), calculado con `/api/me/value` en vivo. El viernes compramos sobres a 30 P que nos valían ~10: esa clase de error ya no puede ocurrir.
2. **Bonus de página verificado** (25 % del valor base de la página): una carta de una página completa nunca se vende por menos de lo que la página pierde.
3. **Silencio estratégico en duelos.** Los rivales ceden solos; cada ronda de charla encoge la tarta. En la repetición de los 24 duelos del viernes la política captura el **98 %** del margen máximo disponible (frente a 0 % del bot anterior y 77 % de una variante "aceptar pronto").
4. **Árbitro de aceptaciones.** Solo hay una aceptación por tick para todo el equipo: los duelos de una ola se escalonan por pendiente de concesión.
5. **Ladder bien entendido.** Puntúa la parte del rango del dealer que capturas: regateamos con pasos calibrados por dealer (de los equipos que mejor lo hicieron) hasta su oferta final.
6. **No alimentar a los líderes.** Nunca operamos en los mercados de los equipos en cabeza (su puntuación de market-making crecería con nuestro flujo).
7. **Esquema verificado.** Todo campo leído del API se comprobó contra respuestas reales grabadas (la causa raíz de las pérdidas del viernes).

## Operación
```bash
cp state/.env.example state/.env      # BAZAAR_KEY=... (nunca en el código)
ops/run.sh agent                      # dry-run por defecto; en vivo: AGENT_ARGS="" ops/run.sh agent
touch state/STOP                      # parada total inmediata · state/STOP_TRADING: solo dealers/P2P
python3 tests/test_guard.py           # guardarraíl, copias, páginas, árbitro, regresiones
```
Plan completo y auditorías: [`PLAN.md`](PLAN.md).
