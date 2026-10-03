# Inteligencia de mercado — 2026-10-03 12:55:06

## Bucle de automejora (t15)

- Distancia al 1º: 10.77 · tendencia últimas instantáneas: -0.11
- Desglose propio (/api/me): neg_points 24.2 · duel_points 10.21 · ladder_points 0.033 · mm_points 0.0 · bench_efficiency 0.933 · bench_points 0.5 · negotiating 14.11 · market 7.5
- Palanca 1: negociación: captura con dealers 0.14 vs 0.75 del líder; tratos/hilos 4/35 → abrir más bajo y pasos cortos (ver dealer_params)
- Palanca 2: market-making: 1 trato en nuestro mercado propio (líderes con 1-2 tratos sacan 12.06 vs puesto 7.5)
- Parámetros aprendidos del mejor trato por dealer/artículo en `recommendations.json` → `self_improve.dealer_params`

## Cambios en mercados (ticks 420→561)

- v01 (t06) tratos 1→2
- v17 (t17) tratos 0→1
- v16 (t16) comisión 300→0bps
- v18 (t18) comisión 300→0bps
- rastro (world) tratos 30→32

Ronda: Saturday · Gran Vía

## Clasificación y perfil de equipos

| # | Equipo | Score | Δronda | Neg | MM | Nivel | Álbum | Dealer: tratos/hilos | Captura rango | Apertura vs dealer | Paso | P2P compra/venta | Compra vs mercado | Venta vs mercado | Le interesa | Le sobra | Mercado propio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Team 14 (t14) | 32.38 | 14.32 | 21.78 | 10.6 | 3 | 31 | 7/16 | 0.75 | 0.45 | 2.0 | 5/1 | 1.0 | 0.62 | LAT, RET | MAL, LAV, SAL | v14 0bps auto trades=1 |
| 2 | Team 18 (t18) | 30.16 | 11.0 | 22.66 | 7.5 | 3 | 33 | 10/13 | 0.75 | 0.58 | 1.5 | 3/4 | 1.12 | 1.05 | SAL, RET | LAT, MAL, LAV | v18 0bps auto trades=0 |
| 3 | Team 5 (t05) | 29.64 | 9.65 | 22.14 | 7.5 | 3 | 29 | 17/23 | 0.37 | 0.59 | 2.5 | 3/2 | 1.0 | 0.94 | LAV, RET | LAT, SAL, MAL | v10 0bps auto trades=2 |
| 4 | Team 12 (t12) | 29.6 | 1.78 | 19.08 | 10.52 | 3 | 37 | 14/25 | 0.5 | 0.48 | 2.0 | 4/5 | 0.88 | 1.05 | RET | SAL, MAL, LAV | v02 0bps board trades=2 |
| 5 | Team 17 (t17) | 28.3 | 6.27 | 18.84 | 9.46 | 3 | 31 | 4/19 | 1.0 | 0.52 | 3.0 | 2/1 | 1.13 | 1.19 | MAL | LAV, LAT | v17 0bps auto trades=1 |
| 6 | Team 10 (t10) | 28.3 | 7.55 | 16.25 | 12.06 | 3 | 27 | 7/17 | 0.37 | 1.54 | 2.0 | 0/3 | None | 1.24 | RET | SAL, MAL, LAV | v07 0bps board trades=2 |
| 7 | Team 13 (t13) | 27.91 | -2.03 | 22.42 | 5.49 | 3 | 29 | 23/89 | 0.37 | 0.3 | 1.75 | 2/4 | 1.02 | 0.91 | RET, LAT | SAL, LAV, MAL | v03 0bps board trades=0 |
| 8 | Team 1 (t01) | 25.44 | 17.05 | 17.94 | 7.5 | 3 | 29 | 4/12 | 0.18 | 4.2 | 3 | 8/3 | 0.98 | 1.0 |  | LAT, SAL, LAV | v08 300bps auto trades=0; v19 0bps board trades=0 |
| 9 | Team 2 (t02) | 24.61 | 17.78 | 17.11 | 7.5 | 3 | 33 | 25/55 | 0.46 | 2.0 | 1.0 | 6/8 | 0.88 | 1.11 | RET, SAL, LAV |  | v04 0bps auto trades=0 |
| 10 | Team 6 (t06) | 24.14 | 11.47 | 12.5 | 11.64 | 3 | 30 | 7/17 | 0.55 | 1.47 | 2 | 3/7 | 1.08 | 1.0 | SAL, LAV, RET | LAT, MAL | v01 0bps board trades=2 |
| 11 | Team 4 (t04) | 23.94 | 3.45 | 16.44 | 7.5 | 3 | 32 | 16/31 | 0.5 | 0.66 | 2.0 | 3/5 | 1.0 | 0.95 | LAV, LAT, RET | MAL, SAL | v05 0bps board trades=0 |
| 12 | Team 8 (t08) | 23.14 | 5.53 | 15.68 | 7.46 | 3 | 29 | 13/38 | 0.74 | 1.38 | 2.0 | 2/0 | 1.06 | None | SAL, MAL | LAV, LAT, RET | v06 0bps board trades=0 |
| 13 | Team 15 (t15) | 21.61 | 11.28 | 14.11 | 7.5 | 3 | 34 | 4/35 | 0.14 | 0.66 | 2.0 | 4/6 | 1.19 | 0.89 | RET, SAL, MAL | LAT, LAV | v15 300bps auto trades=0 |
| 14 | Team 16 (t16) | 21.57 | 14.73 | 14.07 | 7.5 | 3 | 26 | 17/22 | 0.75 | 0.41 | 1 | 1/1 | 0.89 | 1.08 | SAL | LAV, MAL, LAT | v16 0bps auto trades=0 |
| 15 | Team 9 (t09) | 19.14 | 9.62 | 11.64 | 7.5 | 3 | 33 | 16/25 | 0.49 | 1.29 | 2.0 | 5/1 | 1.0 | 0.86 | RET, SAL, LAV | LAT | v12 0bps auto trades=0 |
| 16 | Team 3 (t03) | 18.95 | 4.35 | 15.34 | 3.61 | 3 | 31 | 7/8 | 0.5 | 0.49 | 1.0 | 1/1 | 0.88 | 1.33 | SAL, LAV, LAT | MAL | v09 300bps auto trades=0; v20 0bps board trades=0 |
| 17 | Team 7 (t07) | 16.73 | 7.77 | 9.23 | 7.5 | 3 | 35 | 4/11 | 0.54 | 0.35 | 3.0 | 0/0 | None | None |  | MAL, LAT, LAV | v11 300bps auto trades=0 |
| 18 | Team 11 (t11) | 7.5 | 7.5 | 0.0 | 7.5 | 3 | 13 | 0/0 | None | None | None | 0/0 | None | None |  |  | v13 300bps auto trades=0 |

