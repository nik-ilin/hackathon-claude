"""Inteligencia de negociación a partir del feed público.

Add-on independiente: no importa ni modifica ningún módulo existente.
Lógica pura, sin red, biblioteca estándar. La recolección vive en
`feed_watch.py`; aquí sólo se interpretan eventos ya recogidos.

Qué observa el feed público (verificado, tick 189-214):

- `settlement` con `kind: "trade"`: precio **liquidado**, partes, venue,
  comisión y las cartas movidas. Es la única evidencia de precio real.
- `offer.listed`: lo que un equipo **pide** por una carta, con su ID real.
- `thread.message` con `kind: "persona"`: la negociación de *otros* equipos
  con los dealers, texto y oferta estructurada. De ahí sale la escalera de
  concesiones y el suelo de cada dealer sin gastar cuota propia.

Un precio pedido no es un valor: `settled` y `quoted` se mantienen separados.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

# ---------------------------------------------------------------- utilidades

#: Los dealers no son equipos; sus IDs no empiezan por "t" + dígitos.
def is_team(actor: object) -> bool:
    """`t04` sí; `abuela`, `chato`, `None` no."""
    return isinstance(actor, str) and len(actor) == 3 and actor[0] == "t" and actor[1:].isdigit()


def _cards(side: dict) -> list[dict]:
    """Las cartas concretas de un lado de una oferta (`assets`)."""
    return [a for a in (side or {}).get("assets") or [] if a.get("kind") == "card"]


def _types(side: dict) -> list[str]:
    """Las cartas pedidas por referencia (`types`), p.ej. `card:RET-09`."""
    return [t for t in (side or {}).get("types") or [] if isinstance(t, str)]


def _ref_of_type(t: str) -> Optional[str]:
    return t.split(":", 1)[1] if t.startswith("card:") else None


def _cash(side: dict) -> int:
    return int((side or {}).get("cash") or 0)


# ------------------------------------------------------------- observaciones

@dataclass(frozen=True)
class Settled:
    """Una carta que cambió de manos por efectivo. Evidencia dura."""
    tick: int
    ref: str
    rarity: str
    price: int
    venue: Optional[str]
    fee: int
    seller: Optional[str]
    buyer: Optional[str]


@dataclass(frozen=True)
class Quote:
    """Un precio *pedido*, no pagado. `side` es lo que hace el emisor."""
    tick: int
    ref: str
    rarity: Optional[str]
    price: int
    maker: str
    side: str            # "ask" (vende) | "bid" (compra)
    venue: Optional[str]
    is_dealer: bool
    final: bool = False
    thread: Optional[int] = None


# --------------------------------------------------------------- extracción

def read_settlements(events: Iterable[dict]) -> Iterator[Settled]:
    """Liquidaciones de **una** carta por efectivo.

    Los lotes se omiten a propósito: con varias cartas el precio no se
    puede atribuir a una sola sin suponer el reparto.
    """
    for e in events:
        if e.get("type") != "settlement":
            continue
        p = e.get("payload") or {}
        if p.get("kind") != "trade" or not p.get("price"):
            continue
        items = [i for i in p.get("items") or [] if i.get("kind") == "card"]
        if len(items) != 1:
            continue
        c = items[0]
        parties = p.get("parties") or []
        seller, buyer = c.get("frm"), c.get("to")
        if seller is None and len(parties) == 2:
            seller, buyer = parties
        yield Settled(
            tick=int(p.get("tick") or e.get("tick") or 0),
            ref=c["ref"],
            rarity=c.get("rarity") or "?",
            price=int(p["price"]),
            venue=p.get("venue"),
            fee=int(p.get("fee") or 0),
            seller=seller,
            buyer=buyer,
        )


def _quote_from_offer(tick: int, offer: dict, *, venue: Optional[str]) -> Optional[Quote]:
    """Una oferta estructurada → una cotización de una sola carta, o nada."""
    if not offer:
        return None
    maker = offer.get("maker")
    if not isinstance(maker, str):
        return None
    give, want = offer.get("give") or {}, offer.get("want") or {}
    gc, wc = _cards(give), _cards(want)
    gt, wt = _types(give), _types(want)

    ref = rarity = None
    side = None
    price = 0
    # vende una carta y pide efectivo
    if _cash(want) and not wc and not wt:
        if len(gc) == 1 and not gt:
            ref, rarity, side, price = gc[0]["ref"], gc[0].get("rarity"), "ask", _cash(want)
        elif len(gt) == 1 and not gc:
            ref, side, price = _ref_of_type(gt[0]), "ask", _cash(want)
    # ofrece efectivo y pide una carta
    elif _cash(give) and not gc and not gt:
        if len(wc) == 1 and not wt:
            ref, rarity, side, price = wc[0]["ref"], wc[0].get("rarity"), "bid", _cash(give)
        elif len(wt) == 1 and not wc:
            ref, side, price = _ref_of_type(wt[0]), "bid", _cash(give)

    if not ref or not side or price <= 0:
        return None
    return Quote(
        tick=tick, ref=ref, rarity=rarity, price=price, maker=maker, side=side,
        venue=venue if venue is not None else offer.get("venue"),
        is_dealer=not is_team(maker),
        final=bool(offer.get("final")),
        thread=offer.get("thread"),
    )


def read_quotes(events: Iterable[dict]) -> Iterator[Quote]:
    """Cotizaciones del tablón y de las conversaciones con dealers."""
    for e in events:
        t = e.get("type")
        p = e.get("payload") or {}
        tick = int(e.get("tick") or 0)
        if t == "offer.listed":
            q = _quote_from_offer(tick, p.get("offer") or {}, venue=p.get("venue"))
            if q:
                yield q
        elif t == "thread.message":
            q = _quote_from_offer(tick, p.get("offer") or {}, venue=None)
            if q:
                yield q


# ----------------------------------------------------------- lo que negocia

@dataclass
class DealerLine:
    """Lo que sabemos de un dealer sobre una carta, en un sentido."""
    dealer: str
    ref: str
    side: str                       # "ask": nos la vende | "bid": nos la compra
    prices: list[int] = field(default_factory=list)
    finals: list[int] = field(default_factory=list)
    settled: list[int] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.prices)

    @property
    def floor(self) -> Optional[int]:
        """El precio más favorable para nosotros visto hasta ahora.

        Comprando, el mínimo que ha pedido; vendiendo, el máximo que ha
        ofrecido. Una liquidación pesa más que una cotización, y un
        `final: true` es la última palabra declarada del dealer.
        """
        pool = self.settled or self.finals or self.prices
        if not pool:
            return None
        return min(pool) if self.side == "ask" else max(pool)

    @property
    def opening(self) -> Optional[int]:
        """Su precio de apertura: el peor para nosotros."""
        if not self.prices:
            return None
        return max(self.prices) if self.side == "ask" else min(self.prices)

    @property
    def steps(self) -> list[int]:
        """Tamaños de concesión observados, de apertura a suelo."""
        ladder = sorted(set(self.prices), reverse=(self.side == "ask"))
        return [abs(b - a) for a, b in zip(ladder, ladder[1:])]

    @property
    def confidence(self) -> str:
        if self.settled:
            return "SETTLED"
        if self.finals:
            return "FINAL"
        if self.n >= 4:
            return "HIGH"
        return "MEDIUM" if self.n >= 2 else "LOW"


@dataclass
class CardMarket:
    """El mercado entre equipos para una referencia."""
    ref: str
    rarity: Optional[str] = None
    settled: list[int] = field(default_factory=list)
    asks: list[int] = field(default_factory=list)
    bids: list[int] = field(default_factory=list)
    holders: set[str] = field(default_factory=set)
    seekers: set[str] = field(default_factory=set)

    @property
    def fair(self) -> Optional[float]:
        """Referencia de precio. Las liquidaciones manda sobre lo pedido."""
        if self.settled:
            return statistics.median(self.settled)
        if self.asks and self.bids:
            return (statistics.median(self.asks) + statistics.median(self.bids)) / 2
        if self.asks:
            return statistics.median(self.asks)
        if self.bids:
            return statistics.median(self.bids)
        return None

    @property
    def confidence(self) -> str:
        if len(self.settled) >= 3:
            return "HIGH"
        if self.settled:
            return "MEDIUM"
        if self.asks or self.bids:
            return "LOW"
        return "UNKNOWN"

    def undercut(self, step: int = 1) -> Optional[int]:
        """Precio para ser el más barato del tablón, sin regalar la carta."""
        if not self.asks:
            return int(self.fair) if self.fair else None
        return max(1, min(self.asks) - step)


class Oracle:
    """Agrega eventos del feed en conocimiento accionable.

    Es acumulativo e idempotente por `id` de evento: alimentarlo dos veces
    con el mismo lote no altera el resultado.
    """

    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.dealers: dict[tuple[str, str, str], DealerLine] = {}
        self.cards: dict[str, CardMarket] = {}
        self.ticks: set[int] = set()

    # -- ingesta ----------------------------------------------------------
    def ingest(self, events: Iterable[dict]) -> int:
        """Añade los eventos no vistos. Devuelve cuántos eran nuevos."""
        fresh = []
        for e in events:
            eid = e.get("id")
            if eid is None or eid in self.seen:
                continue
            self.seen.add(eid)
            fresh.append(e)
            if e.get("tick"):
                self.ticks.add(int(e["tick"]))
        # En orden cronológico, y cada evento entero antes del siguiente: una
        # publicación vieja no debe resucitar a un poseedor que ya vendió.
        for e in sorted(fresh, key=lambda x: (x.get("tick") or 0, x.get("id") or 0)):
            one = (e,)
            for s in read_settlements(one):
                self._add_settled(s)
            for q in read_quotes(one):
                self._add_quote(q)
        return len(fresh)

    def _card(self, ref: str, rarity: Optional[str] = None) -> CardMarket:
        cm = self.cards.get(ref)
        if cm is None:
            cm = self.cards[ref] = CardMarket(ref=ref)
        if rarity and not cm.rarity:
            cm.rarity = rarity
        return cm

    def _add_settled(self, s: Settled) -> None:
        cm = self._card(s.ref, s.rarity)
        if is_team(s.seller) and is_team(s.buyer):
            cm.settled.append(s.price)
            cm.holders.discard(s.seller)
            cm.holders.add(s.buyer)
            return
        # una de las partes es un dealer: es evidencia de su suelo real
        dealer = s.seller if not is_team(s.seller) else s.buyer
        if not isinstance(dealer, str):
            return
        self._card(s.ref, s.rarity)          # la rareza sirve para generalizar
        side = "ask" if dealer == s.seller else "bid"
        self._line(dealer, s.ref, side).settled.append(s.price)

    def _line(self, dealer: str, ref: str, side: str) -> DealerLine:
        k = (dealer, ref, side)
        dl = self.dealers.get(k)
        if dl is None:
            dl = self.dealers[k] = DealerLine(dealer=dealer, ref=ref, side=side)
        return dl

    def _add_quote(self, q: Quote) -> None:
        if q.is_dealer:
            dl = self._line(q.maker, q.ref, q.side)
            dl.prices.append(q.price)
            if q.final:
                dl.finals.append(q.price)
            return
        cm = self._card(q.ref, q.rarity)
        if q.side == "ask":
            cm.asks.append(q.price)
            cm.holders.add(q.maker)
        else:
            cm.bids.append(q.price)
            cm.seekers.add(q.maker)

    # -- consultas --------------------------------------------------------
    def dealer_floor(self, dealer: str, ref: str, side: str = "ask") -> Optional[DealerLine]:
        return self.dealers.get((dealer, ref, side))

    def rarity_floor(self, rarity: str, side: str = "ask") -> Optional[float]:
        """Suelo típico de una rareza, para una carta nunca vista.

        Las tiradas son fijas por rareza, y los dealers usan las mismas
        reglas para todos los equipos, así que una rareza generaliza mejor
        que una referencia suelta.
        """
        vals = [
            dl.floor
            for dl in self.dealers.values()
            if dl.side == side and dl.floor is not None
            and (self.cards.get(dl.ref) or CardMarket(dl.ref)).rarity == rarity
        ]
        return statistics.median(vals) if vals else None

    def opening_to_avoid(self, dealer: str, ref: str) -> Optional[int]:
        """Su precio de apertura: aceptarlo no puntúa en la escalera."""
        dl = self.dealer_floor(dealer, ref, "ask")
        return dl.opening if dl else None

    def suggest_counter(self, dealer: str, ref: str, *, their_price: int,
                        side: str = "ask") -> Optional[int]:
        """Contraoferta defendible: parte del suelo conocido, no de su precio.

        Comprando, abre un paso *por debajo* del suelo observado para
        dejarse margen de concesión sin cruzarlo. Sin suelo conocido, cae a
        la mediana de la rareza; sin eso, no opina.
        """
        dl = self.dealer_floor(dealer, ref, side)
        floor = dl.floor if dl else None
        if floor is None:
            cm = self.cards.get(ref)
            if cm and cm.rarity:
                f = self.rarity_floor(cm.rarity, side)
                floor = int(f) if f else None
        if floor is None:
            return None
        if side == "ask":
            return max(1, min(their_price - 1, floor - max(1, round(floor * 0.08))))
        return max(their_price + 1, floor + max(1, round(floor * 0.08)))

    def arbitrage(self, *, margin: int = 1) -> list[dict]:
        """Cartas más baratas entre equipos que al dealer más barato.

        Comprar a un equipo por debajo del suelo del dealer es excedente sin
        negociar. No considera comisiones: las pone quien ejecuta.
        """
        out = []
        best: dict[str, tuple[str, int]] = {}
        for (dealer, ref, side), dl in self.dealers.items():
            if side != "ask" or dl.floor is None:
                continue
            if ref not in best or dl.floor < best[ref][1]:
                best[ref] = (dealer, dl.floor)
        for ref, (dealer, floor) in best.items():
            cm = self.cards.get(ref)
            if not cm or not cm.asks:
                continue
            cheapest = min(cm.asks)
            if floor - cheapest >= margin:
                out.append({
                    "ref": ref, "rarity": cm.rarity, "team_ask": cheapest,
                    "dealer": dealer, "dealer_floor": floor,
                    "saving": floor - cheapest, "holders": sorted(cm.holders),
                })
        return sorted(out, key=lambda r: -r["saving"])

    def who_has(self, ref: str) -> list[str]:
        """Equipos con evidencia publicada de tener la carta."""
        cm = self.cards.get(ref)
        return sorted(cm.holders) if cm else []

    def who_wants(self, ref: str) -> list[str]:
        """Equipos con evidencia publicada de buscarla: a quién vender."""
        cm = self.cards.get(ref)
        return sorted(cm.seekers) if cm else []

    @property
    def coverage(self) -> dict:
        return {
            "events": len(self.seen),
            "ticks": len(self.ticks),
            "tick_min": min(self.ticks) if self.ticks else None,
            "tick_max": max(self.ticks) if self.ticks else None,
            "dealer_lines": len(self.dealers),
            "cards": len(self.cards),
            "settled": sum(len(c.settled) for c in self.cards.values()),
        }
