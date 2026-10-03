# Inteligencia de mercado — 2026-10-03 12:10:19

## Bucle de automejora (t15)

- Distancia al 1º: 17.98 · tendencia últimas instantáneas: -1.66
- Desglose propio (/api/me): neg_points 24.2 · duel_points 2.48 · ladder_points 0.033 · mm_points 0.0 · bench_efficiency 0.933 · bench_points 0.5 · negotiating 6.75 · market 7.5 · p2p_points 21.687
- Palanca 1: negociación: captura con dealers 0.14 vs 0.5 del líder; tratos/hilos 4/35 → abrir más bajo y pasos cortos (ver dealer_params)
- Palanca 2: market-making: 1 trato en nuestro mercado propio (líderes con 1-2 tratos sacan 12.5 vs puesto 7.5)
- Parámetros aprendidos del mejor trato por dealer/artículo en `recommendations.json` → `self_improve.dealer_params`

## Cambios en mercados (ticks 382→442)

- v07 (t10) tratos 1→2
- v14 (t14) comisión 300→0bps
- v14 (t14) tratos 0→1
- v10 (t05) tratos 1→2
- v17 (t17) comisión 300→0bps
- v17 (t17) tratos 0→1
- v18 (t18) comisión 300→0bps
- rastro (world) tratos 27→30

Ronda: Saturday · Gran Vía

## Clasificación y perfil de equipos

| # | Equipo | Score | Δronda | Neg | MM | Nivel | Álbum | Dealer: tratos/hilos | Captura rango | Apertura vs dealer | Paso | P2P compra/venta | Compra vs mercado | Venta vs mercado | Le interesa | Le sobra | Mercado propio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Team 12 (t12) | 32.23 | 4.41 | 20.48 | 11.74 | 2 | 36 | 13/24 | 0.5 | 0.48 | 2 | 4/4 | 0.88 | 1.1 | RET | SAL, MAL, LAV | v02 0bps board trades=2 |
| 2 | Team 13 (t13) | 29.02 | -0.92 | 23.53 | 5.49 | 3 | 29 | 20/76 | 0.32 | 0.33 | 1 | 2/4 | 1.03 | 0.91 | RET, LAT | SAL, LAV, MAL | v03 0bps board trades=0 |
| 3 | Team 18 (t18) | 26.84 | 7.68 | 19.34 | 7.5 | 2 | 31 | 10/13 | 0.75 | 0.58 | 1.5 | 3/3 | 1.12 | 1.1 | SAL, RET | MAL, LAV, LAT | v18 0bps auto trades=0 |
| 4 | Team 17 (t17) | 25.68 | 3.65 | 15.43 | 10.26 | 2 | 30 | 4/17 | 1.0 | 0.25 | 3 | 1/1 | 1.33 | 1.19 | MAL | LAV, LAT | v17 0bps auto trades=1 |
| 5 | Team 14 (t14) | 24.58 | 6.52 | 12.73 | 11.86 | 2 | 30 | 5/11 | 0.75 | 0.42 | 1.5 | 5/1 | 1.0 | 0.62 | LAT, RET | MAL, SAL, LAV | v14 0bps auto trades=1 |
| 6 | Team 4 (t04) | 23.68 | 3.19 | 16.18 | 7.5 | 2 | 32 | 12/25 | 0.43 | 0.63 | 2.0 | 3/5 | 1.0 | 0.95 | LAV, LAT | MAL, SAL, RET | v05 0bps board trades=0 |
| 7 | Team 5 (t05) | 23.61 | 3.62 | 16.11 | 7.5 | 2 | 33 | 13/19 | 0.37 | 0.59 | 1.75 | 3/2 | 1.0 | 0.94 | RET | LAT, SAL, LAV | v10 0bps auto trades=2 |
| 8 | Team 1 (t01) | 23.34 | 14.95 | 15.84 | 7.5 | 2 | 28 | 4/5 | 0.18 | 0.59 | 1 | 8/3 | 0.99 | 1.03 |  | LAT, SAL, LAV | v08 300bps auto trades=0; v19 0bps board trades=0 |
| 9 | Team 9 (t09) | 23.01 | 13.49 | 15.51 | 7.5 | 2 | 33 | 13/20 | 0.4 | 0.62 | 2.0 | 5/1 | 1.0 | 0.88 | RET, SAL, LAV | LAT | v12 0bps auto trades=0 |
| 10 | Team 10 (t10) | 22.68 | 1.93 | 10.18 | 12.5 | 2 | 25 | 5/11 | 0.32 | 1.98 | 2.0 | 0/3 | None | 1.24 | RET | SAL, MAL, LAV | v07 0bps board trades=2 |
| 11 | Team 2 (t02) | 22.45 | 15.62 | 14.95 | 7.5 | 2 | 29 | 21/46 | 0.26 | 2.0 | 1.0 | 5/8 | 0.75 | 1.11 | RET, SAL, LAV |  | v04 0bps auto trades=0 |
| 12 | Team 8 (t08) | 21.2 | 3.59 | 13.74 | 7.46 | 2 | 27 | 9/31 | 0.82 | 1.38 | 2.0 | 1/0 | 1.12 | None | SAL, MAL | LAV, LAT, RET | v06 0bps board trades=0 |
| 13 | Team 6 (t06) | 17.11 | 4.44 | 7.28 | 9.83 | 2 | 27 | 1/9 | 0.0 | 1.38 | 2.0 | 3/6 | 1.1 | 0.9 | SAL, RET, LAV | LAT, MAL | v01 0bps board trades=1 |
| 14 | Team 16 (t16) | 15.89 | 9.05 | 8.39 | 7.5 | 2 | 26 | 16/19 | 0.5 | 0.4 | 1 | 1/1 | 0.92 | 1.11 | SAL | LAV, MAL, LAT | v16 300bps auto trades=0 |
| 15 | Team 15 (t15) | 14.25 | 3.92 | 6.75 | 7.5 | 2 | 34 | 4/35 | 0.14 | 0.66 | 2.0 | 4/6 | 1.19 | 0.89 | RET, SAL, MAL | LAT, LAV | v15 300bps auto trades=0 |
| 16 | Team 3 (t03) | 11.72 | -2.88 | 8.11 | 3.61 | 2 | 27 | 7/8 | 1.0 | 0.49 | 1.0 | 1/1 | 0.88 | 1.33 | LAV, SAL, LAT | MAL | v09 300bps auto trades=0; v20 0bps board trades=0 |
| 17 | Team 7 (t07) | 10.49 | 1.53 | 2.99 | 7.5 | 2 | 29 | 1/7 | 0.0 | 2.4 | 3.0 | 0/0 | None | None |  | MAL, SAL | v11 300bps auto trades=0 |
| 18 | Team 11 (t11) | 7.5 | 7.5 | 0.0 | 7.5 | 2 | 13 | 0/0 | None | None | None | 0/0 | None | None |  |  | v13 300bps auto trades=0 |

