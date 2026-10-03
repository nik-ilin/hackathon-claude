# Ingeniería inversa del top 3 y plan para las próximas horas (sábado 12:30)

Fuente: `intel/market.db` (feed público, tablón, hilos propios y `/api/me` con nuestra clave; solo lectura). Equipos analizados: t12, t14, t13, t17 y t18.
Reglas propias: solo jugadas dentro de RULES.md. No se usan claves ajenas ni fallos del servidor, y no se hacen flags sin pruebas, porque un flag equivocado resta puntos.

## 1. Lo que hacen los líderes (y lo que copiamos)

| Equipo | Motor de puntos | Táctica observada | Copiar / contrarrestar |
|---|---|---|---|
| **t12** (1.º, 32,2) | Neg 20,5 + MM 11,7 | Venue `board` a 0 bps en v02: sus 2 tratos se los dimos nosotros. **Su principal contraparte P2P somos nosotros (4 de 8 tratos).** Con la Abuela compra sobres abriendo a 8-9 con pasos de +2 y cierra a 20-24. | **Dejar de ser su liquidez**: no cruzar con t12/t13/t14 salvo con excedente grande para nosotros (ver 3.1). |
| **t14** (2.º, 31,2) | Neg 19,3 + MM 11,9 | Bajó v14 a 0 bps y le llegó 1 trato (el nuestro). Con la Abuela abre a 5 con pasos de +2. Con Pilar baja de 29 de 2 en 2 hasta que ella llega a 19-20. | Copiar: comisión 0 bps y la misma escalera con Pilar. |
| **t13** (3.º, 29,8) | Neg 24,3 (nivel 3 antes que nadie) | Volumen bruto: 83 hilos con dealers y 365 ofertas a dealers. Abre extremadamente bajo (7 frente al 33 del Chato) y sube de 1 en 1. Con Pilar empezó en 49 y bajó de 8 en 8 hasta 17-18. Hace marketing en el feed («0 % FEE… Every tick you wait, another team gets the card you need»). Nos dirige ofertas en venues de otros (v02, v07, v10). | Copiar el volumen y la apertura baja. **Ignorar sus ofertas dirigidas**: buscan nuestros valores privados. |
| t17 (4.º) | Duelos + MM 10,3 | 0 bps y 1 trato. Raras del Chato: abre a 72 con pasos de +3; el Chato baja 97→86 copiando el paso. | Fórmula de raras: abrir a ~0,74 de la apertura del Chato con pasos de 3. |
| t18 (5.º) | Ladder 0,75 de captura | Comunes de la Abuela: abre a 7, pasos de +1, cierra a 9-10. | Copiar con la Abuela. |

## 2. Cómo responden los dealers (patrones verificados)

- **Abuela** (nivel 1)
  - Vende comunes: abre a 12 → 10 → 9-10. Abrir a 5-7 y subir de 1 en 1.
  - Vende sobres de barrio: 30 → baja 1 por ronda → final 20-24.
  - Le gusta la amabilidad y regala cartas (gift.given). Los regalos no puntúan.
- **Chato** (nivel 2)
  - Poco comunes: se queda en 33 tres rondas y luego 32 → 31 (final). Captura baja: no merece muchos hilos.
  - Raras: abre a 97 y copia nuestro paso (con +3 baja ~3 por ronda). Final ~86-89; el mejor trato observado es 84.
- **Pilar** (nivel 3, ya **desbloqueada para nosotros**). Compra poco comunes:
  - abre a 16 y sube 1 por cada 1-2 rondas; final 17-20 (t14 sacó 19-20 bajando de 29 de 2 en 2);
  - nivel 3 = **más peso en el ladder**.
- **Bluffs:** el Chato dice «Last number I say tonight» y Pilar «my final affection» **sin** `final: true`. Si después mueven el precio, es mala fe: se puede marcar con flag (un flag correcto suma). Solo con la secuencia de mensajes como prueba.

**Nuestro ladder vale 0,033** (dato de `/api/me`): es la mayor fuga. Cuentan los 3 mejores tratos por nivel y uno que falte vale 0. Hacen falta 3 tratos negociados con la Abuela, 3 con el Chato y 3 con Pilar.

## 3. Trampas y ventajas para las próximas horas

1. **Cortar la liquidez a los líderes**: `--deny-teams t12,t13,t14`, o exigirles un excedente alto. Cada trato que les damos les suma valor P2P (y market-making si es en su venue).
2. **Fiebre de Salamanca (≈14:55 → 17:13)**: Pilar paga un 25 % sobre libro por SAL.
   - Nuestra afinidad por SAL es 0,5: valoramos poco esas cartas.
   - **Vender TODAS nuestras SAL a Pilar en ese intervalo** da a la vez valor, los 3 tratos de nivel 3 y el ladder con más peso.
   - Preparar desde ya, antes de las 14:55: acumular SAL baratas (tablón y la Abuela), sin romper páginas completas.
