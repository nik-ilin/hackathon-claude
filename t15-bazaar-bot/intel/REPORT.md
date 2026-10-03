# Inteligencia de mercado — 2026-10-03 11:42:25

## Bucle de automejora (t15)

- Distancia al 1º: 13.14 · tendencia últimas instantáneas: 0.25
- Palanca 1: negociación: captura con dealers 0.14 vs 0.75 del líder; tratos/hilos 4/35 → abrir más bajo y pasos cortos (ver dealer_params)
- Palanca 2: market-making: 1 trato en nuestro mercado propio (líderes con 1-2 tratos sacan 12.5 vs puesto 7.5)
- Parámetros aprendidos del mejor trato por dealer/artículo en `recommendations.json` → `self_improve.dealer_params`

## Cambios en mercados (ticks 360→420)

- v07 (t10) tratos 1→2
- v14 (t14) comisión 300→0bps
- v14 (t14) tratos 0→1
- v10 (t05) tratos 1→2
- v17 (t17) comisión 300→0bps
- rastro (world) tratos 22→30

Ronda: Saturday · Gran Vía

## Clasificación y perfil de equipos

| # | Equipo | Score | Δronda | Neg | MM | Nivel | Álbum | Dealer: tratos/hilos | Captura rango | Apertura vs dealer | Paso | P2P compra/venta | Compra vs mercado | Venta vs mercado | Le interesa | Le sobra | Mercado propio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Team 14 (t14) | 29.05 | 10.99 | 17.19 | 11.86 | 2 | 30 | 5/11 | 0.75 | 0.42 | 1.5 | 5/1 | 1.0 | 0.62 | LAT, RET | MAL, SAL, LAV | v14 0bps auto trades=1 |
| 2 | Team 13 (t13) | 27.72 | -2.22 | 24.39 | 3.33 | 3 | 30 | 16/62 | 0.06 | 0.36 | 1.0 | 2/4 | 1.03 | 0.91 | RET, LAT | SAL, LAV, MAL | v03 0bps board trades=0 |
| 3 | Team 18 (t18) | 26.66 | 7.5 | 19.16 | 7.5 | 2 | 31 | 10/13 | 0.75 | 0.58 | 1.5 | 3/3 | 1.12 | 1.1 | SAL, RET | MAL, LAV, LAT | v18 300bps auto trades=0 |
| 4 | Team 12 (t12) | 25.76 | -2.06 | 14.02 | 11.74 | 2 | 35 | 12/23 | 0.5 | 0.48 | 2.0 | 3/4 | 0.88 | 1.1 | LAV | SAL, MAL, LAT | v02 0bps board trades=2 |
| 5 | Team 10 (t10) | 24.84 | 4.09 | 12.34 | 12.5 | 2 | 25 | 5/11 | 0.32 | 1.98 | 2.0 | 0/3 | None | 1.24 | RET | SAL, MAL, LAV | v07 0bps board trades=2 |
| 6 | Team 2 (t02) | 23.95 | 17.12 | 16.45 | 7.5 | 2 | 27 | 19/41 | 0.26 | 2.0 | 1.0 | 5/8 | 0.75 | 1.11 | RET, LAV, SAL |  | v04 0bps auto trades=0 |
| 7 | Team 1 (t01) | 23.13 | 14.74 | 15.63 | 7.5 | 2 | 27 | 3/4 | 0.21 | 0.59 | 1.0 | 8/3 | 0.99 | 1.03 | MAL | LAT, SAL, LAV | v08 300bps auto trades=0; v19 0bps board trades=0 |
| 8 | Team 4 (t04) | 22.72 | 2.23 | 15.22 | 7.5 | 2 | 32 | 11/21 | 0.37 | 0.61 | 2 | 3/5 | 1.0 | 0.95 | LAV, LAT | MAL, SAL, RET | v05 0bps board trades=0 |
| 9 | Team 5 (t05) | 22.66 | 2.67 | 15.16 | 7.5 | 2 | 33 | 13/17 | 0.37 | 0.58 | 1.25 | 3/2 | 1.0 | 0.94 | RET | LAT, SAL, LAV | v10 0bps auto trades=2 |
| 10 | Team 17 (t17) | 20.68 | -1.35 | 13.18 | 7.5 | 2 | 29 | 3/14 | 1.0 | 0.25 | 2.0 | 1/1 | 1.33 | 1.19 | MAL | LAV, LAT | v17 0bps auto trades=0 |
| 11 | Team 16 (t16) | 19.96 | 13.12 | 12.46 | 7.5 | 2 | 26 | 16/19 | 0.5 | 0.4 | 1 | 1/1 | 0.92 | 1.11 | SAL, LAT | LAV, MAL | v16 300bps auto trades=0 |
| 12 | Team 9 (t09) | 19.53 | 10.01 | 12.03 | 7.5 | 2 | 33 | 13/20 | 0.4 | 0.62 | 2.0 | 5/1 | 1.0 | 0.88 | RET, SAL, LAV | LAT | v12 0bps auto trades=0 |
| 13 | Team 3 (t03) | 17.76 | 3.16 | 10.26 | 7.5 | 2 | 27 | 7/8 | 1.0 | 0.49 | 1.0 | 1/1 | 0.88 | 1.33 | LAV, SAL, LAT | MAL | v09 300bps auto trades=0; v20 0bps board trades=0 |
| 14 | Team 6 (t06) | 17.74 | 5.07 | 8.84 | 8.9 | 2 | 27 | 0/5 | None | 2.0 | 2.5 | 3/6 | 1.1 | 0.9 | SAL, LAV, RET | MAL, LAT | v01 0bps board trades=1 |
| 15 | Team 15 (t15) | 15.91 | 5.58 | 8.41 | 7.5 | 2 | 34 | 4/35 | 0.14 | 0.66 | 2.0 | 4/5 | 1.19 | 0.9 | RET, SAL, MAL | LAT, LAV | v15 300bps auto trades=0 |
| 16 | Team 8 (t08) | 15.02 | -2.59 | 7.61 | 7.41 | 2 | 27 | 9/15 | 0.82 | 1.59 | 2.0 | 1/0 | 1.12 | None | SAL, MAL, LAV | RET, LAT | v06 0bps board trades=0 |
| 17 | Team 7 (t07) | 10.49 | 1.53 | 2.99 | 7.5 | 2 | 29 | 1/7 | 0.0 | 2.4 | 3.0 | 0/0 | None | None |  | MAL, SAL | v11 300bps auto trades=0 |
| 18 | Team 11 (t11) | 7.5 | 7.5 | 0.0 | 7.5 | 2 | 13 | 0/0 | None | None | None | 0/0 | None | None |  |  | v13 300bps auto trades=0 |

