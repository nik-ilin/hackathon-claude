# Análisis del juego — sábado 10:05 (tick 226, t = 3,21 h, ronda 2 al 37 %)

## 1. Dónde estamos
| | Nosotros (t15) | Líder (t12) | 2º (t13) |
|---|---|---|---|
| Puesto · puntuación | 14º · 12,12 | 1º · 27,60 | 2º · 24,66 |
| Negociación | 7,32 (neg_points 10,5; ladder 0; duelos 0) | 19,59 | 22,53 |
| Market-making | 4,80 (puesto gratuito, eficiencia 0,899 = ½) | **8,01** (único broker que supera al puesto) | 2,13 (broker **peor** que el puesto) |

Caja 286 P · nivel 2 · álbum: LAT 10/10, MAL 7/10, LAV 5/10, SAL 5/10, RET 0/10 · **2 sobres sin abrir** (bienvenida, barrio).

## 2. Cómo puntúa de verdad (verificado hoy)
- **Market-making**: el puesto gratuito da 4,8 a todos. Un mercado propio con broker malo da **menos** (t13 2,13, t06 3,66, t08 4,75). Solo t12 lo supera (8,01). Hay 8 Market Tests más hoy y 2 el domingo (valen ~4× cada uno).
- **Negociación**: relativa al líder. Nuestros tres componentes que puntúan están casi a 0: **ladder 0, duelos 0** y P2P 10,5.
- **Ladder**: solo cuentan tratos negociados hasta el final del dealer. Hoy el bot que opera con nuestra clave ha abierto **9 hilos y cerrado 0** (abre «comprar RET-06», recibe el precio y no contraoferta → el dealer cierra por inactividad).

## 3. Errores propios en curso (bot que opera con nuestra clave)
1. **Publica en v02, el mercado de t12 (líder)**: 16 ofertas. Cada trato ahí suma a su «valor creado entre equipos en tu mercado». Hay que usar El Rastro o mercados de equipos no líderes a 0 % (v04 t02, v05 t04, v07 t10).
2. **Hilos con dealers sin regatear**: 0 tratos en la ronda ⇒ ladder 0 y cupo horario gastado.
3. **Pujas públicas por RET a 10–11**: revelan que nos interesa RET (los rivales nos perfilan, ya lo hicieron el viernes).
4. **Sin módulo de duelos activo**: Duelos I empiezan ~12:00. Sin él, otra vez 0.
5. **Dos sobres sin abrir** (no puntúan, pero pueden traer RET y acercar páginas).

## 4. Lo que hacen bien los de arriba
- **t12**: broker propio que supera al puesto; P2P con poca comisión; 29 tratos.
- **t13**: 34 tratos, abre muy lejos del dealer (≈ 0,37 × su precio) y sube de 1 en 1 hasta el final; busca RET, LAV y MAL.
- **t02 (+7,9 en la ronda)**: cierra 7 de 8 hilos con dealers y captura todo el rango; vende a la Abuela en lotes.
- **t05**: compra cartas a la Abuela a 9 abriendo a 7 con pasos de 1.

## 5. Precios de referencia de los dealers (mediana de finales / mejor trato)
| Trato | Apertura | Final típico | Mejor observado |
|---|---|---|---|
| Abuela vende común | 12 | 8–9 | 8 (abrir 4, paso 1) |
| Abuela vende poco común | 29 | 22 | — |
| Abuela vende sobre barrio | 30 | 21 | 19 (abrir 10, paso 1, 7 rondas) |
| Abuela compra común | 5 | 6 | 6 |
| Chato vende rara | 97 | 87–92 | 86 (abrir 70, paso 4) |
| Chato vende poco común | 33 | 27 | 26 (abrir 9, paso 3, 8 rondas) |
| Chato compra poco común | 13 | 15 | 15 |

Para nosotros RET vale: común 16, poco común 40, rara 112 ⇒ **comprar RET a dealers hasta su final es a la vez ladder y valor** (poco común a ~22–27 = +13–18 de margen).

## 6. Próximas fases (hora ≈ 10:05 + (t − 3,21) h)
| t (h) | ≈ Hora | Evento | Acción clave |
|---|---|---|---|
| 5,00 | 11:50 | Market Test | Puesto gratuito (4,8) salvo broker validado |
| 5,15 | 12:00 | **Duelos I** (precio, todos contra todos) | Módulo de duelos activo; nada de aceptaciones de dealer/P2P durante la oleada |
| 7,0 · 9,0 · 11,0 | 13:50 · 15:50 · 17:50 | Market Tests | — |
| 11,65 | 18:30 | **Duelos II** (precio + días, 6 simultáneos, 2 rondas) | Verificar formato de `your_days_weight` en el primer duelo |
| 13,0 · 15,0 | 19:50 · 21:50 | Market Tests | — |
| 14,65 | 21:30 | Market Test **difícil** (firmes e impacientes) | Brokers que esperan pierden: el cruce del puesto es robusto |
| 16,65 | cierre | Ronda 3 (domingo): CHA, +150 P | Domingo: 2 Market Tests (~4× cada uno), Duelos III y final |

## 7. Errores de rivales que podemos aprovechar
- **Brokers peores que el puesto (t13, t06, t08)**: sus mercados casan mal ⇒ las ofertas publicadas allí se quedan sin cruzar; t13 (2º) pierde ~2,7 puntos por Market Test frente al puesto. No compitamos con un broker propio salvo que esté validado: el puesto ya nos pone por delante de ellos en ese bloque.
- **Duelos**: la mayoría de equipos no tiene un módulo bueno (el repo del compañero no lo tenía). Rivales que ceden solos ⇒ silencio y aceptación escalonada captura ~98 % en repetición. Rivales mudos ⇒ una sola oferta propia.
- **Carrera por RET** (t13, t02, t05, t10 y nosotros): los precios de RET subirán. Comprar a los **dealers** (precio acotado por su final) antes que en P2P; vender a esos equipos nuestras cartas de sets que les interesan.
- **Abuela**: muchos aceptan antes de su final (sobres a 24 cuando el final es 19–21).
- **Mercados líderes**: nunca darles flujo; los rivales que publican en v02/v03 alimentan a t12/t13.

## 8. Prioridades para el resto del sábado (por puntos esperados)
1. **Duelos I a las 12:00** con el módulo de duelos (PR #1 del repo o nuestro `bazaar/strategy/duels.py`): de 0 a una parte grande de la tarta en ~34 duelos.
2. **Ladder**: 3 tratos negociados hasta el final con cada dealer esta ronda (Abuela: vender duplicados comunes a 6; Abuela/Chato: comprar RET poco común ≤ 27).
3. **Dejar de publicar en v02** y retirar esas 16 ofertas; vender sobrantes (LAT-05 ×2, LAT-01, MAL-02, LAV-02, LAV-03) a 7–9 en El Rastro / v04 / v05 / v07.
4. **Abrir los dos sobres**.
5. **Market-making**: mantener el puesto (4,8). Abrir mercado propio solo con broker con suelo = cruce del puesto + vigilante; ganancia esperada pequeña frente al riesgo de quedar por debajo (como t13).
6. Duelos II (18:30): mismo módulo; comprobar el formato de los días en el primer duelo.
