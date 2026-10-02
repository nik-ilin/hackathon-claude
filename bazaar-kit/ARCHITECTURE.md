# Team 15 · Arquitectura del agente

Un único coordinador (`coordinator.py`) observa el juego una vez por tick, reconcilia lo enviado con la evidencia del servidor, genera candidatas de todos los módulos y envía como mucho una acción por clase de límite. Es la única autoridad que escribe y comparte un solo presupuesto.

```
observar ─► reconciliar ─► candidatas ─► seleccionar ─► enviar ─► liquidada ─► resultado observado
 (snapshot)   (feed, hilos,   (mercado,      (1 por clase:    (registro       (evidencia     (métricas privadas
              ofertas)        vendedores,    aceptar/mensaje/  antes de        del servidor)  antes/después,
                              seguridad)     publicar/abrir)   escribir)                      atribución incierta)
```

| Módulo | Papel |
|---|---|
| `trading.py` | Valoración marginal (con bono de página), comisiones, lectura de ofertas (venta, compra, swap, lote ≤ 4), recursos comprometidos, valor de sobres, caducidad, ofertas propias inseguras, reconciliación. Sin red. |
| `negotiation.py` | Estado de una conversación reconstruido desde el hilo; política por vendedor (`dealer_policy`, `decide_dealer`); validación estructural; registro y bloqueo de instancia. Sin red. |
| `coordinator.py` | Bucle único, presupuesto compartido, idempotencia, etapas visibles en consola. |
| `market_agent.py` | Motor de mercado independiente (v2 usa `trading.py`; v1 se conserva). |
| `starter_agent.py` | Compras a vendedores en solitario (`--first-purchase`). Su política rápida sigue disponible. |

## Lo que puntúa (Kickoff, pág. 9) y cómo lo usamos

- **Negociación (30):** duelos, la escalera de vendedores (parte del rango de precio capturada) y el valor ganado comerciando con equipos a nuestros valores. El coordinador:
  - compra y vende entre equipos solo con excedente ΔU ≥ margen;
  - con los vendedores, en modo `score`, **nunca acepta su precio de apertura** («a deal at the opening price does not count»).
- **Market-making (30):** Market Test y valor creado entre otros equipos en nuestro mercado. Ver la decisión más abajo.
- **No puntúa:** número de operaciones, comisiones, suerte de sobres, regalos. Por eso los sobres nunca se compran por rutina y la actividad no es un objetivo.
- **La conversión de excedente o de métricas privadas a puntos no está documentada.** El orden entre clases (seguridad > conversaciones abiertas > aceptar con excedente > abrir conversación > publicar) es una heurística declarada.

## Hechos verificados con el servidor

| Hecho | Evidencia |
|---|---|
| `collection_value` = Σ `book × multiplicador × copy_marginals[k]` por copia, más el bono de cada página completa | modelo igual al servidor en los ticks 94, 102, 104, 126, 129 y 137 |
| `/me/value` incluye el bono de página: LAT-10 = 177,1 = 91,0 + 0,25 × 344,5 | tick 129, La Latina 9/10 |
| `your_value` de una copia poseída = marginal de la última copia | 23 referencias, tick 94 |
| Comisión de El Rastro = ⌈5 % × precio⌉ + 1 P por carta, la paga quien acepta | liquidaciones 130, 140, 142 y 146; el efectivo cuadra exactamente (400 → 195 P) |
| Las ofertas liquidadas aparecen con `status: "settled"` en el hilo y como evento `settlement` en `/api/feed` | hilos 123, 130, 135, 139 y 143 |
| `expires_in_ticks` se divide por 4 a 60 s/tick (4→1, 8→2, 20→5); el valor por defecto del SDK (40) da 10 en el tablón | ofertas 1458, 2092 y 1688; 19 ofertas a 10 ticks en el tablón |
| `my_offers` incluye ofertas de otros equipos dirigidas a nosotros: hay que filtrar por `maker` | ofertas 1648 (t18) y 1853 (t05), tick 138 |

Lo que **no** está verificado: la base del bono master; la comisión de un swap sin efectivo (se asume 1 P por carta); la equivalencia en ticks de 30 s y 15 s (hipótesis: ticks de 15 s); qué tratos con vendedores cuentan exactamente para la escalera más allá de «no a precio de apertura».

## Errores encontrados y corregidos

1. **Precio de cierre anotado como nuestra última contraoferta.** LAT-08 se anotó a 13 P y se liquidó a 17 P; un sobre se anotó a 15 P y se liquidó a 30 P. Ahora se lee de la oferta liquidada, y las correcciones se anotan sin borrar el original.
2. **Compra confirmada contando sobres en el inventario.** Si el sobre ya se había abierto, la compra quedaba pendiente para siempre. Ahora la confirma la oferta liquidada.
3. **Bono de página omitido.** LAT-10 aparecía con un valor de 91 P cuando vale 177 P.
4. **Caducidad:** pedíamos 4 ticks y obteníamos 1. Ahora se calibra y se registra la vigencia efectiva que devuelve el servidor.
5. **`committed` acumulativo en el motor v1.** Las pujas caducadas consumían presupuesto para siempre. Ahora las reservas se calculan desde las ofertas abiertas en el servidor.
6. **Una compra propia marcada como externa:** cuando el vendedor aceptaba nuestra contraoferta, se clasificaba como actividad desconocida.
7. **Dos procesos independientes** (vendedores y mercado) con presupuestos separados. Ahora hay un único coordinador.

## Mercado propio y Market Test: decisión

- **Ahora no se abre.** Requiere nivel 2 y 270 P (250 de fianza reembolsable + 20). Tenemos nivel 1 y unos 187 P, y ese efectivo vale más usado para completar La Latina (LAT-10, +86 P de bono).
- **Puesto gratuito:** los equipos sin mercado reciben un puesto gratuito con mecanismo `auto` cuando abren los mercados de equipo. Según RULES.md, igualar al puesto gratuito da la mitad de los puntos del Market Test; los puntos completos se fijan con la media de los tres mejores. Es decir, el puesto gratuito ya da la mitad sin inmovilizar fianza.
- **Para superar al puesto** hace falta un broker en un mercado `board` que estime los límites ocultos de los operadores sintéticos y su paciencia. `starter_broker.py` solo cruza por cotización, que es lo mismo que hace el puesto gratuito. **Eso falta por implementar.** Solo entonces tiene sentido pagar la fianza (sábado: +150 P de asignación y nivel 2 al abrirse El Chato a todos).
- **Duelos:** hay sesiones programadas (la primera es de práctica y no puntúa). No existe módulo de duelos; queda fuera de esta entrega y no se mezcla con la negociación con vendedores.

## Resultados

Este coordinador no se ha ejecutado todavía en real, así que no hay resultados observados que atribuirle. Hay 97 pruebas offline en verde (`python3 -m unittest test_coordinator test_trading test_market test_negotiation`). Las simulaciones de `sim.py` usan vendedores sintéticos y no son una medida del juego real. Cada operación real queda en `data/coordinator_ledger.json` con sus métricas privadas antes y después, para evaluarla después sin ajustar políticas a una sola observación.