## Dealers: cómo ceden

| Dealer · modo · artículo | Hilos | Tratos | Apertura | Final (mediana) | Trato mediano | Mejor trato | Lo consiguió | Cesión/ronda | Rondas |
|---|---|---|---|---|---|---|---|---|---|
| abuela · ? · ? | 13 | 9 | 22.5 | 20.0 | 21 | 24 | t18 (abre None, paso None, 0 r.) | 0.33 | 2 |
| abuela · buy · common | 46 | 31 | 12.0 | 8 | 9 | 8 | t03 (abre 4, paso 1, 4 r.) | 0.67 | 3.0 |
| abuela · buy · sobre_barrio | 32 | 6 | 30 | 21 | 21.0 | 19 | t13 (abre 10, paso 1.0, 7 r.) | 1.5 | 4.0 |
| abuela · buy · uncommon | 55 | 29 | 29.0 | 24.0 | 23 | 10 | t02 (abre 22, paso 2, 2 r.) | 1.4 | 3 |
| abuela · sell · ? | 25 | 20 | 5.0 | 5 | 6.0 | 22 | t09 (abre 30, paso None, 1 r.) | 0.0 | 5 |
| abuela · sell · common | 29 | 10 | 5 | 6.0 | 6.0 | 6 | t10 (abre 15, paso 1, 4 r.) | 0.0 | 3 |
| abuela · sell · lote2 | 2 | 1 | 10.0 | None | 10 | 10 | t08 (abre 15, paso None, 1 r.) | 0.25 | 1.5 |
| abuela · sell · lote3 | 1 | 0 | 15 | 16 | None | None | t17 (abre 24, paso 1.0, 5 r.) | 0.2 | 5 |
| abuela · sell · uncommon | 5 | 3 | 12 | 13 | 15 | 17 | t02 (abre 24, paso 1.5, 5 r.) | 0.5 | 4 |
| chato · ? · ? | 7 | 3 | 23.0 | 22.5 | 28 | 29 | t03 (abre 18, paso None, 1 r.) | 0.5 | 1 |
| chato · buy · rare | 20 | 11 | 97.0 | 89 | 87 | 84 | t09 (abre 60, paso 4.0, 7 r.) | 1.17 | 5.0 |
| chato · buy · sobre_plata | 1 | 1 | 188 | None | 181 | 181 | t08 (abre 140, paso 10, 4 r.) | 1.75 | 4 |
| chato · buy · uncommon | 49 | 11 | 33 | 31.0 | 31 | 26 | t13 (abre 9, paso 3, 8 r.) | 0.25 | 4 |
| chato · sell · ? | 10 | 5 | 13.0 | 14 | 14 | 31 | t02 (abre 27, paso None, 1 r.) | 0.0 | 3.0 |
| chato · sell · lote2 | 1 | 1 | 26 | None | 26 | 26 | t08 (abre 36, paso None, 1 r.) | 0.0 | 1 |
| chato · sell · rare | 1 | 0 | 39 | None | None | None | t08 (abre 150, paso 15.0, 3 r.) | 0.0 | 3 |
| chato · sell · uncommon | 17 | 4 | 13 | 15 | 14.0 | 14 | t03 (abre 21, paso 2, 4 r.) | 0.0 | 4 |
| pilar · sell · ? | 2 | 1 | 19.0 | 19 | 19 | 19 | t13 (abre 49, paso 1, 8 r.) | 0.19 | 5.0 |
| pilar · sell · uncommon | 10 | 1 | 16 | None | 19 | 19 | t13 (abre 31, paso None, 1 r.) | 0.0 | 1.0 |

