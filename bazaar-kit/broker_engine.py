"""Emparejamiento de libro para el Market Test: maximizar el excedente realizado.

Add-on independiente: lógica pura, biblioteca estándar, SIN red. No importa
ningún módulo del repo. La capa que habla con la API vive en `broker_run.py`.

Qué puntúa (RULES.md, «Your own market»): «your score is the share of the
possible gains you realise», y las ganancias se miden «between the true
limits», no entre las cotizaciones. Igualar al puesto gratuito da la mitad
de los puntos; los puntos completos se fijan con la media de los tres mejores.

CONSECUENCIA QUE CAMBIA EL DISEÑO
---------------------------------
El excedente de un cruce entre el vendedor i y el comprador j es

    (precio − coste_i) + (valor_j − precio) = valor_j − coste_i

es decir **no depende del precio de cruce**. Elegir el punto medio en vez de
la cotización no captura ni un punto más de excedente entre los operadores.
El precio sólo importa por dos vías, ambas reales:

1. **Legalidad.** El motor exige `ask <= price` y `price + fee <= bid`.
   `starter_broker.bench_plan` cotiza el punto medio **sin descontar la
   comisión**: con `fee_bps > 0` el motor rechaza justo los cruces más
   ajustados, los que el puesto gratuito sí hace. Cotizar en el `ask` es el
   precio legal más bajo y nunca pierde un cruce que el punto medio gane.
2. **Fuga por comisión.** La comisión sale del excedente y va al mercado, y
   `fees` no puntúa (RULES.md, «Scoring»). ⌈bps·precio⌉ crece con el precio,
   así que el `ask` es el precio que menos excedente fuga.

Dónde se gana de verdad frente al puesto gratuito:

3. **El emparejamiento, no el precio.** El puesto cruza el `ask` más bajo
   contra el `bid` más alto (`starter_broker.bench_plan`: asks ascendentes
   contra bids descendentes, y para al primer par que no cruza). Eso quema la
   puja más alta —la única que puede cubrir un `ask` caro— en el `ask` más
   barato, que cualquier puja cubría. Emparejar asks ascendentes contra bids
   **ascendentes** cruza más pares. Y como
   Σ(valor_j − coste_i) = Σ valor_matched − Σ coste_matched, cada par extra
   con excedente positivo suma: el excedente total depende de *qué*
   operadores quedan emparejados, nunca de con quién se emparejan.
   Aquí se usa programación dinámica exacta sobre la estructura en escalera
   del libro, que domina a cualquiera de los dos zips.
4. **El tiempo.** El banco dura 16 ticks (`bench.started`, tick 201) y «most
   relax their quotes as their patience runs out». Un cruce hecho hoy consume
   dos operadores; esperar a que las cotizaciones se relajen permite más
   pares, pero arriesga que alguien se marche. `BrokerConfig.defer` compara
   las dos cosas con los límites estimados.

La estimación de límites, por tanto, no sirve para elegir el precio: sirve
para **ordenar y para decidir si esperar**. Si el sombreado es hacia fuera
(coste ≤ ask ≤ bid ≤ valor) todo par que cruza por cotización tiene excedente
verdadero positivo, así que la estimación nunca inventa cruces; sí decide qué
par vale más cuando hay que elegir, y cuánto se gana esperando.

FORMATO DE LA API
-----------------
VERIFICADO (contra el servidor hoy, tick 250, y contra `starter_broker.py`,
que es código oficial):

- `GET /api/venues/{id}/offers` devuelve ofertas con
  `{"id": 3417, "maker": "ma55bf699", "give": {"cash": 0, "assets": [{"kind":
  "card", "ref": "LAT-05", ...}], "types": []}, "want": {"cash": 7, "assets":
  [], "types": []}, "status": "open", "expires_tick": ..., "final": false}`.
  El libro del broker trae las mismas ofertas en `offers` (`starter_broker`).
- Una venta: `give.assets` con la carta y `want.cash` = el `ask`.
  Una compra: `give.cash` = el `bid` y `want.types` = `["card:LAT-05"]`
  (`starter_broker.public_plan` construye exactamente esa cadena).
- `GET /api/broker/book` exige `X-Broker-Key`: sin clave responde 401
  `{"error": "bad_key"}`. **No se ha leído ningún libro real.**
- Las claves `bench_offers`, `offers`, `fee_bps` y `fee_per_card` del libro,
  y los ids de banco en forma `"b3-7"` donde `b3` es la corrida: de
  `starter_broker.py` y de la docstring de `Broker.match`.
- La comisión del mercado: ⌈bps·precio/10000⌉ + por_carta, la paga quien
  acepta (`starter_broker.public_plan`, y cuadra con El Rastro en MARKET.md).

SUPUESTO (no verificado, marcado como tal en el código):

- Que una corrida de banco (`b3`) negocia **una sola carta**. `bench_plan`
  empareja por corrida sin mirar la carta, así que agrupamos por corrida y
  sólo bajamos a (corrida, carta) si la corrida nombra más de una.
- Que cada oferta mueve **una** carta. Si llega `qty`/`quantity` > 1 la
  oferta se aparta en `Book.unsupported` en vez de adivinar cómo partirla.
- Que los operadores de banco cotizan **hacia fuera** de un límite oculto
  (lo dice la docstring de `starter_broker`, no se ha medido cuánto).
- Que costes y valores salen de la misma distribución: de ahí sale el
  estimador de sombreado. Es la hipótesis más frágil de este archivo.
- La paciencia, la salida y el ritmo de relajación de cada operador: no hay
  ningún campo documentado. Si el libro trae algo parecido se lee
  (`patience`, `final`, `expires_tick`), y si no, no se inventa.
- Que el precio de cruce no afecta la puntuación. Se sigue de la definición
  del excedente, no de una medición; `price_policy="mid_capped"` deja
  cambiarlo con una línea si el jurado dice lo contrario.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

# --------------------------------------------------------------- comisiones


@dataclass(frozen=True)
class Fees:
    """La comisión del mercado. ⌈bps·precio/10000⌉ + por_carta (VERIFICADO)."""

    bps: int = 0
    per_card: int = 0

    def on(self, price: int, cards: int = 1) -> int:
        return math.ceil(self.bps * int(price) / 10000) + self.per_card * cards


# ------------------------------------------------------------------ ofertas

SELL, BUY = "sell", "buy"


@dataclass(frozen=True)
class Offer:
    """Una oferta del libro, normalizada.

    `quote` es el `ask` si vende y el `bid` si compra: es la cotización
    pública, nunca el límite. `run` es el grupo dentro del que se puede
    emparejar.
    """

    id: object
    run: str
    side: str
    quote: int
    card: Optional[str] = None
    maker: Optional[str] = None
    bench: bool = False
    raw: dict = field(default_factory=dict, repr=False)


def _cash(side: object) -> int:
    try:
        return int((side or {}).get("cash") or 0)  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return 0


def _assets(side: object) -> list:
    try:
        return [a for a in ((side or {}).get("assets") or []) if isinstance(a, dict)]  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return []


def _types(side: object) -> list[str]:
    try:
        return [t for t in ((side or {}).get("types") or []) if isinstance(t, str)]  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return []


def _ref(text: str) -> str:
    """"card:LAT-05" -> "LAT-05"; cualquier otra cosa se deja tal cual."""
    return text.split(":", 1)[1] if ":" in text else text


def _qty(raw: dict) -> int:
    """Cantidad declarada. Hoy ninguna ruta documentada la trae; se lee por si acaso."""
    for key in ("qty", "quantity", "count", "lots"):
        if key in raw:
            try:
                return max(1, int(raw[key]))
            except (TypeError, ValueError):
                pass
    return 1


def parse_offer(raw: dict, *, bench: bool = False) -> Optional[Offer]:
    """Normaliza una oferta, o None si no es un cruce de una carta por efectivo.

    Tolerante a propósito: un campo nuevo no rompe nada, y una forma que no
    entendemos se descarta en vez de adivinarse. Nunca lanza.
    """
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    if raw.get("status") not in (None, "open"):  # el banco puede no traer status
        return None
    if _qty(raw) != 1:  # SUPUESTO: una carta por oferta; un lote se aparta
        return None
    give, want = raw.get("give") or {}, raw.get("want") or {}
    side = raw.get("side")
    gcards, wtypes = _assets(give), _types(want)
    card: Optional[str] = None
    if side not in (SELL, BUY):
        if _cash(want) > 0 and len(gcards) == 1 and not _cash(give):
            side = SELL
        elif _cash(give) > 0 and len(wtypes) == 1 and not _cash(want):
            side = BUY
        else:
            return None  # trueque, lote, sobre, oferta vacía: no es nuestro trabajo
    if side == SELL:
        if len(gcards) != 1 or gcards[0].get("kind") not in (None, "card"):
            return None  # un sobre sellado no es una carta: no se cruza
        quote, card = _cash(want), gcards[0].get("ref")
    else:
        quote = _cash(give)
        card = _ref(wtypes[0]) if wtypes else None
    if quote <= 0:
        return None
    run = str(raw.get("run") or "")
    if not run:
        oid = str(raw["id"])
        run = oid.split("-")[0] if bench and "-" in oid else (card or oid)
    return Offer(id=raw["id"], run=run, side=side, quote=int(quote), card=card,
                 maker=raw.get("maker"), bench=bool(bench), raw=raw)


@dataclass(frozen=True)
class Book:
    """El libro del broker ya normalizado."""

    tick: Optional[int] = None
    fees: Fees = Fees()
    bench: tuple[Offer, ...] = ()
    public: tuple[Offer, ...] = ()
    unsupported: tuple[dict, ...] = ()
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def offers(self) -> tuple[Offer, ...]:
        return self.bench + self.public


#: Nombres alternativos por si el servidor renombra una clave a mitad del juego.
_BENCH_KEYS = ("bench_offers", "bench", "bench_book", "synthetic_offers")
_PUBLIC_KEYS = ("offers", "public_offers", "orders")


def _first_list(raw: dict, keys: Sequence[str]) -> list:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, list):
            return v
    return []


def parse_book(raw: dict) -> Book:
    """Lee `GET /api/broker/book`. Nunca lanza: un libro raro da un libro vacío."""
    if not isinstance(raw, dict):
        return Book()
    fees = Fees(int(raw.get("fee_bps") or 0), int(raw.get("fee_per_card") or 0))
    bench, public, bad = [], [], []
    for group, key_set, flag in ((bench, _BENCH_KEYS, True), (public, _PUBLIC_KEYS, False)):
        for item in _first_list(raw, key_set):
            o = parse_offer(item, bench=flag) if isinstance(item, dict) else None
            (group.append(o) if o else bad.append(item))
    tick = raw.get("tick")
    return Book(tick=int(tick) if isinstance(tick, int) else None, fees=fees,
                bench=tuple(bench), public=tuple(public), unsupported=tuple(bad), raw=raw)


def runs(offers: Iterable[Offer]) -> dict[str, tuple[list[Offer], list[Offer]]]:
    """corrida -> (ventas, compras). Si una corrida nombra más de una carta se
    parte por carta: emparejar cartas distintas sería un cruce que el motor
    rechazaría."""
    by_run: dict[str, list[Offer]] = {}
    for o in offers:
        by_run.setdefault(o.run, []).append(o)
    out: dict[str, tuple[list[Offer], list[Offer]]] = {}
    for run, group in by_run.items():
        cards = {o.card for o in group if o.card}
        keys = [(run, None)] if len(cards) <= 1 else [(run, c) for c in sorted(cards)]
        for run_key, card in keys:
            part = group if card is None else [o for o in group if o.card == card]
            name = run_key if card is None else f"{run_key}/{card}"
            out[name] = ([o for o in part if o.side == SELL], [o for o in part if o.side == BUY])
    return out


# ------------------------------------------------- estimación de los límites


@dataclass(frozen=True)
class LimitModel:
    """Límites ocultos estimados a partir del sombreado de las cotizaciones.

    Modelo: el vendedor pide `coste·(1+θ)` y el comprador puja `valor·(1−θ)`.
    ESTIMACIÓN, no dato: θ sale del libro suponiendo que costes y valores
    vienen de la misma distribución, así que la mediana de los asks y la de
    los bids se separan simétricamente alrededor de la mediana común μ:

        mediana_ask ≈ μ(1+θ),  mediana_bid ≈ μ(1−θ)
        θ̂ = (mediana_ask − mediana_bid) / (mediana_ask + mediana_bid)
    """

    shade: float
    source: str  # "book" (estimado) | "default" (no había con qué estimar)

    def cost(self, ask: float) -> float:
        return ask / (1.0 + self.shade)

    def value(self, bid: float) -> float:
        return bid / (1.0 - self.shade) if self.shade < 1.0 else float(bid)

    def cost_of(self, o: Offer) -> float:
        return self.cost(o.quote)

    def value_of(self, o: Offer) -> float:
        return self.value(o.quote)


def estimate_limits(offers: Iterable[Offer], cfg: "BrokerConfig") -> LimitModel:
    """θ̂ del libro, recortado a [0, max_shade]; el defecto si no se puede."""
    offers = list(offers)
    asks = [o.quote for o in offers if o.side == SELL]
    bids = [o.quote for o in offers if o.side == BUY]
    if not asks or not bids:
        return LimitModel(cfg.default_shade, "default")
    ma, mb = statistics.median(asks), statistics.median(bids)
    if ma + mb <= 0:
        return LimitModel(cfg.default_shade, "default")
    theta = (ma - mb) / (ma + mb)
    if theta <= 0:  # las pujas ya superan a las ventas: nadie está sombreando mucho
        return LimitModel(cfg.default_shade, "default")
    return LimitModel(min(theta, cfg.max_shade), "book")


# --------------------------------------------------------------- el plan


@dataclass(frozen=True)
class BrokerConfig:
    price_policy: str = "ask"       # "ask" (el legal más bajo) | "mid_capped" | "mid" (el puesto)
    default_shade: float = 0.15     # θ cuando el libro no da para estimarlo
    max_shade: float = 0.45         # tope: un θ absurdo convertiría un ask en un coste ridículo
    min_surplus: float = 0.0        # excedente estimado mínimo, neto de comisión, para cruzar
    max_matches: Optional[int] = None  # el tablón público admite 10 por tick
    allow_same_maker: bool = False  # el banco usa pseudónimos; en el tablón nunca
    defer: bool = False             # esperar a que se relajen las cotizaciones
    relax: float = 0.25             # cuánto se espera que se relajen por tick (SUPUESTO)
    survive: float = 0.85           # prob. de que un operador siga ahí el próximo tick (SUPUESTO)


@dataclass(frozen=True)
class Match:
    sell: object
    buy: object
    price: int
    run: str
    est_surplus: float
    reason: str = ""

    def as_payload(self) -> dict:
        """El cuerpo de `POST /api/broker/matches` (VERIFICADO en bazaar_sdk.Broker.match)."""
        return {"sell": self.sell, "buy": self.buy, "price": int(self.price)}


def cross_price(ask: int, bid: int, fees: Fees, *, policy: str = "ask", cards: int = 1) -> Optional[int]:
    """El precio de cruce, o None si no hay ninguno legal.

    Legal = `ask <= price` y `price + fee(price) <= bid`. Como la comisión no
    decrece con el precio, si el `ask` no es legal no lo es ningún precio.
    """
    ask, bid = int(ask), int(bid)
    if ask + fees.on(ask, cards) > bid:
        return None
    if policy == "ask":
        return ask
    mid = (ask + bid) // 2
    if policy == "mid":  # el puesto gratuito: no descuenta la comisión, así que puede ser ilegal
        return mid
    for p in range(max(mid, ask), ask - 1, -1):  # "mid_capped": el punto medio bajado hasta que sea legal
        if p + fees.on(p, cards) <= bid:
            return p
    return ask


def is_legal(m: Match, sell: Offer, buy: Offer, fees: Fees, cards: int = 1) -> bool:
    """Lo que el motor comprobará antes de aceptar el emparejamiento."""
    return sell.quote <= m.price and m.price + fees.on(m.price, cards) <= buy.quote


# ------------------------------------------------- 2. el cruce por cotización


def plan_quote_cross(book: Book, cfg: BrokerConfig = BrokerConfig()) -> list[Match]:
    """La referencia a batir: lo que hace `starter_broker.bench_plan` y el puesto
    gratuito. Asks ascendentes contra bids **descendentes**, parando al primer
    par que no cruza, al punto medio y sin descontar la comisión.

    Se reproduce tal cual (incluido el punto medio ilegal) porque es la línea
    base contra la que se mide; `legal_matches` enseña cuántos rechaza el motor.
    """
    out: list[Match] = []
    for name, (sells, buys) in sorted(runs(book.offers).items()):
        for s, b in zip(sorted(sells, key=lambda o: o.quote),
                        sorted(buys, key=lambda o: -o.quote)):
            if b.quote < s.quote:
                break
            if not cfg.allow_same_maker and s.maker is not None and s.maker == b.maker:
                continue
            out.append(Match(s.id, b.id, (s.quote + b.quote) // 2, name,
                             float(b.quote - s.quote), "quote cross (free stall)"))
    return out[: cfg.max_matches] if cfg.max_matches else out


# ---------------------------------- 3. el emparejamiento que maximiza excedente


def _staircase_match(sells: list[Offer], buys: list[Offer],
                     feasible: Callable[[Offer, Offer], bool],
                     weight: Callable[[Offer, Offer], float]) -> list[tuple[Offer, Offer]]:
    """Emparejamiento de peso máximo exacto sobre un libro en escalera.

    Con ventas y compras ordenadas **ambas** de forma ascendente, la
    factibilidad `ask + fee <= bid` es monótona: si (i,j) cruza, también cruzan
    (i',j) con i'<i y (i,j') con j'>j. En un grafo así existe un óptimo sin
    cruces (argumento de intercambio: dos pares cruzados se pueden desenredar
    sin perder factibilidad, y el peso valor_j − coste_i es separable, así que
    desenredar no cambia la suma). Por eso basta la programación dinámica
    sobre los dos índices, en O(m·n), y no hace falta un Hungarian.

    `test_broker_engine` lo compara contra fuerza bruta en libros pequeños.
    """
    S = sorted(sells, key=lambda o: (o.quote, str(o.id)))
    B = sorted(buys, key=lambda o: (o.quote, str(o.id)))
    m, n = len(S), len(B)
    best = [[0.0] * (n + 1) for _ in range(m + 1)]
    take = [[False] * (n + 1) for _ in range(m + 1)]
    skip_sell = [[True] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            value, taken, drop_sell = best[i + 1][j], False, True
            if best[i][j + 1] > value:
                value, drop_sell = best[i][j + 1], False
            if feasible(S[i], B[j]):
                w = weight(S[i], B[j]) + best[i + 1][j + 1]
                if w >= value:  # >= : ante el empate se cruza, porque un par más nunca resta
                    value, taken = w, True
            best[i][j], take[i][j], skip_sell[i][j] = value, taken, drop_sell
    pairs, i, j = [], 0, 0
    while i < m and j < n:
        if take[i][j]:
            pairs.append((S[i], B[j]))
            i, j = i + 1, j + 1
        elif skip_sell[i][j]:
            i += 1
        else:
            j += 1
    return pairs


def _plan_run(name: str, sells: list[Offer], buys: list[Offer], book: Book,
              model: LimitModel, cfg: BrokerConfig) -> list[Match]:
    def price_of(s: Offer, b: Offer) -> Optional[int]:
        return cross_price(s.quote, b.quote, book.fees, policy=cfg.price_policy)

    def surplus(s: Offer, b: Offer) -> float:
        """Excedente estimado entre límites, neto de la comisión que se fuga."""
        p = price_of(s, b)
        fee = book.fees.on(p) if p is not None else 0
        return model.value_of(b) - model.cost_of(s) - fee

    def feasible(s: Offer, b: Offer) -> bool:
        if not cfg.allow_same_maker and s.maker is not None and s.maker == b.maker:
            return False
        p = price_of(s, b)
        if p is None or p < s.quote or p + book.fees.on(p) > b.quote:
            return False  # "mid" puede dar un precio ilegal: no se planifica
        return surplus(s, b) >= cfg.min_surplus

    out = []
    for s, b in _staircase_match(sells, buys, feasible, lambda x, y: model.value_of(y) - model.cost_of(x)):
        p = price_of(s, b)
        assert p is not None  # feasible() ya lo garantizó
        out.append(Match(s.id, b.id, p, name, surplus(s, b),
                         f"surplus plan (shade {model.shade:.2f} {model.source})"))
    return out


def _relaxed(offers: Sequence[Offer], model: LimitModel, relax: float) -> list[Offer]:
    """El libro un tick después si cada operador acerca su cotización a su límite.

    SUPUESTO: la relajación es una fracción fija de la distancia que queda.
    `starter_broker` dice que «most relax their quotes as their patience runs
    out» y que «the firm ones never do», pero no cuánto ni quiénes.
    """
    out = []
    for o in offers:
        limit = model.cost_of(o) if o.side == SELL else model.value_of(o)
        moved = o.quote + relax * (limit - o.quote)
        q = max(1, int(math.floor(moved)) if o.side == SELL else int(math.ceil(moved)))
        out.append(Offer(o.id, o.run, o.side, q, o.card, o.maker, o.bench, o.raw))
    return out


def _relaxed_true(offers: Sequence[Offer], truth: "Truth", relax: float) -> list[Offer]:
    """Igual que `_relaxed`, pero hacia el límite **verdadero**: así se comporta
    un operador, que acerca su cotización a su límite y nunca la pasa. Sólo lo
    usa el simulador, que es el único que conoce los límites."""
    out = []
    for o in offers:
        limit = truth.of(o.id)
        if limit is None:
            out.append(o)
            continue
        moved = o.quote + relax * (limit - o.quote)
        # Nunca más allá del límite: un vendedor no pide menos que su coste.
        q = max(1, math.ceil(moved) if o.side == SELL else math.floor(moved))
        q = max(q, math.ceil(limit)) if o.side == SELL else min(q, math.floor(limit))
        out.append(Offer(o.id, o.run, o.side, max(1, int(q)), o.card, o.maker, o.bench, o.raw))
    return out


def plan(book: Book, cfg: BrokerConfig = BrokerConfig(), *, ticks_left: Optional[int] = None) -> list[Match]:
    """El plan del tick: los emparejamientos que más excedente realizan.

    Con `cfg.defer` y ticks por delante, un cruce se aparta si el mismo
    vendedor, en el libro relajado de un tick después, se emparejaría con un
    comprador bastante mejor como para cubrir el riesgo de que alguien se vaya.
    """
    model = estimate_limits(book.offers, cfg)
    out: list[Match] = []
    for name, (sells, buys) in sorted(runs(book.offers).items()):
        here = _plan_run(name, sells, buys, book, model, cfg)
        if cfg.defer and ticks_left is not None and ticks_left > 1:
            here = _deferred(name, here, sells, buys, book, model, cfg)
        out.extend(here)
    out.sort(key=lambda m: (-m.est_surplus, str(m.run), str(m.sell)))  # lo mejor primero por si hay tope
    return out[: cfg.max_matches] if cfg.max_matches else out


def _deferred(name: str, here: list[Match], sells: list[Offer], buys: list[Offer],
              book: Book, model: LimitModel, cfg: BrokerConfig) -> list[Match]:
    """Aparta los cruces que mejoran bastante esperando un tick.

    Esperar paga si el excedente del mejor par futuro del mismo vendedor,
    descontado por la probabilidad de que las dos partes sigan ahí, supera el
    excedente de cruzar hoy. Con `survive=0.85` eso exige una mejora de más
    del 38 % (1/0.85² − 1), así que no se aparta nada por un margen pequeño.
    """
    later = _plan_run(name, _relaxed(sells, model, cfg.relax), _relaxed(buys, model, cfg.relax),
                      book, model, cfg)
    future = {m.sell: m.est_surplus for m in later}
    keep, both_stay = [], cfg.survive ** 2
    for m in here:
        if future.get(m.sell, 0.0) * both_stay > m.est_surplus:
            continue  # se espera: el vendedor vale más contra el comprador de mañana
        keep.append(m)
    return keep


# ------------------------------------------------------------- 4. la medida


@dataclass(frozen=True)
class Truth:
    """Los límites verdaderos. Sólo existen en los libros sintéticos de aquí:
    el servidor no los publica nunca."""

    cost: dict[object, float] = field(default_factory=dict)
    value: dict[object, float] = field(default_factory=dict)

    def of(self, offer_id: object) -> Optional[float]:
        return self.cost.get(offer_id, self.value.get(offer_id))


def possible_surplus(book: Book, truth: Truth) -> float:
    """El excedente máximo alcanzable: dentro de cada corrida, los costes más
    bajos contra los valores más altos mientras el valor supere al coste.
    Sin la restricción de las cotizaciones, que es sombreado, no economía."""
    total = 0.0
    for _, (sells, buys) in runs(book.offers).items():
        costs = sorted(truth.cost[s.id] for s in sells if s.id in truth.cost)
        values = sorted((truth.value[b.id] for b in buys if b.id in truth.value), reverse=True)
        for c, v in zip(costs, values):
            if v <= c:
                break
            total += v - c
    return total


def legal_matches(matches: Sequence[Match], book: Book, cfg: BrokerConfig = BrokerConfig()
                  ) -> tuple[list[Match], list[Match]]:
    """(aceptados, rechazados) como lo haría el motor: precio legal, cada oferta
    una sola vez y nunca las dos puntas del mismo maker."""
    by_id = {o.id: o for o in book.offers}
    used: set[object] = set()
    ok, bad = [], []
    for m in matches:
        s, b = by_id.get(m.sell), by_id.get(m.buy)
        if (s is None or b is None or s.side != SELL or b.side != BUY
                or m.sell in used or m.buy in used
                or (not cfg.allow_same_maker and s.maker is not None and s.maker == b.maker)
                or not is_legal(m, s, b, book.fees)):
            bad.append(m)
            continue
        used.update((m.sell, m.buy))
        ok.append(m)
    return ok, bad


def realized_surplus(matches: Sequence[Match], book: Book, truth: Truth, *,
                     charge_fee: bool = True) -> float:
    """El excedente que realizan de verdad los emparejamientos aceptados.

    = Σ (valor_comprador − coste_vendedor), menos la comisión si se cobra: la
    comisión sale del excedente de los operadores y las comisiones no puntúan.
    """
    ok, _ = legal_matches(matches, book)
    total = 0.0
    for m in ok:
        c, v = truth.cost.get(m.sell), truth.value.get(m.buy)
        if c is None or v is None:
            continue
        total += v - c - (book.fees.on(m.price) if charge_fee else 0)
    return total


def efficiency(matches: Sequence[Match], book: Book, truth: Truth, **kw) -> float:
    """La fracción del excedente posible que se realiza. Es lo que puntúa."""
    best = possible_surplus(book, truth)
    return realized_surplus(matches, book, truth, **kw) / best if best > 0 else 0.0


# --------------------------------------------- libros sintéticos para medir


def synth_book(seed: int = 0, *, runs_n: int = 3, per_side: int = 6, fees: Fees = Fees(),
               shade: tuple[float, float] = (0.10, 0.40), level: tuple[int, int] = (8, 40),
               spread: float = 0.35, lopsided: float = 0.0) -> tuple[Book, Truth]:
    """Un libro de banco con límites conocidos, para medir estrategias.

    Generador (SUPUESTOS, no el banco real): en cada corrida hay un nivel de
    precio μ; cada vendedor tiene un coste y cada comprador un valor alrededor
    de μ, y cada uno cotiza sombreando su límite una fracción propia. Con
    `lopsided` un lado tiene menos operadores que el otro, para que haya
    operadores sin contraparte.
    """
    rng = random.Random(seed)
    bench, cost, value = [], {}, {}
    for r in range(runs_n):
        run, mu = f"b{r + 1}", rng.uniform(*level)
        card = f"SYN-{r + 1:02d}"
        n_sell = max(1, per_side - int(round(per_side * lopsided)))
        for k in range(n_sell):
            c = max(1.0, rng.gauss(mu, mu * spread))
            ask = max(1, int(round(c * (1 + rng.uniform(*shade)))))
            oid = f"{run}-s{k}"
            cost[oid] = c
            bench.append({"id": oid, "give": {"cash": 0, "assets": [{"kind": "card", "ref": card}]},
                          "want": {"cash": ask, "types": []}, "maker": f"{run}-m{k}"})
        for k in range(per_side):
            v = max(2.0, rng.gauss(mu, mu * spread))
            bid = max(1, int(round(v * (1 - rng.uniform(*shade)))))
            oid = f"{run}-b{k}"
            value[oid] = v
            bench.append({"id": oid, "give": {"cash": bid, "assets": [], "types": []},
                          "want": {"cash": 0, "types": [f"card:{card}"]}, "maker": f"{run}-n{k}"})
    raw = {"tick": 201, "fee_bps": fees.bps, "fee_per_card": fees.per_card, "bench_offers": bench, "offers": []}
    return parse_book(raw), Truth(cost, value)


def compare(seeds: Iterable[int] = range(40), *, cfg: Optional[BrokerConfig] = None,
            **book_kw) -> dict:
    """Base (cruce por cotización) frente al plan de excedente, sobre varios libros."""
    cfg = cfg or BrokerConfig()
    rows = []
    for seed in seeds:
        book, truth = synth_book(seed, **book_kw)
        base, mine = plan_quote_cross(book), plan(book, cfg)
        ok_b, bad_b = legal_matches(base, book)
        ok_m, bad_m = legal_matches(mine, book)
        rows.append({
            "seed": seed,
            "possible": possible_surplus(book, truth),
            "base_eff": efficiency(base, book, truth),
            "plan_eff": efficiency(mine, book, truth),
            "base_matches": len(ok_b), "base_refused": len(bad_b),
            "plan_matches": len(ok_m), "plan_refused": len(bad_m),
        })
    agg = lambda k: statistics.fmean(r[k] for r in rows)  # noqa: E731
    return {"rows": rows, "n": len(rows),
            "base_eff": agg("base_eff"), "plan_eff": agg("plan_eff"),
            "base_matches": agg("base_matches"), "plan_matches": agg("plan_matches"),
            "base_refused": agg("base_refused"), "plan_refused": agg("plan_refused"),
            "lift": (agg("plan_eff") / agg("base_eff") - 1.0) if agg("base_eff") > 0 else float("inf")}


# ------------------------------- la sesión completa: 16 ticks con relajación


def simulate(seed: int = 0, *, strategy: str = "plan", cfg: Optional[BrokerConfig] = None,
             ticks: int = 16, relax: float = 0.2, leave: float = 0.06,
             fees: Fees = Fees(), **book_kw) -> dict:
    """Una sesión de Market Test entera, con cotizaciones que se relajan y
    operadores que se van. Mide la fracción del excedente posible inicial.

    SUPUESTO entero: el servidor no documenta ni la relajación ni las salidas.
    Sirve para comparar estrategias entre sí, no para predecir la puntuación.
    """
    cfg = cfg or BrokerConfig()
    rng = random.Random(10_000 + seed)
    book0, truth = synth_book(seed, fees=fees, **book_kw)
    best = possible_surplus(book0, truth)
    live = {o.id: o for o in book0.offers}
    done = 0.0
    for t in range(ticks):
        now = Book(tick=201 + t, fees=fees, bench=tuple(live.values()), raw={})
        matches = plan_quote_cross(now, cfg) if strategy == "base" else plan(now, cfg, ticks_left=ticks - t)
        ok, _ = legal_matches(matches, now)
        for m in ok:
            done += (truth.value[m.buy] - truth.cost[m.sell] - fees.on(m.price))
            live.pop(m.sell, None)
            live.pop(m.buy, None)
        nxt = {}
        for o in _relaxed_true(tuple(live.values()), truth, relax):
            if rng.random() >= leave:  # el impaciente se va sin cruzar: excedente perdido
                nxt[o.id] = o
        live = nxt
    return {"seed": seed, "strategy": strategy, "possible": best, "realized": done,
            "efficiency": done / best if best > 0 else 0.0, "left_open": len(live)}


def session_compare(seeds: Iterable[int] = range(20), **kw) -> dict:
    base = [simulate(s, strategy="base", **kw) for s in seeds]
    mine = [simulate(s, strategy="plan", **kw) for s in seeds]
    b, m = statistics.fmean(r["efficiency"] for r in base), statistics.fmean(r["efficiency"] for r in mine)
    return {"base_eff": b, "plan_eff": m, "lift": (m / b - 1.0) if b > 0 else float("inf"), "n": len(base)}


def report(seeds: Iterable[int] = range(40), **kw) -> str:
    """Una tabla legible: es la prueba de que el plan vale más que la base."""
    lines = ["BANCO ESTÁTICO (un libro, un tick)"]
    for label, fees in (("sin comisión", Fees()), ("comisión 3 %", Fees(300)), ("3 % + 1 P", Fees(300, 1))):
        c = compare(seeds, fees=fees, **kw)
        lines.append(f"  {label:<14} base {c['base_eff']:6.1%} ({c['base_matches']:.1f} cruces, "
                     f"{c['base_refused']:.1f} rechazados) -> plan {c['plan_eff']:6.1%} "
                     f"({c['plan_matches']:.1f} cruces)   +{c['lift']:.0%}")
    lines.append("SESIÓN DE 16 TICKS (comisión 3 %, cotizaciones que se relajan, operadores que se van)")
    for label, cfg in (("sin esperar", BrokerConfig()), ("esperando (defer)", BrokerConfig(defer=True))):
        s = session_compare(range(20), cfg=cfg, fees=Fees(300))
        lines.append(f"  {label:<18} base {s['base_eff']:6.1%} -> plan {s['plan_eff']:6.1%}   {s['lift']:+.0%}")
    lines.append("  Esperar rinde MENOS: el puesto tiene 16 ticks para recuperar el par que se dejó,")
    lines.append("  así que apartar un cruce sólo arriesga que alguien se vaya. `defer` va apagado.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
