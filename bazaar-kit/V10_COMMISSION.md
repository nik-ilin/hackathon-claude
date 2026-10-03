# Integración con Team 5 en v10

El coordinador consulta `/api/threads/1179` y solo importa el mensaje `7728` si la API confirma `kind=team`, `sender=t05`
y que las dos partes del hilo son `t05` y `t15`. Se acepta la orientación indicada por Team 5 (`team=t05`, `with=t15`).
Los términos autorizados quedan fijados al lote:

| Carta | Comprador | Precio | Venue |
|---|---|---:|---|
| `LAV-01` | `t09` | 9 P | `v10` |
| `MAL-07` | `t02` | 14 P | `v10` |
| `RET-01` | `t09` | 9 P | `v10` |

`LAV-08` no forma parte de la autorización. La aprobación vence a las 18:30, hora de Madrid, el mismo día en que se
importa. El coordinador no la extiende al día siguiente.

Activa el modo con `./run.sh coord --v10-commission`. `strategy_window.py` incluye el flag para los siguientes ciclos
que lance. Las ofertas usan la API estructurada: una copia concreta de una carta por el precio aprobado, venue `v10`,
destinatario exacto y vencimiento dentro de la autorización. Solo genera candidatas si hay al menos dos copias; la
barrera general del coordinador protege páginas propias completas y activos comprometidos.

El acuerdo prohíbe vender cartas que completen página a los seis equipos líderes. El endpoint del leaderboard no
expone sus colecciones, así que el modo bloquea ofertas autorizadas dirigidas a un equipo del top 6 y las deja visibles
como bloqueadas para revisión. También bloquea si no hay un leaderboard verificable.

La venta suma 1 P de comisión pendiente solo cuando el feed confirma la liquidación de la oferta exacta, en `v10`, al
comprador aprobado y con la carta transferida desde `t15`. El coordinador reconcilia el feed y muestra las ventas y el
importe confirmado. El pago acordado después de las 18:30 (publicar a `t05` una carta por N P y esperar su aceptación)
se mantiene como cierre manual; este modo no publica esa oferta automáticamente.

El cambio de código no altera un coordinador que ya estaba ejecutándose: hay que esperar a que finalice o reiniciarlo
con autorización operativa antes de que cargue el flag nuevo.
