# Auditoría de los líderes — sábado 10:30 (tick ~280)

Fuente: hilos completos con dealers de t18, t12, t02 y t13 en `intel/market.db` (mensajes, precios y finales).

## Claves de cada líder
| Equipo | Qué hace | Por qué puntúa |
|---|---|---|
| **t18** (1º, +9,9 en la ronda) | Compra RET a los dealers en serie, un hilo tras otro. Rara al Chato: abre a 0,72 y sube de 4 en 4 (70 → 86). Comunes a la Abuela: abre a 7 y sube de 1 en 1 (cierra a 9–10). | Ladder: 10 de 13 hilos cerrados con 95 % de captura de rango. Además completa la página RET (afinidad alta para él). |
| **t12** (2º) | Broker propio en v02 (MM 10,4). Compra sobres y RET a la Abuela: abre a 0,3–0,5 con pasos de 1–2. | Único broker que supera al puesto; ladder sólido. |
| **t02** (3º, +18,4) | Vende comunes a la Abuela: pide 10 y baja de 1 en 1 hasta que ella da su final de 6. Compra RET poco común al Chato (28 → 31). | Muchos tratos pequeños con el rango entero capturado. |
| **t13** (4º, −6,9) | Abre absurdamente bajo (5–12 para una poco común de 33) con pasos de 1–3 y abandona a las 3 rondas. | Sus últimos 12 hilos no cierran: el Chato no se mueve ante pasos pequeños. **Error que está cometiendo ahora.** |

## Reglas de los dealers (observadas, no supuestas)
1. **El Chato copia nuestro paso.** «Tú mueves cuatro, yo muevo uno… Cuatro tuyos, cuatro míos.» Con +1 se queda en 33 tres rondas; con +3/+4 cede 2–4 por ronda.
2. **El Chato acepta él mismo** cuando nuestro precio queda a 1–2 del suyo (t18: 86 frente a su 88, «Hecho»).
3. **Tras el final del Chato, contraofertar final − 1 funcionó 2 de 2 veces** (t13: 27F → 26 aceptado).
4. **La Abuela cede ~1 por ronda y su final baja con la paciencia**: sobre de barrio a 19–21 con pasos de 1 (t13) frente a 22–24 con pasos de 2 (t12).
5. **La Abuela vende poco comunes a 21–22; el Chato, a 27–31.** Las poco comunes se compran a la Abuela.
6. **La Abuela compra comunes a 5 y su final es 6**, abras como abras (t13 abre a 22 y obtiene lo mismo que t02 abriendo a 10).
7. **El ladder mide la parte del rango, no el precio**: una común de 12 → 9 cuenta igual que una rara. Los líderes encadenan tratos baratos.

## Cambios aplicados a nuestra estrategia (`bazaar/strategy/dealers.py`)
- Pasos por dealer: Chato poco común +3 (antes +2), rara +4; Abuela +1 en todo, sobres incluidos (antes +2).
- Apertura de rara al Chato a 0,70 de su precio (la mejor rara observada).
- Contraoferta única a final − 1 tras el final del Chato; si no la acepta, se toma su final en el tick siguiente.
- Enrutado: no se compra a un dealer si otro tiene un final aprendido ≥ 3 P más barato para esa rareza.
- Pruebas: `tests/test_dealers_audit.py`.

## Lo que viene y cómo anticiparnos
| Cambio previsible | Riesgo / oportunidad | Respuesta |
|---|---|---|
| **Carrera por RET** (t18, t13, t02, t05, t04, t10 compran) | Si los dealers tienen existencias limitadas, se agotan; en P2P, las RET se encarecerán. | Comprar RET a los dealers **ya** (comunes a la Abuela ≤ 10, poco comunes a la Abuela ≤ 22, raras al Chato ≤ 88). Nuestro valor: 16 / 40 / 112. |
| **El Chato «recuerda»** («Trato limpio, lo recordaré») | Puede haber reputación: los que lo insultan con aperturas de 0,2 (t13) quizá reciban peores precios. | Aperturas razonables (0,55–0,70) y pasos grandes; nunca abandonar un hilo. |
| **Nivel 3 / nuevos dealers** | Los primeros en negociar con un dealer nuevo se llevan el ladder fácil (3 mejores tratos por nivel y ronda). | El agente abre hilo en cuanto aparece en `me.unlocked`; vigilar `levels` en el informe. |
| **Duelos I (~12:00) y II (~18:30)** | Casi nadie tiene un módulo bueno. | Módulo de duelos activo en el terminal que opera; nada de aceptaciones de dealer durante la oleada. |
| **Market Test difícil (~21:30) y domingo (4×)** | Los brokers lentos pierden frente al puesto. | Mantener el puesto gratuito salvo broker validado. |
| **Domingo: set CHA y +150 P** | Afinidad CHA 0,9 para nosotros: poco valor. | Vender CHA a quien lo busque y gastar la caja en RET. |
| **t13 sigue abandonando hilos** | Su ladder se estanca; puede corregirlo. | Ventaja temporal para nosotros solo si nuestro operador **cierra** sus hilos (hoy llevamos 0/23). |