## Dealers: cómo ceden

| Dealer · modo · artículo | Hilos | Tratos | Apertura | Final (mediana) | Trato mediano | Mejor trato | Lo consiguió | Cesión/ronda | Rondas |
|---|---|---|---|---|---|---|---|---|---|
| abuela · ? · ? | 13 | 9 | 22.5 | 20.0 | 21 | 24 | t18 (abre None, paso None, 0 r.) | 0.33 | 2 |
| abuela · buy · common | 49 | 34 | 12 | 8 | 9.0 | 8 | t03 (abre 4, paso 1, 4 r.) | 0.67 | 3 |
| abuela · buy · sobre_barrio | 43 | 10 | 30 | 21 | 21.5 | 19 | t13 (abre 10, paso 1.0, 7 r.) | 1.5 | 4 |
| abuela · buy · uncommon | 78 | 35 | 29 | 24 | 23 | 10 | t02 (abre 22, paso 2, 2 r.) | 1.4 | 3.0 |
| abuela · sell · ? | 6 | 3 | 12 | 5 | 6 | 22 | t09 (abre 30, paso None, 1 r.) | 0.0 | 1.0 |
| abuela · sell · common | 67 | 31 | 5 | 5.5 | 6 | 6 | t10 (abre 15, paso 1, 4 r.) | 0.0 | 4 |
| abuela · sell · lote2 | 2 | 1 | 10.0 | None | 10 | 10 | t08 (abre 15, paso None, 1 r.) | 0.25 | 1.5 |
| abuela · sell · lote3 | 1 | 0 | 15 | 16 | None | None | t17 (abre 24, paso 1.0, 5 r.) | 0.2 | 5 |
| abuela · sell · uncommon | 7 | 3 | 12 | 13.5 | 15 | 17 | t02 (abre 24, paso 1.5, 5 r.) | 0.5 | 4 |
| chato · ? · ? | 9 | 3 | 23.0 | 22.5 | 28 | 29 | t03 (abre 18, paso None, 1 r.) | 0.5 | 0 |
| chato · buy · rare | 32 | 17 | 97.0 | 89.0 | 87 | 84 | t09 (abre 60, paso 4.0, 7 r.) | 1.17 | 5.0 |
| chato · buy · sobre_plata | 1 | 1 | 188 | None | 181 | 181 | t08 (abre 140, paso 10, 4 r.) | 1.75 | 4 |
| chato · buy · uncommon | 70 | 11 | 33.0 | 31.0 | 31 | 26 | t13 (abre 9, paso 3, 8 r.) | 0.25 | 4.0 |
| chato · sell · ? | 5 | 1 | 13 | None | 31 | 31 | t02 (abre 27, paso None, 1 r.) | 0.0 | 1 |
| chato · sell · lote2 | 1 | 1 | 26 | None | 26 | 26 | t08 (abre 36, paso None, 1 r.) | 0.0 | 1 |
| chato · sell · rare | 1 | 0 | 39 | None | None | None | t08 (abre 150, paso 15.0, 3 r.) | 0.0 | 3 |
| chato · sell · uncommon | 29 | 12 | 13 | 14 | 14.0 | 15 | t02 (abre 21, paso 1.0, 5 r.) | 0.0 | 4 |
| pilar · sell · ? | 6 | 1 | 16.0 | None | 24 | 24 | t09 (abre 30, paso 1, 4 r.) | 0.17 | 3.0 |
| pilar · sell · epic | 1 | 1 | 122 | None | 140 | 140 | t08 (abre 165, paso 8, 4 r.) | 4.5 | 4 |
| pilar · sell · uncommon | 35 | 21 | 16.0 | 19 | 19 | 25 | t04 (abre 40, paso 1, 6 r.) | 0.25 | 3 |