3. **Market Tests** (≈14:45, 17:05, 19:23; **el difícil ≈21:17**; 21:41): pasar v15 a 0 bps. Un solo trato entre otros equipos en v15 lleva el market-making de ~7,5 a ~12. Anunciar v15 en el feed como hace t13.
4. **Duelos II (los organizadores dicen «hacia las 18:30»; el calendario del servidor da ≈17:49: estar listos a las 17:40; precio + días, decay 0,08, 6 simultáneos, 2 rondas)**:
   - Los rivales son bots con perfiles fijos y el silencio evita el decay (`rounds` = 0 si no hablamos).
   - Perfiles medidos en Duelos I:
     - **Plata**: cede 1-3 por tick → esperar.
     - **Verde**: ciclos que mejoran → esperar.
     - **Oro y Luna**: saltos grandes y luego se plantan → aceptar tras el salto.
     - **Rojo y Noche**: a veces **empeoran** con el tiempo (Rojo vendedor 133→166) → aceptar pronto u ofertar.
     - **Mudos**: una oferta propia (`--ladder`).
   - Los días son la **urgencia de la fecha de entrega** (organizadores). Consejo oficial: revisar todas las ofertas frente al límite.
   - Con días: «the pie grows for teams that trade on what each side cares about». Cedemos los días que nos importan poco y cobramos en precio (logrolling).
5. **Domingo** (ticks de 15 s):
   - Ronda 3 ≈09:29 con 150 P de asignación.
   - Duelos III ≈11:29 (decay 0,10, 12 ticks).
   - **Final ≈14:29**: dealers cerrados y última oleada de duelos.
   - El ladder del domingo empieza de cero: 9 tratos negociados en la primera hora.

## 4. Orden de ejecución (el compañero; todo escritura del bot)

1. Ahora: v15 a 0 bps. Coordinador con `--no-rival-venues --duende-venue rastro` (PR #6).
2. Ahora → 14:55: ladder, 3 tratos con la Abuela y 3 con el Chato (PR #8 `--dealer-ladder`), y acumular SAL.
3. 14:55 → 17:13: vender SAL a Pilar con escalera tipo t14 (abrir alto y bajar de 2 en 2 hasta 19-20 × 1,25).
4. 17:40 (Duelos II hacia las 17:49–18:30): parar el coordinador. Duelos II con `python3 duel_runner.py --execute --days --ladder --reconcile --verify-accept` (PR #6 + #7).
5. 21:00: vigilar el Market Test difícil con v15 a 0 bps.

## 5. Nuestro inventario: ventas concretas (`/api/me`, tick ~600)

Caja: 118 P. El ladder solo necesita **3 tratos negociados por dealer**, y para eso vender es más barato que comprar.

| Activo | Nuestro valor | Comprador | Precio esperado | Ganancia | Nota |
|---|---|---|---|---|---|
| **LAV-08** poco común ×2 | 4,4 | **Pilar** | 19 (final), hasta 25 pidiendo 33-40 y bajando | +15 a +20 c/u | 2 tratos de nivel 3 **ya** |
| **SAL-06, SAL-07** poco comunes | 12,5 | **Pilar en la fiebre** (≈14:55-17:13) | 19-25 × 1,25 ≈ 24-31 | +12 a +18 c/u | 3.er-4.º trato de nivel 3 |
| **LAV-10** rara | 49 | Pilar (paga por encima de libro) o el Chato | por medir: pedir alto | ? | Solo si la oferta final supera 49 |
| Duplicadas comunes: LAT-05, LAV-01, LAV-02, LAV-03, MAL-01, MAL-02, MAL-05 | 1,8-3,2 | **Abuela** | 6 (su final), pedir 15 y bajar de 1 en 1 como t10 | +3 a +4 c/u | 3 tratos de nivel 1 |
| **sobre_plata sin abrir** | — | abrirlo | — | — | t04 sacó una épica (LAT-11) de un sobre_plata y **Pilar paga 140 por épicas** |
| LAT-09, LAT-10 raras | 177 | **no vender** | — | — | La página LAT está completa (page_guard) |

**Faroles de Pilar:** cuando dice «my final courtesy» o «my last gesture» sin `final: true`, todavía sube 1-2 P (3 casos medidos). Hay que seguir regateando hasta `final: true`.

No hay mentiras denunciables: de 1.811 mensajes de dealers con precio, el número del texto coincide con la oferta en el 100 %. El detector `intel/lies.py` vigila los dealers nuevos del domingo.
