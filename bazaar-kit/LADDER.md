# La escalera de vendedores: por qué no estábamos puntuando

Medido en el leaderboard del tick 820 (ronda 2, Saturday · Gran Vía, peso 1.0).

## 1. Qué separa al top

Correlación con `score` sobre los 17 equipos activos:

| variable         | r      |
|------------------|--------|
| `negotiating`    | **+0.852** |
| `market`         | +0.138 |
| `level`          | +0.089 |
| `album_filled`   | +0.005 |
| `pages_complete` | **−0.095** |
| `deals`          | **−0.353** |
| `luck`           | −0.343 |

`deals` sale NEGATIVO. No es volumen: es rango capturado por trato.

Puntos de negociación por trato:

```
t01  0.939  (23 tratos, 1 página, álbum 31)  -> rank 3
t03  0.938  (25 tratos, 1 página, álbum 32)  -> rank 6
t14  0.643  (32 tratos, 2 páginas)           -> rank 1
t15  0.356  (45 tratos, 3 páginas, álbum 44) -> rank 11
```

Tenemos el álbum más grande del juego y la peor eficiencia por trato del grupo
de cabeza. El valor de colección no es el problema: es el regateo.

Confirmado además por medición directa: completar El Retiro añadió +197.5 P de
valor de colección y el score bajó de 22.00 a 21.71. El bono de página no
puntúa. Vender SAL-08 (12.5 P de valor privado) a 25 P dio **+1.37**.

## 2. Los dos defectos

**a) No existía el canal de venta a vendedores.** `negotiation.item_of()` leía
sólo `topic["buy"]` y devolvía `None` para `{"sell": {"assets": [...]}}`, así que
el hilo se descartaba completo. `coordinator.candidates()` recorría únicamente
`menu["sells"]` (lo que el vendedor vende), nunca `menu["buys"]`. El SDK ya lo
soportaba (`bazaar_sdk.py:196`). Es decir: la única acción que ha dado puntos hoy
no estaba implementada.

**b) `dealer_policy()` tenía la asignación invertida.** La abuela
(patience 0.85, shrewdness 0.20 — la blanda) se llevaba la política paciente, y
chato, pilar y picaros caían en un fallback de `open_frac=0.90` con UNA
contraoferta y `accept_on_concession=True`. Abrir al 90 % de su precio captura
~10 % del rango, y la escalera paga exactamente cuota de rango.

## 3. Lo que hace este PR

- `item_of()` reconoce topics de venta; `is_sell()` y `sell_assets()` nuevos.
- `price_floor(value_lost, margin)`: suelo de venta a partir de
  `Valuation.delta(counts, add=0, remove={ref: 1})`, que **ya incluye el bono de
  página que se rompería**. Un común de página completa sale con suelo 110 P, así
  que no se vende por accidente.
- `decide_dealer_sell()`: simétrico de `decide_dealer`. Su precio es una PUJA, así
  que abrimos alto y concedemos bajando; nunca por debajo del suelo, nunca
  repetimos ni subimos una petición ya hecha, y en modo score nunca cerramos a su
  puja de apertura (su apertura es su puja más BAJA: aceptarla captura rango 0).
- `dealer_policy()` por vendedor, derivada de los rasgos de `/api/dealers`:

  | vendedor | patience/shrewd | open_frac | gap | contraof. | ticks | sell_open |
  |---|---|---|---|---|---|---|
  | abuela  | 0.85 / 0.20 | 0.45 | 0.30 | 3 | 8 | 2.4x |
  | chato   | 0.35 / 0.85 | 0.55 | 0.40 | 2 | 5 | 1.8x |
  | pilar   | 0.60 / 0.75 | 0.50 | 0.35 | 3 | 6 | 2.2x |
  | picaros | 0.40 / 0.70 | 0.50 | 0.40 | 2 | 5 | 2.0x |

  `accept_on_concession=False` para todos en modo score: aceptar la primera
  rebaja cierra en la parte baja del rango y gasta una de las 3 casillas del nivel.
- `coordinator`: los hilos de venta se atienden con suelo en vez de techo, y una
  sección nueva propone ventas desde `menu["buys"]`, ordenadas por nivel del
  vendedor (los niveles altos pesan más) y por valor privado entregado (menos es
  mejor). Ejecuta `dealer_sell_open` con
  `open_thread(dealer, topic={"sell": {"assets": [id]}})`.
- Guardas: una venta que rompería una página completa sale **bloqueada** salvo
  `--allow-last-copy <REF>`. A `picaros` se le añade siempre el aviso de validar
  los assets de cada oferta (`POST /api/flags`).

## 4. Reparto de munición (13 cartas libres, 12 casillas)

| nivel | vendedor | compra | enviar |
|---|---|---|---|
| 4 | picaros | común, infrecuente | `SAL-01` `SAL-02` `SAL-03` |
| 3 | pilar | infrec./rara/épica | `MAL-06` + faltan 2 |
| 2 | chato | infrec./rara | `MAL-08`, `MAL-07` (2ª copia) |
| 1 | abuela | común, infrecuente | `SAL-04` `MAL-01` `MAL-02` |

A picaros van comunes de Salamanca, no de Malasaña: el rango de la escalera es
el mismo (book 10) y un común de SAL cuesta 5 P de valor privado frente a 11 P.

El cuello de botella son los infrecuentes: chato y pilar necesitan 6 y sólo hay
4 copias. El Retiro, La Latina y Lavapiés tienen 3 infrecuentes cada uno
bloqueados en páginas completas que no puntúan; `--allow-last-copy` es la puerta,
y conviene abrirla con **una** carta midiendo el score antes y después.

## 5. Lo que este PR NO hace

- No toca duelos (`duels_run.py` sigue parado a propósito: otra persona juega en
  vivo con la clave del equipo).
- No abre venue `board`. Cuesta 270 P y hay 74 P. Medido: todo venue con 0 trades
  puntúa 7.47–7.50 sea `board` o `auto`, así que abrirlo sin broker que cruce no
  penaliza pero tampoco suma. Los 4 mejores mercados del juego (12.50, 12.12,
  11.48, 9.38) son todos `board` con tráfico real; el techo de cualquier `auto`
  es 9.26 y los rangos no se solapan.
- No cambia el modelo de valoración ni los multiplicadores privados.