## Market Test — puesto gratuito = 7.5

- t10: 12.06 (SUPERA al puesto) · v07 0bps board trades=2
- t06: 11.64 (SUPERA al puesto) · v01 0bps board trades=2
- t14: 10.6 (SUPERA al puesto) · v14 0bps auto trades=1
- t12: 10.52 (SUPERA al puesto) · v02 0bps board trades=2
- t17: 9.46 (SUPERA al puesto) · v17 0bps auto trades=1
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
- Ofertas publicadas en v02: 57
- Ofertas publicadas en v03: 6
- Ofertas publicadas en v04: 7
- Ofertas publicadas en v05: 6
- Ofertas publicadas en v06: 7
- Ofertas publicadas en v07: 8
- Ofertas publicadas en v10: 5  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v11: 3
- Ofertas publicadas en v12: 12
- Ofertas publicadas en v13: 1
- Ofertas publicadas en v14: 15  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v16: 1
- Ofertas publicadas en v17: 16
- Ofertas publicadas en v18: 12  ⚠️ mercado de un LÍDER (le suma market-making)
- Ofertas publicadas en v19: 13
- Ofertas publicadas en v20: 16

## Precios P2P liquidados (mediana por carta suelta)

- common: 8.0 P
- rare: 76 P
- uncommon: 21 P

## A quién vender cada set

- LAT: t14, t04, t03, t13
- LAV: t06, t04, t09, t05, t03, t02
- MAL: t08, t17
- RET: t18, t10, t14, t06, t04, t09, t05, t13, t12, t02
- SAL: t18, t08, t06, t09, t03, t02, t16

Líderes (no operar en sus mercados): t14, t18, t05