## Dealers: cómo ceden

| Dealer · modo · artículo | Hilos | Tratos | Apertura | Final (mediana) | Trato mediano | Mejor trato | Lo consiguió | Cesión/ronda | Rondas |
|---|---|---|---|---|---|---|---|---|---|
| abuela · ? · ? | 13 | 9 | 22.5 | 20.0 | 21 | 24 | t18 (abre None, paso None, 0 r.) | 0.33 | 2 |
| abuela · buy · common | 48 | 33 | 12.0 | 8 | 9 | 8 | t03 (abre 4, paso 1, 4 r.) | 0.67 | 3.0 |
| abuela · buy · sobre_barrio | 38 | 6 | 30 | 21 | 21.0 | 19 | t13 (abre 10, paso 1.0, 7 r.) | 1.5 | 4.0 |
| abuela · buy · uncommon | 69 | 31 | 29.0 | 24.0 | 23 | 10 | t02 (abre 22, paso 2, 2 r.) | 1.45 | 3 |
| abuela · sell · ? | 26 | 20 | 5 | 5 | 6.0 | 22 | t09 (abre 30, paso None, 1 r.) | 0.0 | 4.5 |
| abuela · sell · common | 33 | 11 | 5 | 6 | 6 | 6 | t10 (abre 15, paso 1, 4 r.) | 0.0 | 3 |
| abuela · sell · lote2 | 2 | 1 | 10.0 | None | 10 | 10 | t08 (abre 15, paso None, 1 r.) | 0.25 | 1.5 |
| abuela · sell · lote3 | 1 | 0 | 15 | 16 | None | None | t17 (abre 24, paso 1.0, 5 r.) | 0.2 | 5 |
| abuela · sell · uncommon | 5 | 3 | 12 | 13 | 15 | 17 | t02 (abre 24, paso 1.5, 5 r.) | 0.5 | 4 |
| chato · ? · ? | 8 | 3 | 23.0 | 22.5 | 28 | 29 | t03 (abre 18, paso None, 1 r.) | 0.5 | 0.5 |
| chato · buy · rare | 24 | 13 | 97.0 | 89 | 87 | 84 | t09 (abre 60, paso 4.0, 7 r.) | 1.17 | 5.0 |
| chato · buy · sobre_plata | 1 | 1 | 188 | None | 181 | 181 | t08 (abre 140, paso 10, 4 r.) | 1.75 | 4 |
| chato · buy · uncommon | 61 | 11 | 33 | 31 | 31 | 26 | t13 (abre 9, paso 3, 8 r.) | 0.25 | 4 |
| chato · sell · ? | 12 | 6 | 13.0 | 14 | 14.0 | 31 | t02 (abre 27, paso None, 1 r.) | 0.0 | 3.5 |
| chato · sell · lote2 | 1 | 1 | 26 | None | 26 | 26 | t08 (abre 36, paso None, 1 r.) | 0.0 | 1 |
| chato · sell · rare | 1 | 0 | 39 | None | None | None | t08 (abre 150, paso 15.0, 3 r.) | 0.0 | 3 |
| chato · sell · uncommon | 19 | 5 | 13 | 14.5 | 14 | 14 | t03 (abre 21, paso 2, 4 r.) | 0.0 | 4 |
| pilar · sell · ? | 2 | 1 | 19.0 | 19 | 19 | 19 | t13 (abre 49, paso 1, 8 r.) | 0.19 | 5.0 |
| pilar · sell · uncommon | 12 | 3 | 16 | None | 18 | 19 | t13 (abre 31, paso None, 1 r.) | 0.0 | 1.5 |