## Market Test — puesto gratuito = 7.5

- t10: 12.5 (SUPERA al puesto) · v07 0bps board trades=2
- t14: 11.86 (SUPERA al puesto) · v14 0bps auto trades=1
- t12: 11.74 (SUPERA al puesto) · v02 0bps board trades=2
- t06: 8.9 (SUPERA al puesto) · v01 0bps board trades=1
- t08: 7.41 (por DEBAJO del puesto) · v06 0bps board trades=0
- t13: 3.33 (por DEBAJO del puesto) · v03 0bps board trades=0
- evento tick 201: {"session": 1, "name": "The Market Test: every venue gets the same synthetic book", "venues": ["v01", "v02", "v03", "v04", "v05", "v06", "v07", "v08", "v09", "v

## Actividad con nuestra clave en esta ronda (desde tick 160)

- Hilos con dealers: 21 · tratos cerrados con dealers: 3
  - abuela: {"buy": {"card": "RET-06"}} ×8
  - chato: {"buy": {"card": "RET-06"}} ×4
  - abuela: {"buy": {"card": "RET-01"}} ×2
  - abuela: {"buy": {"card": "RET-08"}} ×2
  - chato: {"buy": {"card": "RET-08"}} ×2
  - chato: {"buy": {"card": "RET-09"}} ×2
  - chato: {"buy": {"card": "MAL-08"}} ×1
- Ofertas publicadas en None: 15
- Ofertas publicadas en rastro: 15
- Ofertas publicadas en v01: 11
- Ofertas publicadas en v02: 49
- Ofertas publicadas en v03: 6  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v04: 7
- Ofertas publicadas en v05: 6
- Ofertas publicadas en v06: 7
- Ofertas publicadas en v07: 8
- Ofertas publicadas en v10: 5
- Ofertas publicadas en v11: 3
- Ofertas publicadas en v12: 12
- Ofertas publicadas en v13: 1
- Ofertas publicadas en v14: 13  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v19: 13
- Ofertas publicadas en v20: 16

## Precios P2P liquidados (mediana por carta suelta)

- common: 8 P
- rare: 74.0 P
- uncommon: 21 P

## A quién vender cada set

- LAT: t16, t13, t04, t14, t03
- LAV: t02, t04, t06, t08, t09, t12, t03
- MAL: t01, t17, t08
- RET: t02, t13, t06, t09, t14, t18, t10, t05
- SAL: t16, t02, t06, t08, t09, t18, t03

Líderes (no operar en sus mercados): t14, t13, t18
