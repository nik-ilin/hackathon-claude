"""Reconstrucción de las manos rivales a partir del feed público.

Add-on independiente: lee `feed_oracle` (nada más) y no escribe en el juego.
Lógica pura, sin red, biblioteca estándar.

`feed_oracle` responde «cuánto vale una carta». Aquí se responde **a quién**:
qué se le ha visto a cada equipo, qué ha declarado que busca, y cuánto de más
pagaría por cerrar una página. Son dos preguntas distintas y por eso dos
módulos.

La regla que ordena todo el módulo: **el feed sólo muestra lo que se publica**.
Que no se haya visto una carta en manos de un equipo no prueba que no la tenga;
sólo prueba que no la ha publicado. Por eso la API separa tres estados y los
nombra sin disimulo:

- `held_*`  → posesión **observada** (liquidación o publicación propia).
- `sought_*`→ ausencia **declarada** por él mismo (puja, trueque, tema de
  conversación con un dealer). Es la única evidencia de que le falta algo.
- `unseen_*`→ **ni una cosa ni la otra**: ignorancia nuestra, no un hueco suyo.

Ningún campo devuelve una cifra plausible cuando no hay datos: devuelve `None`
o una lista vacía.

Jerarquía de evidencia (constantes `EV_*`):

- `SETTLED`: la carta cambió de manos en un `settlement`. Hecho.
- `PUBLISHED`: el equipo lo publicó (oferta en el tablón o en un hilo con un
  dealer). Es su palabra, pero una oferta estructurada compromete: nadie
  ofrece en firme una carta que no tiene, porque la liquidación fallaría.
- `TOPIC`: el tema con el que abrió una conversación. Intención, no oferta.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from feed_oracle import Oracle, is_team      # sólo lectura: nada se modifica

# --------------------------------------------------------------- constantes

EV_SETTLED = "SETTLED"
EV_PUBLISHED = "PUBLISHED"
EV_TOPIC = "TOPIC"

#: De más fuerte a más débil. Sirve para quedarse con la mejor evidencia.
_EV_RANK = {EV_SETTLED: 3, EV_PUBLISHED: 2, EV_TOPIC: 1}

#: Marginales de copia del catálogo público: la 2ª copia vale el 25% de la 1ª.
COPY_MARGINALS = (1.0, 0.25, 0.1)
#: Bonus por página completa (comunes + infrecuentes + raras de un set).
PAGE_BONUS = 0.25
#: Bonus extra por tener además épica y legendaria.
MASTER_BONUS = 0.1


def _best(a: Optional[str], b: str) -> str:
    """La evidencia más fuerte de las dos."""
    return b if a is None or _EV_RANK[b] > _EV_RANK[a] else a


def _set_of(ref: str) -> str:
    """`RET-09` → `RET`. Los IDs del catálogo son `SET-NN`."""
    return ref.split("-", 1)[0]


def _cards(side: dict) -> list[dict]:
    """Cartas concretas de un lado de una oferta: copias con ID de activo."""
    return [a for a in (side or {}).get("assets") or [] if a.get("kind") == "card"]


def _refs_wanted(side: dict) -> list[str]:
    """Referencias pedidas (`types`), p.ej. `card:RET-09` → `RET-09`."""
    out = []
    for t in (side or {}).get("types") or []:
        if isinstance(t, str) and t.startswith("card:"):
            out.append(t.split(":", 1)[1])
    return out


# ------------------------------------------------------------ observaciones

@dataclass
class Held:
    """Posesión **observada** de una referencia por un equipo.

    `assets` son los IDs de copia distintos que se le han visto: es la única
    prueba dura de duplicado, porque dos copias de la misma carta tienen IDs
    distintos. `released` marca que lo último que hizo con ella fue soltarla.
    """
    ref: str
    first_tick: int
    last_tick: int
    evidence: str
    assets: set[int] = field(default_factory=set)
    gone: set[int] = field(default_factory=set)   # copias que se le vio soltar
    sightings: int = 0
    released: bool = False          # la última vez se la quitó de encima
    acquired: bool = False          # alguna vez se la vio entrar

    @property
    def copies_seen(self) -> int:
        """Copias distintas vistas. Sin ID de activo, se asume una."""
        return max(1, len(self.assets))

    @property
    def copies_left(self) -> int:
        """Copias vistas que no se le vio soltar. Es una cota inferior."""
        return len(self.assets - self.gone)

    @property
    def likely_still_holds(self) -> bool:
        """Probablemente la sigue teniendo.

        «Probablemente» en serio: se siguen las copias una a una por su ID, así
        que vender la única que se le vio la saca, vender una de dos la deja, y
        recomprar después la devuelve (los eventos se procesan en orden de
        tick). Sin IDs de copia sólo queda la última acción observada.
        """
        if self.assets:
            return self.copies_left > 0
        return not self.released


@dataclass
class Sought:
    """Ausencia **declarada**: el propio equipo dijo que la quiere.

    Es la evidencia más valiosa del feed, porque invierte el problema: no hay
    que adivinar su hueco, lo ha publicado él.
    """
    ref: str
    first_tick: int
    last_tick: int
    evidence: str
    times: int = 0
    best_bid: Optional[int] = None      # lo máximo que ha ofrecido en efectivo


@dataclass
class Listing:
    """Una publicación en el tablón, con su estado actual."""
    offer: int
    tick: int
    team: str
    ref: str
    side: str                   # "ask" vende | "bid" compra
    price: Optional[int]
    venue: Optional[str]
    expires_tick: Optional[int] = None
    cancelled: bool = False

    def active(self, at_tick: int) -> bool:
        """Sigue en pie en el tick dado: ni cancelada ni caducada."""
        if self.cancelled:
            return False
        return self.expires_tick is None or self.expires_tick > at_tick


@dataclass
class TeamView:
    """Todo lo observado de un equipo. Vacío = no se ha visto nada de él.

    `sample` es el número de observaciones: con 0, cualquier conclusión sobre
    este equipo es ignorancia disfrazada, y los métodos de `Rivals` lo dicen
    devolviendo confianza `NONE`.
    """
    team: str
    held: dict[str, Held] = field(default_factory=dict)
    sought: dict[str, Sought] = field(default_factory=dict)
    listings: list[Listing] = field(default_factory=list)
    set_events: Counter = field(default_factory=Counter)

    @property
    def sample(self) -> int:
        return sum(h.sightings for h in self.held.values()) + \
            sum(s.times for s in self.sought.values())

    @property
    def held_refs(self) -> list[str]:
        """Referencias que probablemente tiene ahora mismo."""
        return sorted(r for r, h in self.held.items() if h.likely_still_holds)

    @property
    def seen_ever_refs(self) -> list[str]:
        """Referencias que se le han visto alguna vez, aunque ya las soltara."""
        return sorted(self.held)

    @property
    def released_refs(self) -> list[str]:
        """Las que soltó y no se le han vuelto a ver: sabemos que las tuvo."""
        return sorted(r for r, h in self.held.items() if not h.likely_still_holds)

    @property
    def sought_refs(self) -> list[str]:
        return sorted(self.sought)

    @property
    def duplicates_observed(self) -> list[str]:
        """Duplicados con prueba dura: dos IDs de copia que no ha soltado."""
        return sorted(r for r, h in self.held.items() if h.copies_left > 1)

    def duplicates_offered(self, at_tick: Optional[int] = None) -> list[str]:
        """Lo que está ofreciendo: le sobra, o eso cree él.

        Una carta en venta no es necesariamente un duplicado, pero sí es una
        que está dispuesto a soltar, que para negociar es lo mismo.
        """
        tick = self.last_tick if at_tick is None else at_tick
        return sorted({l.ref for l in self.listings
                       if l.side == "ask" and l.active(tick)})

    @property
    def last_tick(self) -> int:
        ticks = [h.last_tick for h in self.held.values()]
        ticks += [s.last_tick for s in self.sought.values()]
        return max(ticks) if ticks else 0

    @property
    def active_sets(self) -> list[tuple[str, int]]:
        """Sets donde concentra actividad, del más al menos movido.

        El set en el que negocia es el que probablemente le toca multiplicador
        alto, pero el multiplicador es privado: esto es una pista de conducta,
        nunca una lectura de sus valores.
        """
        return self.set_events.most_common()


# ------------------------------------------------------------ huecos y presión

@dataclass
class SetGaps:
    """Estado de la página de un set para un equipo, con la duda explícita.

    Las tres listas son disjuntas y suman la página entera:
    `held_refs` (observado) + `sought_refs` (declarado ausente) +
    `unseen_refs` (sin evidencia de nada).
    """
    team: str
    set_id: str
    page_refs: list[str]
    held_refs: list[str]
    sought_refs: list[str]
    unseen_refs: list[str]
    confidence: str             # DECLARED | OBSERVED | WEAK | NONE
    caveat: str

    @property
    def missing_min(self) -> int:
        """Cota inferior de lo que le falta: sólo lo que ha declarado."""
        return len(self.sought_refs)

    @property
    def missing_max(self) -> int:
        """Cota superior: todo lo que no se le ha visto."""
        return len(self.sought_refs) + len(self.unseen_refs)


@dataclass
class PagePressure:
    """Cuánto de más pagaría un equipo por una carta de esta página.

    Aritmética, con `page_bonus = 0.25` (del catálogo público):

        bonus_book = 0.25 · Σ book de las 10 cartas de la página

    El bonus sólo se cobra cuando la página está completa, así que:

    - `extra_if_this_closes_page = bonus_book` — lo que vale la **última**
      carta de más. Es el techo, y sólo aplica si de verdad le falta una.
    - `extra_amortized = bonus_book / missing_max` — reparto del bonus entre
      todo lo que le puede faltar. Es la cota prudente.

    Qué es observado y qué es supuesto:

    - OBSERVADO: `held_refs` y `sought_refs` de `SetGaps`, y los `book` del
      catálogo público.
    - SUPUESTO 1: que no se le haya visto una carta no significa que le falte.
      De ahí que haya dos cifras y no una.
    - SUPUESTO 2: el bonus se expresa en unidades de `book`. El multiplicador
      de set es privado y distinto para cada equipo (los mismos seis valores
      barajados), así que su valor real es `multiplicador · bonus_book` con
      un multiplicador que no podemos conocer: si este set es su favorito
      pagará más que esto, y si es el peor, menos. Aquí no se simula.
    - NO MODELADO: `copy_marginals` y `master_bonus` no entran en la presión
      de página. El primero describe lo que pierde quien acumula duplicados
      (por eso vende), el segundo la épica y la legendaria, que no son página.
    """
    team: str
    set_id: str
    gaps: SetGaps
    page_book_sum: Optional[int]
    bonus_book: Optional[float]
    extra_if_this_closes_page: Optional[float]
    extra_amortized: Optional[float]
    confidence: str
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ Rivals

class Rivals:
    """Agrega el feed en una vista por equipo. Acumulativo e idempotente.

    Alimentarlo dos veces con el mismo lote no cambia nada: se deduplica por
    `id` de evento, igual que `Oracle`.
    """

    #: Fracción de la página que hay que haber visto para que la ausencia del
    #: resto empiece a significar algo. Por debajo es ruido.
    OBSERVED_FRACTION = 0.4

    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.teams: dict[str, TeamView] = {}
        self.listings: dict[int, Listing] = {}      # por ID de oferta
        self.asset_ref: dict[int, str] = {}         # ID de copia → referencia
        self.pages: dict[str, list[str]] = {}       # set → refs de página
        self.book: dict[str, int] = {}
        self.rarity: dict[str, str] = {}
        self.tick_max: int = 0

    # -- catálogo ---------------------------------------------------------
    def load_catalog(self, catalog: dict) -> int:
        """Página, book y rareza desde `/api/catalog`, que es público.

        La página son las cartas con `page: true` (comunes, infrecuentes y
        raras); épica y legendaria quedan fuera a propósito.
        """
        n = 0
        for st in catalog.get("sets") or []:
            sid = st.get("id")
            page: list[str] = []
            for c in st.get("cards") or []:
                ref = c.get("id")
                if not ref:
                    continue
                if c.get("book") is not None:
                    self.book[ref] = int(c["book"])
                if c.get("rarity"):
                    self.rarity[ref] = c["rarity"]
                if c.get("page"):
                    page.append(ref)
                n += 1
            if sid and page:
                self.pages[sid] = sorted(page)
        return n

    def page_refs(self, set_id: str) -> list[str]:
        """Sin catálogo cargado, lista vacía: no se inventa la página."""
        return list(self.pages.get(set_id, ()))

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
        # En orden de tick: una venta vieja no debe borrar una compra nueva.
        for e in sorted(fresh, key=lambda x: (x.get("tick") or 0, x.get("id") or 0)):
            tick = int(e.get("tick") or 0)
            self.tick_max = max(self.tick_max, tick)
            t = e.get("type")
            p = e.get("payload") or {}
            if t == "settlement":
                self._settlement(tick, p)
            elif t == "offer.listed":
                self._listed(tick, p)
            elif t == "offer.cancelled":
                self._cancelled(p)
            elif t == "thread.message":
                self._thread_offer(tick, p)
            elif t == "thread.opened":
                self._thread_topic(tick, p)
        return len(fresh)

    def view(self, team: str) -> TeamView:
        """La vista de un equipo. Nunca `None`: si no hay nada, está vacía."""
        v = self.teams.get(team)
        return v if v is not None else TeamView(team=team)

    def _view(self, team: str) -> TeamView:
        v = self.teams.get(team)
        if v is None:
            v = self.teams[team] = TeamView(team=team)
        return v

    # -- registro de señales ----------------------------------------------
    def _hold(self, team: str, ref: str, tick: int, evidence: str, *,
              asset: Optional[int] = None, acquired: bool = False,
              released: bool = False) -> None:
        v = self._view(team)
        h = v.held.get(ref)
        if h is None:
            h = v.held[ref] = Held(ref=ref, first_tick=tick, last_tick=tick,
                                   evidence=evidence)
        h.last_tick = max(h.last_tick, tick)
        h.first_tick = min(h.first_tick, tick)
        h.evidence = _best(h.evidence, evidence)
        h.sightings += 1
        if asset is not None:
            h.assets.add(asset)
            self.asset_ref[asset] = ref
            # Las copias se siguen una a una: soltar la #20 no dice nada de la #7.
            if released:
                h.gone.add(asset)
            elif acquired:
                h.gone.discard(asset)
        if acquired:
            h.acquired = True
        # Sólo la última acción manda: vender y recomprar deja `released` en
        # falso, que es lo correcto para decidir a quién pedirle la carta.
        if acquired or released:
            h.released = released
        v.set_events[_set_of(ref)] += 1

    def _want(self, team: str, ref: str, tick: int, evidence: str, *,
              bid: Optional[int] = None) -> None:
        v = self._view(team)
        s = v.sought.get(ref)
        if s is None:
            s = v.sought[ref] = Sought(ref=ref, first_tick=tick, last_tick=tick,
                                       evidence=evidence)
        s.last_tick = max(s.last_tick, tick)
        s.first_tick = min(s.first_tick, tick)
        s.evidence = _best(s.evidence, evidence)
        s.times += 1
        if bid is not None and (s.best_bid is None or bid > s.best_bid):
            s.best_bid = bid
        v.set_events[_set_of(ref)] += 1

    def _settlement(self, tick: int, p: dict) -> None:
        """Una liquidación mueve cartas: evidencia dura en los dos sentidos.

        Se procesan también los lotes y las `match` de venue: aquí no se
        reparte ningún precio entre cartas, sólo se anota quién tiene qué,
        y para eso un lote vale igual que una carta sola.
        """
        if p.get("kind") not in ("trade", "match"):
            return
        single = len([i for i in p.get("items") or [] if i.get("kind") == "card"]) == 1
        price = int(p["price"]) if single and p.get("price") else None
        for i in p.get("items") or []:
            if i.get("kind") != "card" or not i.get("ref"):
                continue
            ref, asset = i["ref"], i.get("id")
            if isinstance(asset, int):
                self.asset_ref[asset] = ref
            frm, to = i.get("frm"), i.get("to")
            if is_team(frm):
                # Vendió: la tuvo (dato duro) y es candidata a sobrarle.
                self._hold(frm, ref, tick, EV_SETTLED, asset=asset, released=True)
            if is_team(to):
                self._hold(to, ref, tick, EV_SETTLED, asset=asset, acquired=True)
                # Compró lo que le faltaba: la petición queda satisfecha, pero
                # se conserva como historia, no se borra: un equipo que pagó
                # por esta carta volverá a pagar por otra del mismo set.
                s = self._view(to).sought.get(ref)
                if s is not None and price is not None:
                    s.best_bid = max(s.best_bid or 0, price)

    def _listed(self, tick: int, p: dict) -> None:
        """Lo que un equipo publica en un venue.

        Tres formas, las tres informativas: vender por efectivo (la tiene),
        pujar con efectivo (le falta) y el trueque carta por carta, que dice
        las dos cosas en la misma oferta.
        """
        o = p.get("offer") or {}
        maker = o.get("maker")
        if not is_team(maker):
            return
        oid = o.get("id")
        venue = o.get("venue") or p.get("venue")
        exp = o.get("expires_tick")
        give, want = o.get("give") or {}, o.get("want") or {}
        gc, wc = _cards(give), _cards(want)
        gt, wt = _refs_wanted(give), _refs_wanted(want)
        gcash, wcash = int(give.get("cash") or 0), int(want.get("cash") or 0)

        for a in gc:                        # lo que pone sobre la mesa, lo tiene
            self._hold(maker, a["ref"], tick, EV_PUBLISHED, asset=a.get("id"))
        for ref in wt:                      # lo que pide por referencia, le falta
            self._want(maker, ref, tick, EV_PUBLISHED,
                       bid=gcash if gcash and not gc else None)
        for a in wc:                        # pide una copia concreta: también le falta
            self._want(maker, a["ref"], tick, EV_PUBLISHED,
                       bid=gcash if gcash else None)

        # Una sola carta y efectivo: es una cotización con precio utilizable.
        if isinstance(oid, int):
            if wcash and len(gc) == 1 and not gt:
                self._record(Listing(oid, tick, maker, gc[0]["ref"], "ask",
                                     wcash, venue, exp))
            elif gcash and len(wt) == 1 and not wc:
                self._record(Listing(oid, tick, maker, wt[0], "bid",
                                     gcash, venue, exp))
            elif len(gc) == 1 and len(wt) == 1:
                # Trueque: sin efectivo no hay precio, pero sí intención.
                self._record(Listing(oid, tick, maker, gc[0]["ref"], "ask",
                                     None, venue, exp))

    def _record(self, l: Listing) -> None:
        self.listings[l.offer] = l
        self._view(l.team).listings.append(l)

    def _cancelled(self, p: dict) -> None:
        """Una cancelación retira la oferta, no la posesión.

        Que retire la venta no significa que ya no tenga la carta; que retire
        la puja sí sugiere que la consiguió por otro lado.
        """
        l = self.listings.get(p.get("offer"))
        if l is not None:
            l.cancelled = True

    def _thread_offer(self, tick: int, p: dict) -> None:
        """Ofertas de un equipo dentro de una conversación con un dealer.

        El feed publica las negociaciones ajenas enteras. Lo que un equipo le
        ofrece a un dealer es tan vinculante como una publicación: si le ofrece
        la carta, la tiene; si le ofrece efectivo por una referencia, le falta.
        """
        o = p.get("offer") or {}
        maker = o.get("maker")
        if not is_team(maker):
            return
        give, want = o.get("give") or {}, o.get("want") or {}
        gcash = int(give.get("cash") or 0)
        for a in _cards(give):
            self._hold(maker, a["ref"], tick, EV_PUBLISHED, asset=a.get("id"))
        for ref in _refs_wanted(want):
            self._want(maker, ref, tick, EV_PUBLISHED, bid=gcash or None)
        for a in _cards(want):
            self._want(maker, a["ref"], tick, EV_PUBLISHED, bid=gcash or None)

    def _thread_topic(self, tick: int, p: dict) -> None:
        """El tema con el que se abre una conversación.

        `{"buy": {"card": "RET-06"}}` es la declaración más limpia del feed:
        va a por esa referencia. `{"sell": {"assets": [...]}}` sólo trae IDs
        de copia, así que vale cuando esa copia ya se vio con su referencia.
        """
        team = p.get("team")
        if not is_team(team):
            return
        topic = p.get("topic") or {}
        buy, sell = topic.get("buy") or {}, topic.get("sell") or {}
        if isinstance(buy.get("card"), str):
            self._want(team, buy["card"], tick, EV_TOPIC)
        for aid in sell.get("assets") or []:
            ref = self.asset_ref.get(aid)
            if ref:                      # sin referencia conocida no se supone
                self._hold(team, ref, tick, EV_TOPIC, asset=aid)

    # -- inferencia -------------------------------------------------------
    def gaps(self, team: str, set_id: str) -> SetGaps:
        """Qué puede faltarle a un equipo de una página, con su incertidumbre.

        `unseen_refs` NO es «lo que le falta»: es lo que no le hemos visto. El
        feed sólo publica lo que se publica, y un equipo que guarda sus cartas
        y negocia en privado aparece aquí como si no tuviera nada.
        """
        page = self.page_refs(set_id)
        v = self.view(team)
        held = [r for r in page if r in v.held and v.held[r].likely_still_holds]
        sought = [r for r in page if r in v.sought and r not in held]
        unseen = [r for r in page if r not in held and r not in sought]

        in_set = v.set_events.get(set_id, 0)
        if not page:
            conf, caveat = "NONE", "sin catálogo cargado no hay página que comparar"
        elif not in_set:
            conf, caveat = "NONE", "ninguna actividad observada en este set"
        elif sought:
            conf = "DECLARED"
            caveat = "las de `sought_refs` las ha pedido él; las de `unseen_refs` no"
        elif len(held) >= max(1, round(len(page) * self.OBSERVED_FRACTION)):
            conf = "OBSERVED"
            caveat = "media página vista: la ausencia del resto empieza a pesar"
        else:
            conf = "WEAK"
            caveat = f"sólo {in_set} observaciones en el set: muestra corta"
        return SetGaps(team=team, set_id=set_id, page_refs=page, held_refs=held,
                       sought_refs=sought, unseen_refs=unseen,
                       confidence=conf, caveat=caveat)

    def completion_pressure(self, team: str, set_id: str) -> PagePressure:
        """Cuánto pagaría de más por una carta de esta página.

        La aritmética y lo que es supuesto están en `PagePressure`. Sin
        catálogo o sin una sola observación del equipo en el set, las cifras
        salen en `None`: no hay nada que calcular y no se rellena.
        """
        g = self.gaps(team, set_id)
        notes: list[str] = []
        books = [self.book[r] for r in g.page_refs if r in self.book]
        complete = len(books) == len(g.page_refs) and bool(books)
        page_sum = sum(books) if books else None
        if books and not complete:
            notes.append(f"book incompleto: {len(books)}/{len(g.page_refs)} cartas")

        bonus = page_sum * PAGE_BONUS if page_sum else None
        last = bonus
        amort = bonus / g.missing_max if bonus and g.missing_max else None

        if g.confidence == "NONE" or bonus is None:
            # Sin página o sin conducta observada no hay presión que estimar.
            return PagePressure(team, set_id, g, page_sum, bonus, None, None,
                                "NONE", notes + ["sin evidencia: no se estima"])
        if g.missing_max == 0:
            notes.append("página entera vista: ya no tiene hueco que pagar")
            return PagePressure(team, set_id, g, page_sum, bonus, None, None,
                                g.confidence, notes)
        if g.missing_max > 1:
            notes.append(
                f"le pueden faltar entre {g.missing_min} y {g.missing_max}: el techo "
                f"sólo aplica si {g.missing_max - 1} de las no vistas ya las tiene")
        notes.append("cifras en unidades de book; su multiplicador de set es privado")
        return PagePressure(team, set_id, g, page_sum, round(bonus, 2),
                            round(last, 2), round(amort, 2) if amort else None,
                            g.confidence, notes)

    # -- objetivos --------------------------------------------------------
    def sell_targets(self, oracle: Oracle, our_duplicates: Iterable[str], *,
                     exclude: Iterable[str] = ()) -> list[dict]:
        """A quién venderle lo que nos sobra, y con cuánta prima.

        Ordena por **evidencia** antes que por prima: cobrar mucho a quien
        quizá no quiera la carta no vale nada. Los motivos van en `basis`,
        cada uno con su etiqueta de evidencia, para que quien negocie sepa
        qué puede citar y qué es sólo nuestra inferencia.

        `rank` suma sólo las tres señales que han demostrado valer:
        +3 la pidió él (+2 si fue sólo un tema de conversación), +3 ya pagó
        sobreprecio en este set, +2 es la única carta de la página que no se
        le ha visto. Un equipo que simplemente negocia en el set se queda en
        0: aparece como candidato, no como objetivo.

        `premium_over` dice sobre qué se calcula la prima (`settled` /
        `quoted` / `book`). Si no hay ninguna referencia de precio, el campo
        de precio sale `None` en vez de una cifra inventada.
        """
        skip = set(exclude)
        paid = self._premiums_by_team(oracle)
        out: list[dict] = []
        for ref in dict.fromkeys(our_duplicates):       # sin repetir, en orden
            set_id = _set_of(ref)
            cm = oracle.cards.get(ref)
            book = (cm.book if cm and cm.book else None) or self.book.get(ref)
            # Orden deliberado: lo liquidado, luego el book (público y exacto) y
            # sólo al final lo pedido. Tomar como base la puja del propio
            # comprador sería circular: su urgencia quedaría dentro de la base
            # y la prima saldría en cero.
            if cm and cm.ordinary:
                base, over = statistics.median(cm.ordinary), "settled"
            elif book is not None:
                base, over = float(book), "book"
            elif cm and cm.fair is not None:
                base, over = cm.fair, "quoted"
            else:
                base, over = None, None
            for team, v in self.teams.items():
                if team in skip:
                    continue
                h = v.held.get(ref)
                s = v.sought.get(ref)
                # Si se le ve con la carta y no ha vuelto a pedirla, no es
                # comprador: es competencia vendiéndola.
                if h is not None and h.likely_still_holds and (
                        s is None or s.last_tick < h.last_tick):
                    continue
                basis: list[str] = []
                rank = 0
                extra: Optional[float] = None
                if s is not None:
                    rank += 3 if s.evidence != EV_TOPIC else 2
                    basis.append(f"la pidió ({s.evidence}, x{s.times}, "
                                 f"tick {s.last_tick})")
                anchor = paid.get((team, ref)) or paid.get((team, set_id))
                if anchor:
                    rank += 3
                    basis.append(f"pagó {anchor['paid']} P por {anchor['ref']} "
                                 f"= {anchor['multiple']}x book (SETTLED)")
                pp = self.completion_pressure(team, set_id)
                g = pp.gaps
                # El techo entero sólo se cobra si ésta es LA carta que le
                # falta: con cinco huecos abiertos el bonus está lejos y
                # cargárselo completo es inventarse su urgencia.
                last_card = (g.missing_max == 1
                             and ref in g.sought_refs + g.unseen_refs)
                if last_card and pp.extra_if_this_closes_page is not None:
                    extra = pp.extra_if_this_closes_page
                    rank += 2
                    basis.append(
                        f"página {set_id}: tiene {len(g.held_refs)}/"
                        f"{len(g.page_refs)} y ésta es la única que no se le "
                        f"ha visto; el bonus vale {pp.bonus_book} de book (INFERIDO)")
                elif pp.extra_amortized is not None:
                    extra = pp.extra_amortized
                    basis.append(
                        f"página {set_id}: {len(g.held_refs)}/"
                        f"{len(g.page_refs)} visto, bonus repartido entre "
                        f"{g.missing_max} huecos (INFERIDO)")
                if not basis:
                    continue
                ask = None
                if base is not None:
                    # El techo anclado en lo que ya pagó pesa más que el bonus
                    # teórico: es su conducta, no nuestro modelo.
                    by_model = base + (extra or 0)
                    by_paid = anchor["paid"] if anchor else None
                    ask = int(round(max(by_model, by_paid or 0)))
                out.append({
                    "team": team, "ref": ref, "rank": rank, "basis": basis,
                    "evidence": (EV_SETTLED if anchor else
                                 s.evidence if s else "INFERRED"),
                    "base_price": round(base, 1) if base is not None else None,
                    "premium_over": over,
                    "suggested_ask": ask,
                    "premium": (int(round(ask - base))
                                if ask is not None and base is not None else None),
                    "gap_confidence": pp.confidence,
                    "missing_max": g.missing_max,
                    "sample": v.sample,
                })
        return sorted(out, key=lambda r: (-r["rank"], -(r["premium"] or 0)))

    def buy_targets(self, oracle: Oracle, our_gaps: Iterable[str]) -> list[dict]:
        """Quién tiene lo que nos falta, y a cómo lo está dando.

        Primero las ofertas vivas con precio, de la más barata a la más cara;
        después los poseedores sin oferta publicada, con `ask: None`, porque
        a ésos hay que abrirles conversación. `cheap_vs` dice contra qué se
        midió la ganga (mercado entre equipos o suelo de dealer).
        """
        rows: list[dict] = []
        dealer_floor: dict[str, int] = {}
        for (dealer, ref, side), dl in oracle.dealers.items():
            if side == "ask" and dl.floor is not None:
                if ref not in dealer_floor or dl.floor < dealer_floor[ref]:
                    dealer_floor[ref] = dl.floor
        for ref in dict.fromkeys(our_gaps):
            cm = oracle.cards.get(ref)
            fair = cm.fair if cm else None
            floor = dealer_floor.get(ref)
            # Comparar una oferta contra una «justa» hecha de esa misma oferta
            # es medirse con uno mismo: primero lo liquidado, luego el suelo
            # del dealer, y sólo si no hay nada de eso, lo pedido.
            book = (cm.book if cm and cm.book else None) or self.book.get(ref)
            if cm and cm.ordinary:
                bench, vs = statistics.median(cm.ordinary), "settled"
            elif floor is not None:
                bench, vs = float(floor), "dealer"
            elif book is not None:
                bench, vs = float(book), "book"
            elif fair is not None:
                bench, vs = fair, "quoted"
            else:
                bench, vs = None, None
            for team, v in self.teams.items():
                live = [l for l in v.listings
                        if l.ref == ref and l.side == "ask"
                        and l.active(self.tick_max) and l.price is not None]
                h = v.held.get(ref)
                if live:
                    l = min(live, key=lambda x: x.price or 0)
                    rows.append({
                        "team": team, "ref": ref, "ask": l.price,
                        "venue": l.venue, "tick": l.tick,
                        "fair": round(fair, 1) if fair is not None else None,
                        "dealer_floor": floor,
                        "saving": (int(round(bench - l.price))
                                   if bench is not None else None),
                        "cheap_vs": vs,
                        "evidence": EV_PUBLISHED, "listed": True,
                    })
                elif h is not None and h.likely_still_holds:
                    rows.append({
                        "team": team, "ref": ref, "ask": None, "venue": None,
                        "tick": h.last_tick,
                        "fair": round(fair, 1) if fair is not None else None,
                        "dealer_floor": floor, "saving": None, "cheap_vs": None,
                        "evidence": h.evidence, "listed": False,
                        "copies_seen": h.copies_seen,
                    })
        # Con precio y barato primero; sin precio, al final por evidencia.
        return sorted(rows, key=lambda r: (
            not r["listed"], r["ask"] if r["ask"] is not None else 0,
            -_EV_RANK.get(r["evidence"], 0)))

    def _premiums_by_team(self, oracle: Oracle) -> dict[tuple[str, str], dict]:
        """Índice de `Oracle.premium_buyers()` por (equipo, ref) y (equipo, set).

        `Oracle` ya detecta quién pagó muy por encima del book; aquí sólo se
        indexa para cruzarlo con los huecos. Se queda el múltiplo más alto.
        """
        idx: dict[tuple[str, str], dict] = {}
        for r in oracle.premium_buyers():
            if not r.get("multiple"):
                continue
            for k in ((r["team"], r["ref"]), (r["team"], r["set"])):
                cur = idx.get(k)
                if cur is None or r["multiple"] > cur["multiple"]:
                    idx[k] = r
        return idx

    # -- resumen ----------------------------------------------------------
    @property
    def coverage(self) -> dict:
        """Tamaño de la muestra. Sin esto, nada de lo anterior se interpreta."""
        return {
            "events": len(self.seen),
            "tick_max": self.tick_max,
            "teams": len(self.teams),
            "held_observations": sum(len(v.held) for v in self.teams.values()),
            "sought_observations": sum(len(v.sought) for v in self.teams.values()),
            "listings": len(self.listings),
            "pages": len(self.pages),
        }

    def to_json(self) -> dict:
        """Volcado para otro proceso, sin importar este módulo."""
        return {
            "coverage": self.coverage,
            "teams": [
                {"team": v.team, "sample": v.sample,
                 "held": v.held_refs, "released": v.released_refs,
                 "sought": v.sought_refs,
                 "duplicates_observed": v.duplicates_observed,
                 "duplicates_offered": v.duplicates_offered(self.tick_max),
                 "active_sets": v.active_sets}
                for v in sorted(self.teams.values(), key=lambda x: -x.sample)
            ],
        }
