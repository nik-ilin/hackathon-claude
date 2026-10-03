# Plan para el 1.er puesto — sábado 11:00 (tick ~315)

> Documento histórico del tick ~315. Precios, clasificación y diagnóstico no
> describen necesariamente el estado actual. Los «finales» observados no son
> suelos garantizados. No se aplican automáticamente sus parámetros al agente.

Estamos 14.º (15,13). t18 lidera con 29,7 gracias al **ladder**: 10 tratos cerrados con dealers. Nosotros: **ladder 0** (0 tratos en 24 hilos).

## Causa (verificada con `coordinator.py` en modo análisis, sin enviar nada)

```
efectivo 262 P · libre 1 P · reservado en ofertas 161 P
```

- 161 P están bloqueados en pujas pasivas en v02: MAL-09 a 39, MAL-10 a 38 **y** a 35 (duplicada), LAV-09 a 23, RET-06 a 22, LAV-04 a 4.
- Con `--reserve 100` el máximo por carta queda en 1 P. Cada hilo con un dealer se abre y se abandona sin contraofertar («máximo 1 P frente a precio publicado 25 P»).
- **v02 («El Duende») es el mercado de t12, que va 2.º.** Cada trato ahí le suma market-making a un rival directo. Las ventas en v04 alimentan a t02, que va 3.º.

## Arreglo inmediato: solo parámetros, sin tocar código

```bash
cd bazaar-kit
./run.sh coord --execute --ticks 120 --reserve 40 --max-posts 0 --max-spend 120 --duende-venue rastro
```

| Parámetro | Efecto |
|---|---|
| `--reserve 40` | Libera 60 P para los dealers. Comprobado: «libre 61 P» y se selecciona «abrir con abuela RET-06». |
| `--max-posts 0` | No publica pujas nuevas que vuelvan a bloquear la caja. Las que ya están caducan solas entre los ticks 335 y 358. |
| `--max-spend 120` | Margen para unos 5 tratos con dealers en la ronda (3 mejores por nivel cuentan en el ladder). |
| `--duende-venue rastro` | Si más adelante se vuelve a publicar, que sea en El Rastro, no en el mercado de t12. |

**Duelos I (~12:00)** con el `duel_runner` ya integrado en `main`, igual que estaba previsto.

## Por qué esto es lo que más puntos da

- **Ladder:** mide la parte del rango del dealer que se captura, no el precio. RET poco común a la Abuela (abre 29, final ~22) vale 40 P para nosotros, así que es ladder y valor a la vez.
- **Política de dealers del coordinador:** con la Abuela abre al 65 % y cierra el 40 % de la brecha. En modo score no acepta el precio de apertura, así que cada cierre puntúa.
- **Market-making:** el puesto gratuito ya da 6,73. Publicar en mercados de líderes solo les suma a ellos.

## Siguientes pasos (cuando el ladder ya funcione)

1. Más tratos de RET con la Abuela: RET-08, y RET-01/03/04/05 a menos de 10.
2. Vender comunes duplicadas a la Abuela a 6 (su final). También cuenta para el ladder.
3. Nivel 3 (Pilar, ya desbloqueado por t13): abrir hilos en cuanto aparezca en `unlocked`.