## Market Test — puesto gratuito = 7.5

- t10: 12.5 (SUPERA al puesto) · v07 0bps board trades=2
- t14: 11.86 (SUPERA al puesto) · v14 0bps auto trades=1
- t12: 11.74 (SUPERA al puesto) · v02 0bps board trades=2
- t17: 10.26 (SUPERA al puesto) · v17 0bps auto trades=1
- t06: 9.83 (SUPERA al puesto) · v01 0bps board trades=1
- t08: 7.46 (por DEBAJO del puesto) · v06 0bps board trades=0
- t13: 5.49 (por DEBAJO del puesto) · v03 0bps board trades=0
- t03: 3.61 (por DEBAJO del puesto) · v09 300bps auto trades=0; v20 0bps board trades=0
- evento tick 441: {"session": 2, "name": "The Market Test: every venue gets the same synthetic book", "venues": ["v01", "v02", "v03", "v04", "v05", "v06", "v07", "v10", "v11", "v
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
- Ofertas publicadas en v02: 57  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v03: 6  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v04: 7
- Ofertas publicadas en v05: 6
- Ofertas publicadas en v06: 7
- Ofertas publicadas en v07: 8
- Ofertas publicadas en v10: 5
- Ofertas publicadas en v11: 3
- Ofertas publicadas en v12: 12
- Ofertas publicadas en v13: 1
- Ofertas publicadas en v14: 15
- Ofertas publicadas en v16: 1
- Ofertas publicadas en v17: 16
- Ofertas publicadas en v18: 12  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v19: 13
- Ofertas publicadas en v20: 16

## Precios P2P liquidados (mediana por carta suelta)

- common: 8.0 P
- rare: 74.0 P
- uncommon: 21 P

## A quién vender cada set

- LAT: t04, t14, t13, t03
- LAV: t04, t06, t02, t03, t09
- MAL: t17, t08
- RET: t05, t14, t10, t12, t06, t02, t13, t18, t09
- SAL: t08, t06, t02, t03, t16, t18, t09

Líderes (no operar en sus mercados): t12, t13, t18
