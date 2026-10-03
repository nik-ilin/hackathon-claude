"""Señal de los eventos del feed que el oráculo ignora.

Add-on independiente y de lógica pura: no importa ningún módulo del repo, no
abre la red y no escribe nada. Sólo interpreta eventos ya recogidos.

`feed_oracle.py` consume tres tipos de evento (`settlement`, `offer.listed`,
`thread.message`). Sobre 1.011 eventos reales (ticks 192-254) el feed trae
otros doce, y ahí está lo que falta para decidir **a qué precio** publicar:

- `offer.cancelled`: retirada observada, con motivo desconocido. No demuestra
  rechazo del precio; se excluye del denominador de tasas de venta.
- `thread.closed.reason`: por qué fracasan las conversaciones con cada dealer.
- `pack.opened` + la liquidación del sobre: si comprar sobres sale a cuenta.
- `venue.fee_announced` / `fee_changed` / `opened`: la comisión vigente y la
  anunciada que aún no aplica. Decidir con la comisión vieja es un error.
- `duel.closed`: cuántos duelos acaban en `no_deal`.
- `bench.started`: cuándo corrió el banco sintético y sobre qué venues.

Dos reglas que atraviesan todo el módulo:

1. **OBSERVADO frente a INFERIDO está en la API, no en los comentarios.** Cada
   resultado lleva `basis`. Un `offer.cancelled` es observado; que una oferta
   concreta se haya liquidado se *infiere* cruzando el id del activo, porque
   el feed no publica qué oferta cerró cada liquidación.
2. **Una oferta puede desaparecer del feed sin evento alguno.** Eso no es un
   rechazo: es `UNKNOWN`. Nunca entra en el denominador de una tasa de venta.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

# --------------------------------------------------------------- vocabulario

#: Lo vio el feed tal cual.
OBSERVED = "observado"
#: Se deduce cruzando dos eventos que el feed no relaciona entre sí.
INFERRED = "inferido"

#: Desenlace de una oferta publicada.
SETTLED = "settled"        # se liquidó (inferido por id de activo)
CANCELLED = "cancelled"    # retirada explícita; no prueba rechazo del precio
OPEN = "open"              # su `expires_tick` es posterior a lo observado
UNKNOWN = "unknown"        # desapareció sin evento: no se sabe, no es rechazo

#: Por debajo de esto una cifra es una anécdota, no una estimación.
MIN_SAMPLE = 3


def is_team(actor: object) -> bool:
    """`t04` sí; `abuela`, `v05`, `None` no. Copia local a propósito: este
    add-on no debe romperse si otro módulo cambia su versión."""
    return (isinstance(actor, str) and len(actor) == 3
            and actor[0] == "t" and actor[1:].isdigit())


def _cards(side: dict) -> list[dict]:
    return [a for a in (side or {}).get("assets") or [] if a.get("kind") == "card"]


def _refs_wanted(side: dict) -> list[str]:
    """Cartas pedidas por referencia: `card:RET-09` → `RET-09`."""
    out = []
    for t in (side or {}).get("types") or []:
        if isinstance(t, str) and t.startswith("card:"):
            out.append(t.split(":", 1)[1])
    return out


def _cash(side: dict) -> int:
    return int((side or {}).get("cash") or 0)


def _median(xs: list[int]) -> Optional[float]:
    return statistics.median(xs) if xs else None


# ------------------------------------------------------- ciclo de una oferta

@dataclass
class OfferLife:
    """Una oferta publicada y lo que se sabe de su final.

    `side`: `ask` vende una carta concreta por efectivo, `bid` ofrece efectivo
    por una referencia. Los lotes y los trueques quedan fuera: con varias
    cartas el precio no se puede atribuir a una sola.
    """
    offer: int
    side: str
    tick: int
    maker: str
    price: int
    venue: Optional[str]
    ref: Optional[str] = None
    rarity: Optional[str] = None
    asset: Optional[int] = None          # sólo en `ask`: id del activo
    to: Optional[str] = None             # oferta dirigida a un equipo
    expires_tick: Optional[int] = None
    final: bool = False
    # Resueltos al consultar, no al ingerir.
    outcome: str = UNKNOWN
    outcome_basis: str = OBSERVED
    cancelled_tick: Optional[int] = None
    settled_tick: Optional[int] = None
    settled_price: Optional[int] = None

    @property
    def accepted(self) -> bool:
        """El mercado pagó al menos lo pedido (ask) o cerró dentro del
        presupuesto (bid). Liquidar por debajo de lo pedido no valida ese
        precio: la carta se movió, pero el precio pedido no se aceptó."""
        if self.outcome != SETTLED or self.settled_price is None:
            return False
        if self.side == "ask":
            return self.settled_price >= self.price
        return self.settled_price <= self.price

    @property
    def rejected(self) -> bool:
        """Precio no alcanzado en una liquidación inferida; cancelar no demuestra rechazo."""
        return self.outcome == SETTLED and not self.accepted

    @property
    def resolved(self) -> bool:
        """Tiene desenlace utilizable. `OPEN` y `UNKNOWN` no lo tienen."""
        return self.accepted or self.rejected


@dataclass
class PriceLevel:
    """Cuántas ofertas a este precio se aceptaron y cuántas se rechazaron."""
    price: int
    accepted: int = 0
    rejected: int = 0
    open_now: int = 0
    cancelled: int = 0
    unknown: int = 0
    assets: set = field(default_factory=set)     # activos distintos probados
    makers: set = field(default_factory=set)

    @property
    def n(self) -> int:
        """Muestra utilizable: sólo lo resuelto."""
        return self.accepted + self.rejected

    @property
    def sell_rate(self) -> Optional[float]:
        """Probabilidad de venta observada, o `None` sin muestra."""
        return self.accepted / self.n if self.n else None

    @property
    def distinct_assets(self) -> int:
        """Un equipo que republica la misma carta cinco veces infla `n`.
        Esto dice cuántas cartas distintas hay realmente detrás."""
        return len(self.assets)

    def as_dict(self) -> dict:
        return {"price": self.price, "accepted": self.accepted,
                "rejected": self.rejected, "n": self.n,
                "sell_rate": self.sell_rate, "open": self.open_now,
                "unknown": self.unknown, "cancelled": self.cancelled,
                "rate_warning": "conditional on inferred settlements; not calibrated fill probability",
                "distinct_assets": self.distinct_assets,
                "makers": len(self.makers), "basis": INFERRED}


@dataclass
class ThreadLife:
    """Una conversación con un dealer u otro equipo, y cómo acabó."""
    thread: int
    tick: int
    kind: Optional[str]
    team: Optional[str]
    counterpart: Optional[str]           # `with`: dealer o equipo
    topic: dict = field(default_factory=dict)
    closed_tick: Optional[int] = None
    reason: Optional[str] = None


@dataclass
class FailureProfile:
    """Motivos de cierre por contraparte. `reasons` es observado; el resto se
    infiere de la ausencia de evento, que no es lo mismo que un éxito."""
    counterpart: str
    opened: int = 0
    closed: int = 0
    reasons: Counter = field(default_factory=Counter)
    open_without_close: int = 0          # inferido: sin `thread.closed`
    settled_after_open: int = 0          # inferido: hubo liquidación después

    @property
    def top_reason(self) -> Optional[tuple[str, int]]:
        return self.reasons.most_common(1)[0] if self.reasons else None

    def as_dict(self) -> dict:
        return {"counterpart": self.counterpart, "opened": self.opened,
                "closed_observed": self.closed,
                "reasons": dict(self.reasons),
                "top_reason": self.top_reason,
                "open_without_close_inferred": self.open_without_close,
                "settled_after_open_inferred": self.settled_after_open,
                "basis_reasons": OBSERVED, "basis_rest": INFERRED}


@dataclass
class PackStat:
    """Economía de un tipo de sobre.

    ADVERTENCIA: la muestra es ridícula (unos pocos `pack.opened` y casi
    ninguna compra con precio en el feed). Esto no es una valoración; es el
    recuento de lo que se vio. Y según las reglas, lo que sale de un sobre no
    puntúa por sí mismo (cuenta como *suerte*): sólo vale lo que se negocie
    con ello después.
    """
    pack: str
    opened: int = 0
    with_best: int = 0                   # `best` no nulo: tirada reseñable
    best_rarities: Counter = field(default_factory=Counter)
    prices_paid: list[int] = field(default_factory=list)
    teams: set = field(default_factory=set)

    @property
    def median_price(self) -> Optional[float]:
        return _median(self.prices_paid)

    @property
    def enough_sample(self) -> bool:
        return len(self.prices_paid) >= MIN_SAMPLE and self.opened >= MIN_SAMPLE

    def as_dict(self) -> dict:
        return {"pack": self.pack, "opened": self.opened,
                "with_best": self.with_best,
                "best_rarities": dict(self.best_rarities),
                "prices_paid": sorted(self.prices_paid),
                "median_price": self.median_price,
                "n_prices": len(self.prices_paid),
                "teams": len(self.teams),
                "enough_sample": self.enough_sample,
                "basis": OBSERVED}


@dataclass
class PendingFee:
    """Comisión anunciada que todavía no se aplicó."""
    venue: str
    announced_tick: int
    effective_tick: Optional[int]
    fee_bps: Optional[int]
    fee_per_card: Optional[int]


@dataclass
class VenueFee:
    """Estado de comisiones de un venue: lo vigente y lo anunciado."""
    venue: str
    name: Optional[str] = None
    owner: Optional[str] = None
    fee_bps: Optional[int] = None
    fee_per_card: Optional[int] = None
    as_of_tick: Optional[int] = None
    source: Optional[str] = None         # `venue.opened` | `venue.fee_changed`
    closed_tick: Optional[int] = None    # `venue.closed`: ya no sirve de nada
    pending: list = field(default_factory=list)
    changes: int = 0
    announcements: int = 0        # `venue.announcement`: publicidad del venue
    fee_notices: int = 0          # `venue.fee_announced`: avisos de cambio
    # Comisión realmente cobrada, de `settlement.fee`. Es la única que importa.
    charged: list = field(default_factory=list)   # (price, fee)

    @property
    def effective_bps(self) -> Optional[float]:
        """Comisión observada en puntos básicos sobre el precio liquidado.
        Puede no cuadrar con `fee_bps` si hay cargo por carta o redondeo."""
        tot_p = sum(p for p, _ in self.charged)
        tot_f = sum(f for _, f in self.charged)
        return 10000.0 * tot_f / tot_p if tot_p else None

    def pending_at(self, tick: int) -> list:
        """Anuncios que ya deberían aplicar en `tick` y no se confirmaron."""
        return [p for p in self.pending
                if p.effective_tick is not None and p.effective_tick <= tick]

    def as_dict(self) -> dict:
        return {"venue": self.venue, "name": self.name, "owner": self.owner,
                "closed_tick": self.closed_tick, "fee_bps": self.fee_bps, "fee_per_card": self.fee_per_card,
                "as_of_tick": self.as_of_tick, "source": self.source,
                "pending": [(p.fee_bps, p.effective_tick) for p in self.pending],
                "changes": self.changes, "announcements": self.announcements,
                "fee_notices": self.fee_notices,
                "charged_n": len(self.charged),
                "effective_bps_observed": self.effective_bps,
                "basis": OBSERVED}


@dataclass
class DuelStat:
    """Duelos cerrados de una sesión. Un duelo que nadie contesta da cero a
    las dos partes, así que `no_deal` es coste, no empate."""
    session: int
    name: Optional[str] = None
    finished: bool = False
    statuses: Counter = field(default_factory=Counter)
    items: Counter = field(default_factory=Counter)

    @property
    def closed(self) -> int:
        return sum(self.statuses.values())

    @property
    def no_deal(self) -> int:
        return self.statuses.get("no_deal", 0)

    @property
    def no_deal_rate(self) -> Optional[float]:
        return self.no_deal / self.closed if self.closed else None

    def as_dict(self) -> dict:
        return {"session": self.session, "name": self.name,
                "finished": self.finished, "closed": self.closed,
                "no_deal": self.no_deal, "no_deal_rate": self.no_deal_rate,
                "statuses": dict(self.statuses),
                "enough_sample": self.closed >= MIN_SAMPLE,
                "basis": OBSERVED}


@dataclass
class BenchRun:
    """Una tanda del banco sintético: ventana, venues y si movió algo."""
    session: int
    name: Optional[str] = None
    start_tick: Optional[int] = None
    ticks: Optional[int] = None
    venues: list = field(default_factory=list)
    settled_by_venue: Counter = field(default_factory=Counter)   # inferido

    @property
    def end_tick(self) -> Optional[int]:
        if self.start_tick is None or self.ticks is None:
            return None
        return self.start_tick + self.ticks

    @property
    def active_venues(self) -> list:
        return sorted(self.settled_by_venue)

    @property
    def silent_venues(self) -> list:
        return [v for v in self.venues if v not in self.settled_by_venue]

    def as_dict(self) -> dict:
        return {"session": self.session, "name": self.name,
                "window": (self.start_tick, self.end_tick),
                "venues": len(self.venues),
                "active_venues": self.active_venues,
                "silent_venues": len(self.silent_venues),
                "settlements_in_window": sum(self.settled_by_venue.values()),
                "basis_window": OBSERVED, "basis_activity": INFERRED}


# ------------------------------------------------------------------- señales

class Signals:
    """Acumula eventos del feed y responde sobre los tipos que el oráculo no
    mira. Idempotente por `id` de evento: alimentarlo dos veces con el mismo
    lote no cambia nada.
    """

    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.kinds: Counter = Counter()
        self.ticks: set[int] = set()
        self._offers: dict[int, OfferLife] = {}
        self.cancels: dict[int, int] = {}          # offer id → tick
        self.threads: dict[int, ThreadLife] = {}
        self.packs: dict[str, PackStat] = {}
        self.venues: dict[str, VenueFee] = {}
        self.duels: dict[int, DuelStat] = {}
        self.benches: dict[int, BenchRun] = {}
        #: Liquidaciones de una carta: lo que permite cerrar el ciclo de una
        #: oferta. (tick, ref, asset, price, fee, venue, frm, to)
        self.settlements: list[tuple] = []
        self.pack_sales: list[tuple] = []          # (tick, pack, team, price)
        self._dirty = True

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
        # En orden cronológico: un anuncio viejo no debe pisar una comisión ya
        # aplicada, y un cierre no puede llegar antes de su apertura.
        for e in sorted(fresh, key=lambda x: (x.get("tick") or 0, x.get("id") or 0)):
            if e.get("tick"):
                self.ticks.add(int(e["tick"]))
            self.kinds[e.get("type")] += 1
            self._route(e)
        if fresh:
            self._dirty = True
        return len(fresh)

    def _route(self, e: dict) -> None:
        t, p = e.get("type"), e.get("payload") or {}
        tick = int(e.get("tick") or 0)
        if t == "offer.listed":
            self._add_offer(tick, p.get("offer") or {}, p.get("venue"))
        elif t == "offer.cancelled":
            oid = p.get("offer")
            if isinstance(oid, int):
                self.cancels.setdefault(oid, tick)
        elif t == "settlement":
            self._add_settlement(tick, p)
        elif t == "thread.opened":
            self._add_thread(tick, p)
        elif t == "thread.closed":
            self._close_thread(tick, p)
        elif t == "pack.opened":
            self._add_pack(p)
        elif t in ("venue.opened", "venue.fee_announced", "venue.fee_changed",
                   "venue.announcement", "venue.closed"):
            self._venue_event(t, tick, p)
        elif t == "duel.closed":
            self._add_duel(p)
        elif t == "duels.finished":
            d = self._duel(p.get("session"))
            if d:
                d.finished, d.name = True, p.get("name") or d.name
        elif t == "bench.started":
            self._add_bench(p)

    def _add_offer(self, tick: int, offer: dict, venue: Optional[str]) -> None:
        oid, maker = offer.get("id"), offer.get("maker")
        if not isinstance(oid, int) or not isinstance(maker, str):
            return
        give, want = offer.get("give") or {}, offer.get("want") or {}
        gc, wc = _cards(give), _cards(want)
        gcash, wcash = _cash(give), _cash(want)
        wrefs = _refs_wanted(want)
        common = dict(offer=oid, tick=tick, maker=maker,
                      venue=offer.get("venue") or venue, to=offer.get("to"),
                      expires_tick=offer.get("expires_tick"),
                      final=bool(offer.get("final")))
        if len(gc) == 1 and not gcash and not wc and not wrefs and wcash > 0:
            # Vende una carta concreta por efectivo: el caso limpio.
            c = gc[0]
            self._offers[oid] = OfferLife(
                side="ask", price=wcash, ref=c.get("ref"),
                rarity=c.get("rarity"), asset=c.get("id"), **common)
        elif gcash > 0 and not gc and len(wrefs) == 1 and not wc:
            # Pide una referencia pagando efectivo: puja de compra.
            self._offers[oid] = OfferLife(
                side="bid", price=gcash, ref=wrefs[0], **common)
        # Lo demás (lotes, trueques, multi-carta) se omite: el precio no se
        # puede atribuir a una sola carta sin suponer el reparto.

    def _add_settlement(self, tick: int, p: dict) -> None:
        items = p.get("items") or []
        price = int(p.get("price") or 0)
        when = int(p.get("tick") or tick)
        packs = [i for i in items if i.get("kind") == "pack"]
        if packs and price:
            # Compra de sobre: la única forma de saber lo que cuesta.
            team = next((x for x in (p.get("parties") or []) if is_team(x)), None)
            for i in packs:
                self.pack_sales.append((when, i.get("ref") or i.get("name"),
                                        team, price))
        cards = [i for i in items if i.get("kind") == "card"]
        if len(cards) != 1 or not price:
            return
        c = cards[0]
        parties = p.get("parties") or []
        frm, to = c.get("frm"), c.get("to")
        if frm is None and len(parties) == 2:
            frm, to = parties
        self.settlements.append((when, c.get("ref"), c.get("id"), price,
                                 int(p.get("fee") or 0), p.get("venue"), frm, to))
        v = p.get("venue")
        if v:
            self._venue(v).charged.append((price, int(p.get("fee") or 0)))

    def _add_thread(self, tick: int, p: dict) -> None:
        tid = p.get("thread")
        if not isinstance(tid, int) or tid in self.threads:
            return
        self.threads[tid] = ThreadLife(
            thread=tid, tick=tick, kind=p.get("kind"), team=p.get("team"),
            counterpart=p.get("with"), topic=p.get("topic") or {})

    def _close_thread(self, tick: int, p: dict) -> None:
        tid = p.get("thread")
        if not isinstance(tid, int):
            return
        th = self.threads.get(tid)
        if th is None:
            # El cierre puede llegar sin haber visto la apertura: el feed sólo
            # guarda 500 eventos. Se registra igual, con lo que se sabe.
            th = self.threads[tid] = ThreadLife(
                thread=tid, tick=tick, kind=p.get("kind"), team=p.get("team"),
                counterpart=p.get("with"))
        th.closed_tick, th.reason = tick, p.get("reason")

    def _add_pack(self, p: dict) -> None:
        name = p.get("pack")
        if not name:
            return
        st = self.packs.setdefault(name, PackStat(pack=name))
        st.opened += 1
        if p.get("team"):
            st.teams.add(p["team"])
        best = p.get("best")
        if best:
            st.with_best += 1
            st.best_rarities[best.get("rarity") or "?"] += 1

    def _venue(self, vid: str) -> VenueFee:
        v = self.venues.get(vid)
        if v is None:
            v = self.venues[vid] = VenueFee(venue=vid)
        return v

    def _venue_event(self, t: str, tick: int, p: dict) -> None:
        vid = p.get("venue")
        if not isinstance(vid, str):
            return
        v = self._venue(vid)
        v.name = p.get("name") or v.name
        if t == "venue.announcement":
            v.announcements += 1
            return
        if t == "venue.closed":
            # Publicar ahí es tirar la oferta: el venue ya no existe.
            v.closed_tick = tick
            return
        if t == "venue.opened":
            v.owner, v.closed_tick = p.get("owner") or v.owner, None
            v.fee_bps, v.fee_per_card = p.get("fee_bps"), p.get("fee_per_card")
            v.as_of_tick, v.source = tick, t
        elif t == "venue.fee_announced":
            v.fee_notices += 1
            v.pending.append(PendingFee(
                venue=vid, announced_tick=tick,
                effective_tick=p.get("effective_tick"),
                fee_bps=p.get("fee_bps"), fee_per_card=p.get("fee_per_card")))
        elif t == "venue.fee_changed":
            v.changes += 1
            v.fee_bps, v.fee_per_card = p.get("fee_bps"), p.get("fee_per_card")
            v.as_of_tick, v.source = tick, t
            # El anuncio que acaba de aplicarse deja de estar pendiente.
            v.pending = [q for q in v.pending
                         if not (q.fee_bps == p.get("fee_bps")
                                 and (q.effective_tick or 0) <= tick)]

    def _duel(self, session: object) -> Optional[DuelStat]:
        if not isinstance(session, int):
            return None
        return self.duels.setdefault(session, DuelStat(session=session))

    def _add_duel(self, p: dict) -> None:
        d = self._duel(p.get("session"))
        if d is None:
            return
        d.statuses[p.get("status") or "?"] += 1
        if p.get("item"):
            d.items[p["item"]] += 1

    def _add_bench(self, p: dict) -> None:
        s = p.get("session")
        if not isinstance(s, int):
            return
        self.benches[s] = BenchRun(
            session=s, name=p.get("name"), start_tick=p.get("start_tick"),
            ticks=p.get("ticks"),
            venues=[v for v in (p.get("venues") or []) if isinstance(v, str)])

    # -- resolución del ciclo de las ofertas ------------------------------
    @property
    def offers(self) -> dict[int, OfferLife]:
        """Las ofertas seguidas, con su desenlace ya resuelto.

        Se resuelve al leer y no al ingerir, porque un evento posterior puede
        convertir un `OPEN` en `SETTLED` y nadie debería tener que acordarse de
        recalcular.
        """
        self._resolve()
        return self._offers

    @property
    def last_tick(self) -> int:
        return max(self.ticks) if self.ticks else 0

    def _resolve(self) -> None:
        """Asigna desenlace a cada oferta. Se recalcula entero porque un
        evento nuevo puede convertir un `OPEN` en `SETTLED`."""
        if not self._dirty:
            return
        by_asset: dict[int, list[tuple]] = defaultdict(list)
        by_ref: dict[str, list[tuple]] = defaultdict(list)
        for s in self.settlements:
            if s[2] is not None:
                by_asset[s[2]].append(s)
            if s[1]:
                by_ref[s[1]].append(s)
        last = self.last_tick
        for o in self._offers.values():
            o.outcome, o.outcome_basis = UNKNOWN, OBSERVED
            o.cancelled_tick = self.cancels.get(o.offer)
            o.settled_tick = o.settled_price = None
            hit = (self._match_ask(o, by_asset) if o.side == "ask"
                   else self._match_bid(o, by_ref))
            if hit is not None:
                # El feed no dice qué oferta cerró cada liquidación: el enlace
                # es inferido por id de activo (ask) o referencia (bid).
                o.outcome, o.outcome_basis = SETTLED, INFERRED
                o.settled_tick, o.settled_price = hit[0], hit[3]
            elif o.cancelled_tick is not None:
                o.outcome, o.outcome_basis = CANCELLED, OBSERVED
            elif o.expires_tick is not None and o.expires_tick > last:
                o.outcome, o.outcome_basis = OPEN, OBSERVED
            # Si no: desapareció sin evento. `UNKNOWN`, nunca rechazo.
        self._dirty = False

    def _window(self, o: OfferLife) -> tuple[int, int]:
        end = o.expires_tick if o.expires_tick is not None else self.last_tick
        return o.tick, max(end, o.tick)

    def _match_ask(self, o: OfferLife, by_asset) -> Optional[tuple]:
        """El activo concreto cambió de manos saliendo del emisor, mientras la
        oferta estaba viva. El id de activo es único, así que el enlace es
        firme salvo que el emisor lo vendiera por otra vía el mismo rato."""
        lo, hi = self._window(o)
        for s in by_asset.get(o.asset or -1, ()):
            if lo <= s[0] <= hi and s[6] == o.maker:
                return s
        return None

    def _match_bid(self, o: OfferLife, by_ref) -> Optional[tuple]:
        """Enlace más débil: el emisor compró *esa referencia* dentro de su
        presupuesto y de la ventana. Pudo comprarla a un dealer por otra vía,
        así que la elasticidad de compra se expone aparte y como inferida."""
        lo, hi = self._window(o)
        for s in by_ref.get(o.ref or "", ()):
            if lo <= s[0] <= hi and s[7] == o.maker and s[3] <= o.price:
                return s
        return None

    def lifecycle(self, side: str = "ask") -> list[OfferLife]:
        """Las ofertas de un lado, con su desenlace ya resuelto."""
        self._resolve()
        return [o for o in self._offers.values() if o.side == side]

    def outcome_counts(self, side: Optional[str] = None) -> dict:
        """Reparto de desenlaces. `unknown` mide cuánto no se puede afirmar."""
        self._resolve()
        c: Counter = Counter()
        for o in self._offers.values():
            if side is None or o.side == side:
                c[o.outcome] += 1
        return dict(c)

    # -- 1. elasticidad ---------------------------------------------------
    def elasticity(self, *, ref: Optional[str] = None,
                   rarity: Optional[str] = None,
                   side: str = "ask") -> list[PriceLevel]:
        """Probabilidad de venta por nivel de precio: la curva de demanda.

        Cada `PriceLevel` dice cuántas ofertas a ese precio se aceptaron y
        cuántas se rechazaron, y sólo lo resuelto cuenta en `n`. Una oferta que
        desapareció sin evento va a `unknown` y no toca la tasa: contarla como
        rechazo convertiría la falta de datos en pesimismo.

        Sin muestra devuelve lista vacía, no una curva inventada.
        """
        self._resolve()
        levels: dict[int, PriceLevel] = {}
        for o in self._offers.values():
            if o.side != side:
                continue
            if ref is not None and o.ref != ref:
                continue
            if rarity is not None and o.rarity != rarity:
                continue
            lv = levels.setdefault(o.price, PriceLevel(price=o.price))
            lv.makers.add(o.maker)
            if o.asset is not None:
                lv.assets.add(o.asset)
            if o.accepted:
                lv.accepted += 1
            elif o.rejected:
                lv.rejected += 1
            elif o.outcome == CANCELLED:
                lv.cancelled += 1
            elif o.outcome == OPEN:
                lv.open_now += 1
            else:
                lv.unknown += 1
        return [levels[p] for p in sorted(levels)]

    def sell_odds(self, price: int, *, ref: Optional[str] = None,
                  rarity: Optional[str] = None,
                  side: str = "ask") -> Optional[tuple[float, int]]:
        """Tasa de venta acumulada para quien pide `price` o menos, con su `n`.

        Resume publicaciones con precio <= price. Es una proporción condicionada
        a liquidaciones inferidas, no una probabilidad de ejecución calibrada.
        Las cancelaciones no son rechazos y no entran en el denominador.
        `None` si no hay nada resuelto en ese tramo.
        """
        acc = rej = 0
        for lv in self.elasticity(ref=ref, rarity=rarity, side=side):
            if lv.price <= price:
                acc += lv.accepted
                rej += lv.rejected
        return (acc / (acc + rej), acc + rej) if acc + rej else None

    def price_for_odds(self, target: float, *, ref: Optional[str] = None,
                       rarity: Optional[str] = None,
                       min_n: int = MIN_SAMPLE) -> Optional[int]:
        """El precio más alto cuya tasa de venta observada llega a `target`.

        Exige `min_n` desenlaces: con n=1 el resultado es una anécdota. Sin
        evidencia suficiente devuelve `None` y quien negocia decide a mano.
        """
        best = None
        for lv in self.elasticity(ref=ref, rarity=rarity):
            got = self.sell_odds(lv.price, ref=ref, rarity=rarity)
            if got and got[1] >= min_n and got[0] >= target:
                best = lv.price
        return best

    def rejection_band(self, *, rarity: Optional[str] = None
                       ) -> Optional[dict]:
        """Dónde empiezan los rechazos: precio mínimo y máximo rechazado y
        aceptado, por rareza. Útil cuando no hay bastante para una curva."""
        acc, rej = [], []
        for lv in self.elasticity(rarity=rarity):
            acc += [lv.price] * lv.accepted
            rej += [lv.price] * lv.rejected
        if not acc and not rej:
            return None
        return {"rarity": rarity,
                "accepted_prices": sorted(acc), "rejected_prices": sorted(rej),
                "max_accepted": max(acc) if acc else None,
                "min_rejected": min(rej) if rej else None,
                "median_rejected": _median(rej), "n": len(acc) + len(rej),
                "basis": INFERRED}

    # -- 2. motivos de fracaso -------------------------------------------
    def failures(self) -> dict[str, FailureProfile]:
        """Motivos de cierre de conversación por contraparte.

        `reasons` es lo que dijo el feed. El resto es inferido: una
        conversación sin `thread.closed` puede seguir abierta o haberse caído
        del tramo de feed recogido.
        """
        self._resolve()
        out: dict[str, FailureProfile] = {}
        sett_by_pair: Counter = Counter()
        for tick, _ref, _a, _p, _f, _v, frm, to in self.settlements:
            for a, b in ((frm, to), (to, frm)):
                if is_team(a) and isinstance(b, str) and not is_team(b):
                    sett_by_pair[(a, b)] += 1
        for th in self.threads.values():
            key = th.counterpart or "?"
            fp = out.setdefault(key, FailureProfile(counterpart=key))
            fp.opened += 1
            if th.closed_tick is None:
                fp.open_without_close += 1
            else:
                fp.closed += 1
                fp.reasons[th.reason or "sin_motivo"] += 1
            if sett_by_pair.get((th.team, key)):
                fp.settled_after_open += 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1].opened))

    def failure_reasons(self) -> Counter:
        """Motivos agregados, sólo observados."""
        c: Counter = Counter()
        for fp in self.failures().values():
            c.update(fp.reasons)
        return c

    def teams_hitting(self, reason: str) -> list[str]:
        """Equipos que chocaron con un motivo concreto. `cooloff` o
        `persona_quota` en varios equipos significa límite del dealer, no
        mala suerte de uno."""
        return sorted({th.team for th in self.threads.values()
                       if th.reason == reason and th.team})

    # -- 3. sobres --------------------------------------------------------
    def pack_economics(self) -> dict[str, PackStat]:
        """Qué se vio de los sobres. MUESTRA INSUFICIENTE casi con seguridad:
        el feed publica muy pocos `pack.opened` y casi ninguna compra con
        precio, y `best` llega nulo cuando la tirada no es reseñable. No se
        devuelve ninguna cifra de valor esperado porque no se puede sostener.
        """
        # Se reconstruye desde `pack_sales` en cada llamada: así llamar dos
        # veces no duplica precios.
        for st in self.packs.values():
            st.prices_paid = []
        for tick, name, team, price in self.pack_sales:
            if not name:
                continue
            st = self.packs.setdefault(name, PackStat(pack=name))
            st.prices_paid.append(price)
            if team:
                st.teams.add(team)
        return dict(self.packs)

    def pack_verdict(self, pack: str) -> dict:
        """Veredicto honesto sobre un sobre: con qué `n` y contra qué se
        compara. Si no hay muestra, lo dice en vez de dar un número."""
        st = self.pack_economics().get(pack)
        if st is None:
            return {"pack": pack, "verdict": None, "why": "sin eventos", "n": 0}
        card_prices = [s[3] for s in self.settlements]
        med_card = _median(card_prices)
        why = []
        if not st.prices_paid:
            why.append("no se vio ninguna compra con precio")
        if st.opened < MIN_SAMPLE:
            why.append(f"sólo {st.opened} aperturas")
        if st.with_best == 0 and st.opened:
            why.append("ninguna tirada reseñable (`best` nulo en todas)")
        return {"pack": pack, "opened": st.opened,
                "prices_paid": sorted(st.prices_paid),
                "median_price": st.median_price,
                "median_card_settled": med_card,
                "with_best": st.with_best,
                "verdict": None if not st.enough_sample else (
                    "a favor" if (st.median_price or 0) <= (med_card or 0)
                    else "en contra"),
                "enough_sample": st.enough_sample,
                "why": "; ".join(why) or "muestra suficiente",
                "basis": OBSERVED}

    # -- 4. comisiones ----------------------------------------------------
    def fee_state(self) -> dict[str, VenueFee]:
        """Comisión vigente por venue, con lo anunciado y no aplicado."""
        return dict(sorted(self.venues.items()))

    def fee_blocked(self, tick: Optional[int] = None) -> list[dict]:
        """Venues donde NO se debe decidir con la comisión conocida.

        Un anuncio cuya fecha de efecto ya pasó sin confirmación deja la
        comisión en el aire; también la deja un venue que anuncia una y otra
        vez el mismo cambio, porque el estado real no se puede fijar.
        """
        tick = self.last_tick if tick is None else tick
        out = []
        for v in self.venues.values():
            due = v.pending_at(tick)
            churn = v.fee_notices >= MIN_SAMPLE and v.changes >= 2
            if due or churn:
                out.append({
                    "venue": v.venue, "name": v.name,
                    "fee_bps_known": v.fee_bps, "as_of_tick": v.as_of_tick,
                    "pending_due": [(p.fee_bps, p.effective_tick) for p in due],
                    "announcement_churn": churn,
                    "why": ("anuncio vencido sin confirmar" if due
                            else "anuncia el mismo cambio repetidamente"),
                    "basis": OBSERVED})
        return out

    def cheapest_venues(self, *, include_closed: bool = False) -> list[dict]:
        """Venues ordenados por comisión conocida, marcando los dudosos.
        Prefiere la comisión realmente cobrada cuando hay liquidaciones. Los
        cerrados se omiten: una oferta ahí no la ve nadie."""
        blocked = {b["venue"] for b in self.fee_blocked()}
        out = []
        for v in self.venues.values():
            if v.closed_tick is not None and not include_closed:
                continue
            out.append({"venue": v.venue, "name": v.name,
                        "closed_tick": v.closed_tick,
                        "fee_bps": v.fee_bps, "fee_per_card": v.fee_per_card,
                        "charged_n": len(v.charged),
                        "effective_bps_observed": v.effective_bps,
                        "trust": not (v.venue in blocked or v.fee_bps is None
                                      or v.closed_tick is not None),
                        # Las dos vías son observadas; la cobrada manda.
                        "fee_basis": "cobrada" if v.charged else "declarada",
                        "basis": OBSERVED})
        return sorted(out, key=lambda r: (not r["trust"],
                                          r["fee_bps"] if r["fee_bps"] is not None else 10 ** 6))

    # -- 5. duelos --------------------------------------------------------
    def duel_stats(self) -> dict[int, DuelStat]:
        """`no_deal` frente a trato, por sesión. Observado, sin extrapolar."""
        return dict(sorted(self.duels.items()))

    def no_deal_rate(self) -> Optional[tuple[float, int]]:
        """Tasa global de duelos sin trato, con su `n`. `None` sin duelos."""
        closed = sum(d.closed for d in self.duels.values())
        if not closed:
            return None
        return sum(d.no_deal for d in self.duels.values()) / closed, closed

    # -- 6. banco sintético ----------------------------------------------
    def bench_runs(self) -> dict[int, BenchRun]:
        """Tandas del banco, cruzadas con las liquidaciones de su ventana.

        La actividad es inferida: el feed no marca qué liquidación salió del
        banco, así que se cuenta lo liquidado en esos ticks y en esos venues.
        """
        for b in self.benches.values():
            b.settled_by_venue = Counter()
            lo, hi = b.start_tick, b.end_tick
            if lo is None or hi is None:
                continue
            allowed = set(b.venues)
            for tick, _ref, _a, _p, _f, venue, _frm, _to in self.settlements:
                if lo <= tick <= hi and venue in allowed:
                    b.settled_by_venue[venue] += 1
        return dict(sorted(self.benches.items()))

    # -- cobertura e informe ----------------------------------------------
    @property
    def coverage(self) -> dict:
        """Cuánto se vio, y de qué. `ignored_by_oracle` es la razón de ser de
        este módulo: eventos que el oráculo descarta."""
        oracle_uses = {"settlement", "offer.listed", "thread.message"}
        return {
            "events": len(self.seen),
            "ticks": len(self.ticks),
            "tick_min": min(self.ticks) if self.ticks else None,
            "tick_max": self.last_tick or None,
            "by_type": dict(self.kinds.most_common()),
            "ignored_by_oracle": sum(n for k, n in self.kinds.items()
                                     if k not in oracle_uses),
            "offers_tracked": len(self._offers),
            "cancels": len(self.cancels),
            "threads": len(self.threads),
        }

    def report(self) -> str:
        """Informe de texto. Cada cifra con su `n`; lo que no tiene muestra se
        dice, no se rellena."""
        self._resolve()
        L: list[str] = []
        cov = self.coverage
        L.append(f"Señales del feed · {cov['events']} eventos, ticks "
                 f"{cov['tick_min']}-{cov['tick_max']}, "
                 f"{cov['ignored_by_oracle']} que el oráculo ignora")

        L.append("")
        L.append("PRECIOS (venta, por rareza) — n = liquidaciones inferidas; NO probabilidad calibrada")
        any_curve = False
        for rar in sorted({o.rarity for o in self._offers.values()
                           if o.side == "ask" and o.rarity}):
            for lv in self.elasticity(rarity=rar):
                if lv.n:
                    any_curve = True
                    L.append(f"  {rar:<9} {lv.price:>3} P  "
                             f"{lv.accepted}/{lv.n} vendidas"
                             f" ({lv.sell_rate:.0%})"
                             f"  [intentos sobre {lv.distinct_assets} cartas, "
                             f"open {lv.open_now}, retiradas {lv.cancelled}, desconocidas {lv.unknown}]")
        if not any_curve:
            L.append("  sin desenlaces resueltos: no hay curva")
        L.append(f"  desenlaces ask: {self.outcome_counts('ask')}")

        L.append("")
        L.append("MOTIVOS DE FRACASO por contraparte")
        for fp in self.failures().values():
            L.append(f"  {fp.counterpart:<8} abiertas {fp.opened}, "
                     f"cerradas {fp.closed} {dict(fp.reasons) or '—'}, "
                     f"sin cierre observado {fp.open_without_close}")
        if not self.failure_reasons():
            L.append("  ningún `thread.closed`: motivos desconocidos")

        L.append("")
        L.append("SOBRES (muestra muy pequeña por construcción)")
        for st in self.pack_economics().values():
            L.append(f"  {st.pack:<20} aperturas {st.opened}, "
                     f"con tirada reseñable {st.with_best}, "
                     f"precios vistos {sorted(st.prices_paid) or '—'}")
        if not self.packs:
            L.append("  ningún `pack.opened`")

        L.append("")
        L.append("COMISIONES por venue")
        for v in self.fee_state().values():
            eff = v.effective_bps
            L.append(f"  {v.venue:<7} {'CERRADO ' if v.closed_tick else ''}"
                     f"{str(v.fee_bps):>5} bps"
                     f" +{v.fee_per_card} P/carta (t{v.as_of_tick}, {v.source})"
                     f"  pendientes {[(p.fee_bps, p.effective_tick) for p in v.pending] or '—'}"
                     + (f"  cobrado {eff:.0f} bps n={len(v.charged)}" if eff is not None else ""))
        for b in self.fee_blocked():
            L.append(f"  !! {b['venue']}: {b['why']}")

        L.append("")
        L.append("DUELOS")
        for d in self.duel_stats().values():
            L.append(f"  sesión {d.session} ({d.name or '?'}) cerrados "
                     f"{d.closed}, no_deal {d.no_deal}"
                     + (f" ({d.no_deal_rate:.0%})" if d.no_deal_rate is not None else "")
                     + ("" if d.closed >= MIN_SAMPLE else "  [muestra insuficiente]"))
        if not self.duels:
            L.append("  ningún `duel.closed`")

        L.append("")
        L.append("BANCO SINTÉTICO")
        for b in self.bench_runs().values():
            L.append(f"  sesión {b.session} ticks {b.start_tick}-{b.end_tick}, "
                     f"{len(b.venues)} venues, con operaciones "
                     f"{b.active_venues or '—'}, callados {len(b.silent_venues)}")
        if not self.benches:
            L.append("  ningún `bench.started`")
        return "\n".join(L)


def from_events(events: Iterable[dict]) -> Signals:
    """Atajo: construye y alimenta en un paso."""
    s = Signals()
    s.ingest(events)
    return s
