"""Inteligencia de mercado multi-venue (Day 2). Lógica pura, sin red: el coordinador la usa para decidir y enviar.

Responde a "¿cuál es la mejor utilización marginal de cada prima y cada carta?" separando cinco conceptos por carta:
  OUR_PRIVATE_VALUE   valor marginal privado (trading.Valuation, verificado contra collection_value; incluye el bono
                      de página una sola vez)                                                       [VERIFIED]
  MARKET_VALUE        estimación robusta a partir de ejecuciones > bids/asks cercanos > catálogo    [ESTIMATE]
  COLLECTION_VALUE    lo que gana/pierde la colección al recibir/entregar una copia (= privado)     [VERIFIED]
  TRADING_VALUE       excedente que deja una operación concreta en un venue concreto (ΔU)           [VERIFIED salvo fill]
  LIQUIDITY_VALUE     facilidad de ejecutar (demanda, oferta, spread)                               [HEURISTIC]

Regla dura: ninguna puntuación estratégica justifica una operación con ΔU < margen. Un ask NO es un valor: solo
significa que alguien pide ese precio. Las comisiones se leen del venue en cada ciclo (y la anunciada, si es mayor).
"""
from __future__ import annotations

import json
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Optional

import page_guard as pg
import trading as tr

RARITY_SCARCITY = {"common": 0.1, "uncommon": 0.3, "rare": 0.6, "epic": 0.85, "legendary": 1.0}


@dataclass
class IntelConfig:
    reserve: int = 100
    margin: float = 2.0
    per_card: int = 60
    max_spend: int = 80
    keep_one: bool = True
    allow_last_copy: frozenset = frozenset()
    duende_venue: str = "v02"
    duende_expiry_ticks: int = 120      # recomendación oficial para El Duende (configurable, NO constante global)
    target_expiry_ticks: int = 10       # vigencia EFECTIVA deseada en el resto de venues
    min_undercut_frac: float = 0.03
    max_undercut_frac: float = 0.10
    max_reprices_per_asset: int = 2
    min_reprice_delta: int = 2
    min_offer_age_ticks: int = 3
    stop_buffer_frac: float = 0.15      # no perseguir una guerra de precios que deja < 15 % sobre el suelo
    bid_open_frac: float = 0.6
    bid_ladder_frac: float = 0.15
    min_bid_fit: float = 0.5            # no pujar si nuestra reserva no llega al 50 % del ancla de mercado
    listing_prior: float = 0.3          # HEURISTIC hasta tener datos propios
    bid_prior: float = 0.25
    targeted_prior: float = 0.25
    swap_prior: float = 0.15
    learn_k: int = 5                    # peso del prior frente a la tasa aprendida
    capital_lock_rate: float = 0.02     # penalización por prima inmovilizada en una puja
    asset_lock_penalty: float = 0.5
    max_listing_premium: float = 1.25
    history_window_ticks: int = 240
    allow_arbitrage: bool = False       # el arbitraje se informa, no se ejecuta (no hay ejecución atómica)


# ------------------------------------------------------------------ venues y comisiones

@dataclass
class Venue:
    id: str
    name: str
    fee_bps: int
    fee_per_card: int
    mechanism: str
    owner: str
    pending: Optional[dict] = None
    rules: dict = field(default_factory=dict)

    def effective_fees(self) -> tuple[int, int]:
        """Comisión en vigor o la anunciada si es mayor (puede entrar en vigor antes de que se ejecute la oferta)."""
        bps, per = self.fee_bps, self.fee_per_card
        if self.pending:
            bps = max(bps, int(self.pending.get("fee_bps") or 0))
            per = max(per, int(self.pending.get("fee_per_card") or 0))
        return bps, per

    def fee(self, price: int, n_cards: int) -> int:
        bps, per = self.effective_fees()
        return math.ceil(max(0, price) * bps / 10000) + per * n_cards

    def as_dict(self) -> dict:
        bps, per = self.effective_fees()
        return {"venue": self.id, "status": "open", "fee_bps": bps, "fee_per_card": per}

    def allows(self, card: dict, level: int) -> bool:
        r = self.rules or {}
        if r.get("min_level") and level < int(r["min_level"]):
            return False
        if r.get("rarities") and card.get("rarity") not in r["rarities"]:
            return False
        if r.get("sets") and card.get("set") not in r["sets"]:
            return False
        return True


def venues_from(snap: dict) -> dict:
    team = snap["me"]["id"]
    out = {}
    for v in (snap.get("venues") or {}).get("venues", []):
        if v.get("status") != "open" or v.get("owner") == team:
            continue  # cerrado, o nuestro (no se puede operar en el propio mercado)
        if type(v.get("fee_bps")) is not int or type(v.get("fee_per_card")) is not int:
            continue  # comisiones desconocidas: no se valora
        out[v["venue"]] = Venue(v["venue"], v.get("name", v["venue"]), v["fee_bps"], v["fee_per_card"],
                                (v.get("rules") or {}).get("mechanism", "posted"), v.get("owner"), v.get("pending_fee"),
                                v.get("rules") or {})
    return out


# ------------------------------------------------------------------ libros de órdenes

@dataclass
class Quote:
    venue: str
    offer: int
    maker: str
    kind: str                 # ask | bid | swap
    ref: str                  # carta que se ofrece (ask/swap) o se pide (bid)
    price: int = 0
    asset: Optional[int] = None
    want_ref: Optional[str] = None   # swap: lo que pide a cambio
    to: Optional[str] = None
    expires: Optional[int] = None
    created: Optional[int] = None


def classify(o: dict) -> Optional[tuple]:
    g, w = o.get("give") or {}, o.get("want") or {}
    ga = [a for a in g.get("assets") or [] if isinstance(a, dict)]
    wt = list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]
    if len(ga) == 1 and ga[0].get("kind") == "card" and not g.get("cash") and not g.get("types"):
        if w.get("cash") and not wt and not w.get("assets"):
            return "ask", ga[0]["ref"], int(w["cash"]), ga[0].get("id"), None
        if len(wt) == 1 and str(wt[0]).startswith("card:") and not w.get("cash") and not w.get("assets"):
            return "swap", ga[0]["ref"], 0, ga[0].get("id"), wt[0][5:]
    if g.get("cash") and not ga and not g.get("types") and len(wt) == 1 and str(wt[0]).startswith("card:") \
            and not w.get("cash") and not w.get("assets"):
        return "bid", wt[0][5:], int(g["cash"]), None, None
    return None


def build_books(snap: dict, venues: dict) -> tuple[dict, list]:
    """{ref: {"asks": [...], "bids": [...], "swaps_give": [...], "swaps_want": [...]}} con las ofertas de TODOS los
    venues usables y las dirigidas a nosotros; y la lista de nuestras ofertas abiertas."""
    team, tick = snap["me"]["id"], snap["clock"]["tick"]
    books = defaultdict(lambda: {"asks": [], "bids": [], "swaps_give": [], "swaps_want": []})
    seen, ours = set(), []
    # /api/me/offers PRIMERO: es la fuente autoritativa de lo nuestro. Los tablones que anonimizan (El Rastro) muestran
    # NUESTRAS ofertas bajo un alias; leídas primero, se tomaban por ajenas y `ours` quedaba incompleto (publicaciones
    # y búsquedas duplicadas, e incluso aceptar una oferta propia).
    pools = [(None, (snap.get("offers") or {}).get("offers", []))]
    pools += [(vid, (snap.get("boards") or {}).get(vid, {}).get("offers", [])) for vid in venues]
    for vid, offers in pools:
        for o in offers:
            if o.get("id") in seen or o.get("status") != "open":
                continue
            seen.add(o.get("id"))
            if o.get("maker") == team:
                ours.append(o)
                continue
            if o.get("venue") not in venues or o.get("to") not in (None, team):
                continue
            if o.get("expires_tick") is not None and o["expires_tick"] <= tick:
                continue
            c = classify(o)
            if not c:
                continue
            kind, ref, price, asset, want = c
            q = Quote(o["venue"], o["id"], o.get("maker"), kind, ref, price, asset, want, o.get("to"),
                      o.get("expires_tick"), o.get("created_tick"))
            if kind == "ask":
                books[ref]["asks"].append(q)
            elif kind == "bid":
                books[ref]["bids"].append(q)
            else:
                books[ref]["swaps_give"].append(q)
                books[want]["swaps_want"].append(q)
    for b in books.values():
        b["asks"].sort(key=lambda q: q.price)
        b["bids"].sort(key=lambda q: -q.price)
    return books, ours


# ------------------------------------------------------------------ historial

class History:
    """Historial compacto en JSON Lines: libros resumidos por tick y liquidaciones (deduplicadas). Ventana acotada."""

    def __init__(self, path: Optional[str], window: int = 240, max_lines: int = 20000):
        self.path, self.window, self.max_lines = path, window, max_lines
        self.rows: list = []

    def load(self, tick: int) -> "History":
        self.rows = []
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("tick", 0) >= tick - self.window:
                            self.rows.append(r)
            except OSError as e:  # p. ej. lectura caducada en una carpeta sincronizada: el ciclo sigue sin historial
                print(f"   HISTORIAL no disponible ({type(e).__name__}: {e}); este tick se valora sin historial")
                self.rows = []
        return self

    def known_settlements(self) -> set:
        return {r.get("settlement") for r in self.rows if r.get("kind") == "settlement"}

    def record(self, tick: int, books: dict, settlements: list, ts: float) -> int:
        new = []
        for ref, b in books.items():
            for vid in {q.venue for q in b["asks"] + b["bids"]}:
                asks = [q.price for q in b["asks"] if q.venue == vid]
                bids = [q.price for q in b["bids"] if q.venue == vid]
                new.append({"kind": "book", "tick": tick, "ts": round(ts), "venue": vid, "ref": ref,
                            "best_bid": max(bids) if bids else None, "best_ask": min(asks) if asks else None,
                            "bid_count": len(bids), "ask_count": len(asks)})
        known = self.known_settlements()
        for s in settlements:
            if s.get("settlement") in known:
                continue
            cards = [i for i in s.get("items", []) if i.get("kind") == "card"]
            new.append({"kind": "settlement", "tick": s.get("tick", tick), "settlement": s.get("settlement"),
                        "venue": s.get("venue"), "price": s.get("price"), "fee": s.get("fee"),
                        "refs": [i.get("ref") for i in cards], "parties": s.get("parties")})
        self.rows += new
        if self.path and new:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    for r in new:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                self._prune()
            except OSError as e:   # disco lento (p. ej. carpeta sincronizada): el historial es opcional, el ciclo sigue
                print(f"   HISTORIAL no se pudo guardar ({type(e).__name__}: {e}); se sigue en memoria")
        return len(new)

    def _prune(self):
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > self.max_lines:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(lines[-self.max_lines // 2:])
            os.replace(self.path + ".tmp", self.path)

    def executions(self, ref: str) -> list:
        """Precios ejecutados de una sola carta por efectivo (los lotes no dan un precio por carta fiable)."""
        return [r["price"] for r in self.rows if r.get("kind") == "settlement" and r.get("refs") == [ref]
                and r.get("price")]

    def series(self, ref: str, venue: Optional[str] = None) -> list:
        out = []
        for r in self.rows:
            if r.get("kind") == "book" and r.get("ref") == ref and (venue is None or r.get("venue") == venue):
                bb, ba = r.get("best_bid"), r.get("best_ask")
                mid = (bb + ba) / 2 if bb and ba else (ba or bb)
                if mid:
                    out.append((r["tick"], mid))
        return sorted(out)


def public_settlements(feed_events: list) -> list:
    out = []
    for e in feed_events:
        if e.get("type") == "settlement":
            p = e.get("payload") or {}
            out.append({**p, "tick": p.get("tick", e.get("tick"))})
    return out


def trend(series: list, min_points: int = 6, min_span: int = 5, threshold: float = 0.08) -> str:
    """UP / DOWN / STABLE solo con evidencia suficiente; si no, UNKNOWN."""
    if len(series) < min_points or series[-1][0] - series[0][0] < min_span:
        return "UNKNOWN"
    k = max(2, len(series) // 3)
    a, b = statistics.median(v for _, v in series[:k]), statistics.median(v for _, v in series[-k:])
    if a <= 0:
        return "UNKNOWN"
    ch = (b - a) / a
    return "UP" if ch > threshold else "DOWN" if ch < -threshold else "STABLE"


# ------------------------------------------------------------------ estimación de mercado

@dataclass
class MarketEstimate:
    value: Optional[float]
    confidence: str            # HIGH | MEDIUM | LOW | UNKNOWN
    source: str
    evidence: list


def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def estimate_market_value(asks: list, bids: list, executions: list, catalog_book: Optional[float]) -> MarketEstimate:
    """1) ejecuciones, 2) bid y ask cercanos (microprecio), 3) asks bajos robustos, 4) bids altos robustos, 5) catálogo.
    Un ask alto aislado nunca eleva la estimación: se usa el percentil 25 de los asks."""
    if len(executions) >= 3:
        v = statistics.median(executions)
        return MarketEstimate(round(v, 1), "HIGH" if len(executions) >= 5 else "MEDIUM", "ejecuciones",
                              [f"mediana de {len(executions)} precios ejecutados"])
    ap, bp = [q.price for q in asks], [q.price for q in bids]
    if ap and bp:
        ba, bb = min(ap), max(bp)
        if bb <= ba:
            na, nb = len(ap), len(bp)
            micro = (bb * na + ba * nb) / (na + nb)  # más compradores empujan hacia el ask
            rel = (ba - bb) / max(1.0, (ba + bb) / 2)
            ev = [f"bid {bb} / ask {ba} (spread {rel:.0%}), {nb} bids y {na} asks"]
            if executions:
                ev.append(f"{len(executions)} ejecuciones (insuficientes solas)")
                micro = (micro + statistics.median(executions)) / 2
            return MarketEstimate(round(micro, 1), "MEDIUM" if rel <= 0.3 else "LOW", "microprecio", ev)
    if ap:
        v = _pct(ap, 0.25)
        ev = [f"percentil 25 de {len(ap)} asks (cota superior, no valor)"]
        if catalog_book and v > 1.5 * catalog_book:  # un ask alto no demuestra valor: sin compradores, manda el prior
            v = 1.5 * catalog_book
            ev.append(f"limitado a 1,5 × catálogo ({catalog_book}): no hay pujas ni ejecuciones que lo respalden")
        return MarketEstimate(round(v, 1), "LOW", "asks", ev)
    if bp:
        v = _pct(bp, 0.75)
        return MarketEstimate(round(v, 1), "LOW", "bids", [f"percentil 75 de {len(bp)} bids (cota inferior)"])
    if executions:
        return MarketEstimate(round(statistics.median(executions), 1), "LOW", "ejecuciones",
                              [f"{len(executions)} ejecuciones"])
    if catalog_book:
        return MarketEstimate(float(catalog_book), "UNKNOWN", "catálogo", ["valor de catálogo (prior débil)"])
    return MarketEstimate(None, "UNKNOWN", "sin datos", [])


# ------------------------------------------------------------------ puntuaciones (HEURISTIC)

def _sat(x: float, k: float = 3.0) -> float:
    return 1 - math.exp(-max(0.0, x) / k)


def demand_score(b: dict, executions: list) -> float:
    makers = {q.maker for q in b["bids"]}
    directed = sum(1 for q in b["bids"] if q.to)
    return round(_sat(len(makers) + 0.5 * directed + 0.5 * len(b["swaps_want"]) + 0.5 * len(executions)), 2)


def supply_score(b: dict) -> float:
    return round(_sat(len({q.maker for q in b["asks"]}) + 0.5 * len(b["swaps_give"])), 2)


def scarcity_score(card: dict, supply: float) -> float:
    return round(RARITY_SCARCITY.get(card.get("rarity"), 0.3) * (1 - 0.5 * supply), 2)


def liquidity_score(b: dict, demand: float, supply: float) -> float:
    if b["asks"] and b["bids"]:
        ba, bb = b["asks"][0].price, b["bids"][0].price
        tight = 1 - min(1.0, max(0.0, ba - bb) / max(1.0, (ba + bb) / 2))
    else:
        tight = 0.0
    return round(0.5 * min(demand, supply) + 0.25 * (demand + supply) / 2 + 0.25 * tight, 2)


# ------------------------------------------------------------------ estado por carta

@dataclass
class MarketCardState:
    ref: str
    name: str
    set: str
    rarity: str
    our_value: float          # valor de la última copia (o 0 si no la tenemos)
    copies: int
    asset_ids: list
    missing: bool
    completes_page: bool
    gain_if_bought: float
    loss_if_sold: Optional[float]
    best_bid: Optional[Quote]
    second_bid: Optional[Quote]
    best_ask: Optional[Quote]
    second_ask: Optional[Quote]
    spread: Optional[int]
    bids: list
    asks: list
    swaps_give: list
    swaps_want: list
    supply: float
    demand: float
    scarcity: float
    liquidity: float
    market: MarketEstimate
    trend: str
    executions: list
    venues: list

    def short(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("bids", "asks", "swaps_give", "swaps_want")}
        return d


def build_states(snap: dict, val: tr.Valuation, counts: Counter, books: dict, history: History, venues: dict,
                 refs: Optional[set] = None) -> dict:
    out = {}
    for ref, card in val.cards.items():
        if not card["released"] or card.get("hidden") or (refs is not None and ref not in refs):
            continue
        b = books.get(ref) or {"asks": [], "bids": [], "swaps_give": [], "swaps_want": []}
        n = counts.get(ref, 0)
        ids = sorted(a["id"] for a in snap["me"]["assets"] if a.get("ref") == ref)
        gain, notes = val.delta(counts, Counter({ref: 1}), Counter())
        loss = None
        if n:
            dv, _ = val.delta(counts, Counter(), Counter({ref: 1}))
            loss = round(-dv, 2)
        execs = history.executions(ref)
        dem, sup = demand_score(b, execs), supply_score(b)
        out[ref] = MarketCardState(
            ref, card.get("name", ref), card["set"], card.get("rarity"), round(val.copy_value(ref, n - 1), 2) if n else 0.0,
            n, ids, n == 0, any("completa la página" in x for x in notes), round(gain, 2), loss,
            b["bids"][0] if b["bids"] else None, b["bids"][1] if len(b["bids"]) > 1 else None,
            b["asks"][0] if b["asks"] else None, b["asks"][1] if len(b["asks"]) > 1 else None,
            (b["asks"][0].price - b["bids"][0].price) if b["asks"] and b["bids"] else None,
            b["bids"], b["asks"], b["swaps_give"], b["swaps_want"], sup, dem, scarcity_score(card, sup),
            liquidity_score(b, dem, sup), estimate_market_value(b["asks"], b["bids"], execs, card.get("book")),
            trend(history.series(ref)), execs, sorted({q.venue for q in b["asks"] + b["bids"]}))
    return out


def strategic_value(st: MarketCardState) -> float:
    """SOLO para priorizar entre operaciones ya rentables (nunca para justificar ΔU < margen)."""
    page = 5.0 if st.completes_page else 0.0
    return round(page + 2.0 * st.liquidity + 1.0 * st.scarcity, 2)


# ------------------------------------------------------------------ probabilidad de ejecución

def learned_rate(actions: list, venue: str, kind: str) -> tuple[Optional[float], int]:
    done = [a for a in actions if a.get("type") == kind and a.get("venue", "rastro") == venue
            and a.get("status") in ("settled", "released")]
    if not done:
        return None, 0
    return sum(1 for a in done if a["status"] == "settled") / len(done), len(done)


def fill_probability(side: str, price: int, st: MarketCardState, venue: Venue, cfg: IntelConfig,
                     actions: list = (), targeted_confidence: float = 0.0) -> tuple[float, str]:
    """HEURISTIC (y LEARNED cuando hay al menos una publicación propia cerrada en ese venue)."""
    brokered = venue.mechanism in ("board", "auto")  # El Rastro ("posted") no cruza solo: alguien debe aceptar
    if side == "ask":
        if brokered and any(q.price >= price and q.venue == venue.id for q in st.bids):
            p0 = 0.9  # hay una puja que ya cruza en el mismo venue y el broker/motor la empareja
        else:
            p0 = cfg.listing_prior * (0.5 + st.demand)
            comp = [q.price + _venue_fee(q.venue, q.price) for q in st.asks]
            ours = price + venue.fee(price, 1)
            if comp and ours < min(comp):
                p0 *= 1.3      # somos el vendedor más barato en coste total para el comprador
            elif comp and ours > min(comp):
                p0 *= 0.5      # hay un rival más barato: el comprador lo elegirá antes
            if st.market.value and st.market.confidence in ("HIGH", "MEDIUM") and price > st.market.value:
                p0 *= 0.6
            elif st.market.value and price > 1.5 * st.market.value:
                p0 *= 0.5
    elif side in ("bid", "targeted"):
        anchor = min([q.price for q in st.asks] or [st.market.value or 0]) or None
        ratio = min(1.0, price / anchor / 0.7) if anchor else 1.0  # una puja muy por debajo del mercado casi no se llena
        if side == "targeted":
            p0 = cfg.targeted_prior * targeted_confidence * ratio ** 2
        elif brokered and any(q.price <= price and q.venue == venue.id for q in st.asks):
            p0 = 0.9
        else:
            p0 = cfg.bid_prior * (0.5 + st.supply) * ratio ** 2
            if st.bids and st.bids[0].price >= price:
                p0 *= 0.3
    else:  # swap
        p0 = cfg.swap_prior * (0.5 + st.supply)
    p0 = max(0.005, min(0.9, p0))
    rate, n = learned_rate(list(actions), venue.id, {"ask": "list", "bid": "bid", "targeted": "bid",
                                                     "swap": "swap_list"}[side])
    label = fill_confidence(n)
    if rate is None:
        return round(p0, 3), label
    k = cfg.learn_k * (2 if n < 5 else 1)  # con muy pocos datos el prior pesa el doble: una muestra no manda
    p = (n * rate + k * p0) / (n + k)
    return round(max(0.02, min(0.9, p)), 3), f"{label} ({n} propias en {venue.id})"


def fill_confidence(n: int) -> str:
    """Banda de confianza de la tasa propia: 0 HEURISTIC · 1-4 EARLY DATA · 5-14 LEARNING · 15+ LEARNED."""
    return "HEURISTIC" if n <= 0 else "EARLY DATA / LOW CONFIDENCE" if n < 5 else "LEARNING" if n < 15 else "LEARNED"


def fill_stats(actions: list, venue: str, side: str) -> tuple[int, str]:
    kind = {"ask": "list", "bid": "bid", "targeted": "bid", "swap": "swap_list"}[side]
    _, n = learned_rate(list(actions), venue, kind)
    return n, fill_confidence(n)


_VENUES: dict = {}


def _venue_fee(vid: str, price: int) -> int:
    v = _VENUES.get(vid)
    return v.fee(price, 1) if v else 0


# ------------------------------------------------------------------ precios

def undercut_step(price: int, spread: Optional[int], liquidity: float, cfg: IntelConfig) -> int:
    """Proporcional al precio (3-10 %), más agresivo cuanto menos líquido; mínimo 1 P."""
    frac = cfg.min_undercut_frac + (cfg.max_undercut_frac - cfg.min_undercut_frac) * (1 - liquidity)
    step = max(1, round(price * frac))
    if spread is not None and spread > 0:
        step = min(step, max(1, spread // 2))
    return step


def competitive_sell_price(st: MarketCardState, venue: Venue, cfg: IntelConfig, reprices: int = 0,
                           last_price: Optional[int] = None) -> dict:
    """Precio de venta en `venue`: ser el vendedor más barato EN COSTE TOTAL PARA EL COMPRADOR (precio + comisión del
    venue) sin bajar del suelo = pérdida de colección + margen. Como maker no pagamos comisión: la paga quien acepta."""
    floor = math.ceil((st.loss_if_sold or 0) + cfg.margin)
    out = {"venue": venue.id, "floor": floor, "price": None, "reason": "", "stop": False}
    comp = [(q.price + _venue_fee(q.venue, q.price), q) for q in st.asks]
    if comp:
        best_cost, q = min(comp, key=lambda x: x[0])
        step = undercut_step(q.price, st.spread, st.liquidity, cfg)
        target_cost = best_cost - step
        p = target_cost
        while p > 0 and p + venue.fee(p, 1) > target_cost:
            p -= 1
        reason = (f"undercut: rival {q.price} P en {q.venue} (coste comprador {best_cost}) → {p} P en {venue.id} "
                  f"(coste comprador {p + venue.fee(p, 1)}), paso {step}")
    else:
        mv = st.market.value or 0
        p = math.ceil(max(floor, mv)) if mv else floor + 1
        reason = f"sin vendedores rivales: valor de mercado estimado {st.market.value} ({st.market.confidence})"
    if st.market.value and st.market.confidence in ("HIGH", "MEDIUM"):
        cap = math.floor(st.market.value * cfg.max_listing_premium)
        if p > cap >= floor:
            p, reason = cap, reason + f"; limitado a {cfg.max_listing_premium:.0%} del valor de mercado"
    if st.best_bid and st.best_bid.venue == venue.id and st.best_bid.price >= p:
        p = st.best_bid.price  # una puja cruza: no hace falta bajar más
    if p < floor:
        out.update(reason=f"no competir: el precio necesario ({p} P) queda bajo el suelo ({floor} P)", stop=True)
        return out
    if reprices >= cfg.max_reprices_per_asset:
        out.update(reason=f"guerra de precios: ya {reprices} reprecios de esta carta", stop=True)
        return out
    if reprices and p < floor * (1 + cfg.stop_buffer_frac):
        out.update(reason=f"guerra de precios: {p} P quedaría a menos del {cfg.stop_buffer_frac:.0%} sobre el suelo", stop=True)
        return out
    if last_price is not None and last_price - p < cfg.min_reprice_delta and reprices:
        out.update(reason="la mejora sobre nuestro precio anterior es demasiado pequeña", stop=True)
        return out
    out.update(price=int(p), reason=reason)
    return out


def calculate_bid_price(st: MarketCardState, venue: Venue, cfg: IntelConfig, cap: int,
                        last_bid: Optional[int] = None, urgency: float = 0.0) -> dict:
    """Puja racional: nunca el máximo. reservation = min(ganancia − margen, por carta, capital). Empieza por debajo
    (60 % del ancla de mercado o 1 paso sobre la mejor puja rival) y sube en escalera (15 % de la distancia que queda,
    más deprisa con urgencia de fin de día). Normalmente bid < reservation."""
    reservation = math.floor(min(st.gain_if_bought - cfg.margin, cfg.per_card, cap))
    out = {"venue": venue.id, "reservation": reservation, "price": None, "reason": ""}
    if reservation < 1:
        out["reason"] = f"reserva {reservation} P: no compensa pujar"
        return out
    anchors = [q.price for q in st.asks]
    anchor = min(anchors) if anchors else (st.market.value or st.gain_if_bought)
    if st.market.value and st.market.confidence != "UNKNOWN":
        anchor = min(anchor, st.market.value) if anchors else st.market.value
    if reservation < cfg.min_bid_fit * anchor:
        out["reason"] = (f"no competitivo: nuestra reserva {reservation} P es < {cfg.min_bid_fit:.0%} del ancla de "
                         f"mercado {anchor} P (capital o valor insuficientes)")
        return out
    rival = st.best_bid.price if st.best_bid else 0
    step = undercut_step(max(rival, anchor, 1), st.spread, st.liquidity, cfg)
    if last_bid is None:
        p = max(round(cfg.bid_open_frac * anchor), rival + step if rival else 1)
        reason = f"apertura: {cfg.bid_open_frac:.0%} del ancla {anchor} P" + (f", sobre la rival {rival} P" if rival else "")
    else:
        frac = cfg.bid_ladder_frac * (1 + 2 * urgency)
        p = last_bid + max(step, math.ceil(frac * max(0, reservation - last_bid)))
        reason = f"escalera desde {last_bid} P (urgencia {urgency:.0%})"
    top = reservation - 1 if urgency < 0.9 else reservation
    p = min(p, top)
    if anchors and p >= min(anchors) and min(anchors) + venue.fee(min(anchors), 1) <= reservation:
        out["reason"] = "hay un ask aceptable directamente: comprar, no pujar"
        return out
    if p < 1 or (last_bid is not None and p <= last_bid):
        out["reason"] = "la escalera ha llegado al techo"
        return out
    out.update(price=int(p), reason=reason)
    return out


def ticks_left(clock: dict) -> Optional[int]:
    """Ticks hasta el cierre del día según /api/clock (el ritmo cambia; no se asume una duración fija)."""
    import datetime
    closes, ts = clock.get("closes"), clock.get("tick_seconds")
    if not closes or not ts:
        return None
    try:
        end = datetime.datetime.fromisoformat(closes)
        now = datetime.datetime.now(end.tzinfo)
        return max(0, int((end - now).total_seconds() // ts))
    except ValueError:
        return None


# ------------------------------------------------------------------ planificador multi-venue

def plan(snap: dict, cfg: IntelConfig, *, pendings: list = (), spent: int = 0, actions: list = (),
         history: Optional[History] = None, expiry_ratio: float = 1.0, passive_cap: Optional[int] = None) -> dict:
    """Misma interfaz que trading.plan, ampliada: todas las oportunidades de todos los venues, con venue, ΔU inmediato
    o esperado, probabilidad de ejecución, coste de oportunidad y valor estratégico."""
    me, catalog, clock = snap["me"], snap["catalog"], snap["clock"]
    team, tick = me["id"], clock["tick"]
    val = tr.Valuation(catalog, me.get("affinity") or {})
    counts = tr.counts_of(me["assets"])
    other = round(sum(float(a.get("your_value") or 0) for a in me["assets"] if a.get("kind") != "card"), 2)
    server = me.get("collection_value")
    ok, model = val.calibrate(counts, None if server is None else float(server) - other)
    venues = venues_from(snap)
    _VENUES.clear()
    _VENUES.update(venues)
    books, ours = build_books(snap, venues)
    history = history or History(None)
    states = build_states(snap, val, counts, books, history, venues)
    res = tr.resources((snap.get("offers") or {}).get("offers", []), team, list(pendings))
    free = tr.free_cash(me["cash"], cfg.reserve, res)
    buy_cap = max(0, min(free, cfg.max_spend - spent))
    # pujas PASIVAS: solo el capital de mercado (el coordinador aparta liquidez de vendedores y colchón táctico)
    bid_cap = buy_cap if passive_cap is None else max(0, min(buy_cap, passive_cap))
    left = ticks_left(clock)
    urgency = 0.0 if left is None else max(0.0, min(1.0, 1 - left / 60))
    out = {"tick": tick, "team": team, "cash": me["cash"], "model_value": round(model + other, 2),
           "server_value": server, "valuation_verified": ok, "reserve": cfg.reserve, "free_cash": free,
           "reserved_cash": res.reserved_cash, "pending_cash": res.pending_cash, "spent_session": spent,
           "budget_left": cfg.max_spend - spent, "open_offers": len(res.open_offers), "opportunities": [],
           "rejected": [], "notes": [], "venues": {k: asdict(v) for k, v in venues.items()},
           "states": states, "ticks_left": left, "urgency": round(urgency, 2), "arbitrage": []}
    if not ok:
        out["notes"].append(f"Modelo {model + other} ≠ collection_value {server}: nada se ejecuta")
    level = int(me.get("level") or 1)

    def add(o):
        o.setdefault("blockers", [])
        if not ok and not o.get("protected_page_cancel"):  # la protección no depende de la valoración
            o["blockers"].append("valoración no verificada")
        protect = pg.guard_candidate(o, snap, res.locked_assets)  # prioridad 1: inviable, no "muy negativa"
        if protect:
            o["blockers"] = protect + o["blockers"]
            o["protected_page_block"] = True
        side = {"list": "ask", "bid": "targeted" if o.get("to") else "bid", "swap_list": "swap"}.get(o.get("type"))
        if side and o.get("venue"):
            o["sample_count"], o["confidence_label"] = fill_stats(actions, o["venue"], side)
        o["key"] = tr.idem_key({**o, "give": o.get("venue"), "want": o.get("to")})
        out["opportunities"].append(o)

    # 1. Ofertas ejecutables ya (tablones de todos los venues y dirigidas a nosotros): ΔU exacto, comisión del venue.
    raw = []
    for vid in venues:
        raw += (snap.get("boards") or {}).get(vid, {}).get("offers", [])
    raw += [o for o in (snap.get("offers") or {}).get("offers", []) if o.get("maker") != team]
    seen = set()
    immediate_sell = {}  # ref -> mejor ΔU de vender ya (para coste de oportunidad)
    for o in raw:
        if o.get("id") in seen or o.get("venue") not in venues:
            continue
        offer_refs = {r for side in (o.get("give") or {}, o.get("want") or {})
                      for r in (side.get("cards") or []) if isinstance(r, str)}
        offer_refs.update(t[5:] for side in (o.get("give") or {}, o.get("want") or {})
                          for t in (side.get("types") or []) if isinstance(t, str) and t.startswith("card:"))
        offer_refs.update(a.get("ref") for side in (o.get("give") or {}, o.get("want") or {})
                          for a in (side.get("assets") or []) if isinstance(a, dict) and a.get("ref"))
        if any(r.startswith("CHA-") for r in offer_refs) and o.get("venue") != "v05":
            out["rejected"].append({"offer": o.get("id"), "venue": o.get("venue"),
                                    "why": "CHA-* solo puede operarse en v05"})
            continue
        seen.add(o.get("id"))
        p, why = tr.parse_offer(o, team=team, tick=tick, own_venue=None, my_assets=me["assets"],
                                locked=res.locked_assets, max_cards=4)
        if p is None:
            if o.get("maker") != team:
                out["rejected"].append({"offer": o.get("id"), "venue": o.get("venue"), "why": why})
            continue
        v = venues[o["venue"]]
        e = tr.evaluate(p, val, counts, v.as_dict(), keep_one=cfg.keep_one, allow_last_copy=cfg.allow_last_copy)
        if p.cash_out:
            if p.cash_out + e.fee > buy_cap:
                e.blockers.append(f"coste {p.cash_out + e.fee} P > capacidad {buy_cap} P")
            if (p.cash_out + e.fee) / max(1, sum(p.receive.values())) > cfg.per_card:
                e.blockers.append(f"> {cfg.per_card} P por carta")
        if e.du < cfg.margin:
            continue
        kind = "vender" if p.cash_in and not p.receive else "comprar" if p.cash_out and not p.deliver else "intercambio"
        refs = list(p.receive) + list(p.deliver)
        bonus = max((strategic_value(states[r]) for r in refs if r in states), default=0.0)
        if kind == "vender":
            r = next(iter(p.deliver))
            immediate_sell[r] = max(immediate_sell.get(r, 0), e.du)
        add({"type": "accept", "kind": kind, "venue": o["venue"], "offer": p.offer_id, "source": p.source,
             "maker": p.maker, "receive": dict(p.receive), "deliver": dict(p.deliver), "assets": p.deliver_assets,
             "price": p.price, "fee": e.fee, "cash": e.cash, "dv": e.dv, "du": e.du, "immediate_du": e.du,
             "expected_du": e.du, "p_fill": 1.0, "p_basis": "VERIFIED (ejecución inmediata salvo que otro la tome)",
             "strategic": bonus, "score": round(1000 + e.du + bonus, 2), "expires": p.expires_tick,
             "notes": e.notes + [f"comisión {e.fee} P en {o['venue']} (VERIFIED desde /api/venues)"],
             "blockers": e.blockers, "uncertainty": "inmediata", "reason": f"{kind} ya en {o['venue']}"})

    # 2. Arbitraje entre venues (solo informe: dos patas no atómicas).
    for ref, st in states.items():
        if st.best_ask and st.best_bid and st.best_ask.offer != st.best_bid.offer:
            buy = st.best_ask.price + venues[st.best_ask.venue].fee(st.best_ask.price, 1)
            sell = st.best_bid.price - venues[st.best_bid.venue].fee(st.best_bid.price, 1)
            if sell - buy >= cfg.margin:
                out["arbitrage"].append({"ref": ref, "buy": f"{st.best_ask.price} en {st.best_ask.venue}",
                                         "sell": f"{st.best_bid.price} en {st.best_bid.venue}", "gross": sell - buy,
                                         "note": "no atómico: riesgo de inventario; se informa, no se ejecuta"})

    def last_of(type_, ref=None, asset=None):
        xs = [a for a in actions if a.get("type") == type_ and (ref is None or a.get("ref") == ref)
              and (asset is None or a.get("asset") == asset)]
        return xs[-1] if xs else None

    def reprice_count(ref):
        return sum(1 for a in actions if a.get("type") == "cancel" and a.get("ref") == ref
                   and str(a.get("reason", "")).startswith("reprecio"))

    usable = {k: v for k, v in venues.items()}
    duende = usable.get(cfg.duende_venue)

    def expiry_for(vid):
        return cfg.duende_expiry_ticks if vid == cfg.duende_venue else tr.listing_request(cfg.target_expiry_ticks,
                                                                                         expiry_ratio)

    # 3. Duplicados: comparar vender ya (arriba), publicar en el mejor venue, o reservar para un trueque.
    selling = Counter()
    for o in ours:
        c = classify(o)
        if c and c[0] in ("ask", "swap"):
            selling[c[1]] += 1
    buying = Counter()  # cartas que ya perseguimos: pujas Y trueques que la piden (una sola vía por carta)
    for o in ours:
        c = classify(o)
        if c and c[0] == "bid":
            buying[c[1]] += 1
        elif c and c[0] == "swap" and c[4]:
            buying[c[4]] += 1
    for ref, st in states.items():
        if st.copies < 2 or selling.get(ref) or st.loss_if_sold is None:
            continue
        free_ids = [i for i in st.asset_ids if i not in res.locked_assets]
        if not free_ids:
            continue
        best = None
        venue_choices = ([usable["v05"]] if ref.startswith("CHA-") and "v05" in usable else
                         ([] if ref.startswith("CHA-") else list(usable.values())))
        for v in venue_choices:
            if not v.allows(val.cards[ref], level):
                continue
            last = last_of("list", ref=ref)
            cp = competitive_sell_price(st, v, cfg, reprice_count(ref), last.get("price") if last else None)
            if cp["price"] is None:
                out["rejected"].append({"ref": ref, "venue": v.id, "why": cp["reason"]})
                continue
            p, basis = fill_probability("ask", cp["price"], st, v, cfg, actions)
            surplus = cp["price"] - st.loss_if_sold
            eu = round(p * surplus, 2)
            key = (eu, -v.fee(cp["price"], 1), v.id == cfg.duende_venue)
            if best is None or key > best[6]:
                best = (eu, v, cp, p, basis, surplus, key)  # empate: menor coste comprador, después El Duende
        if not best:
            continue
        eu, v, cp, p, basis, surplus, _ = best
        if surplus < cfg.margin:
            continue
        alt = immediate_sell.get(ref, 0)
        add({"type": "list", "kind": "publicar venta", "venue": v.id, "ref": ref, "asset": free_ids[-1],
             "price": cp["price"], "fee": 0, "cash": cp["price"], "dv": round(-st.loss_if_sold, 2),
             "du": round(surplus, 2), "immediate_du": 0, "expected_du": eu, "p_fill": p, "p_basis": basis,
             "strategic": strategic_value(st), "opportunity_cost": alt,
             "score": round(eu + 0.1 * strategic_value(st) - cfg.asset_lock_penalty, 2),
             "expires": tick + expiry_for(v.id), "expires_in": expiry_for(v.id), "floor": cp["floor"],
             "notes": [cp["reason"], f"neto vendedor {cp['price']} P (el comprador paga {v.fee(cp['price'], 1)} P de "
                                     f"comisión en {v.id})", f"esperado {eu} P = {p:.0%} × {surplus:.1f} P"],
             "uncertainty": f"ejecución NO garantizada ({basis})", "reason": "duplicado: mejor venta esperada"})

    # 4. Cartas ausentes: comprar ya (arriba) frente a puja pública, puja dirigida o trueque.
    evidence = defaultdict(list)  # ref -> equipos con evidencia publicada de tenerla (feed offer.listed / dirigidas)
    try:
        import campaigns as cp_mod
        for x in cp_mod.evidence(snap, tick, cp_mod.CampaignConfig()):
            if x["side"] == "sells":
                evidence[x["ref"]].append(x)
    except Exception:  # el módulo de campañas es opcional para la inteligencia de mercado
        pass
    dups = [r for r, st in states.items() if st.copies >= 2 and not selling.get(r)]
    for ref, st in states.items():
        if not st.missing or buying.get(ref) or st.gain_if_bought < cfg.margin + 1:
            continue
        cands = []
        venue_choices = ([usable["v05"]] if ref.startswith("CHA-") and "v05" in usable else
                         ([] if ref.startswith("CHA-") else list(usable.values())))
        for v in venue_choices:
            if not v.allows(val.cards[ref], level):
                continue
            last = last_of("bid", ref=ref)
            bp = calculate_bid_price(st, v, cfg, bid_cap, last.get("price") if last else None, urgency)
            if bp["price"] is None:
                continue
            p, basis = fill_probability("bid", bp["price"], st, v, cfg, actions)
            surplus = st.gain_if_bought - bp["price"]
            cands.append((p * surplus, v, bp, p, basis, surplus, None))
            for x in evidence.get(ref, [])[:1]:
                pt, bt = fill_probability("targeted", bp["price"], st, v, cfg, actions, targeted_confidence=0.8)
                cands.append((pt * surplus, v, bp, pt, bt + " · propiedad: OBSERVED (oferta publicada)", surplus,
                              x["team"]))
        if cands:
            eu, v, bp, p, basis, surplus, to = max(cands, key=lambda c: (c[0], c[1].id == cfg.duende_venue))
            add({"type": "bid", "kind": "puja dirigida" if to else "publicar compra", "venue": v.id, "to": to,
                 "ref": ref, "price": bp["price"], "fee": 0, "cash": -bp["price"], "dv": st.gain_if_bought,
                 "du": round(surplus, 2), "immediate_du": 0, "expected_du": round(eu, 2), "p_fill": p,
                 "p_basis": basis, "strategic": strategic_value(st), "reservation": bp["reservation"],
                 "score": round(eu + 0.1 * strategic_value(st) - cfg.capital_lock_rate * bp["price"], 2),
                 "expires": tick + expiry_for(v.id), "expires_in": expiry_for(v.id),
                 "notes": [bp["reason"], f"reserva privada {bp['reservation']} P (no se publica)",
                           "completa una página (bono incluido una vez)" if st.completes_page else
                           f"falta en {st.set}"],
                 "uncertainty": f"ejecución NO garantizada ({basis})", "reason": "carta ausente"})
        # trueque: un duplicado nuestro por esta carta, solo si hay evidencia de que alguien la tiene
        if st.supply > 0 or evidence.get(ref):
            for d in dups:
                ds = states[d]
                ids = [i for i in ds.asset_ids if i not in res.locked_assets]
                if not ids:
                    continue
                du, notes = val.delta(counts, Counter({ref: 1}), Counter({d: 1}))
                if du < cfg.margin:
                    continue
                v = duende or min(usable.values(), key=lambda x: x.fee(0, 2))
                p, basis = fill_probability("swap", 0, st, v, cfg, actions)
                opp = max(immediate_sell.get(d, 0), 0)
                eu = round(p * du, 2)
                add({"type": "swap_list", "kind": "publicar trueque", "venue": v.id, "ref": ref, "asset": ids[-1],
                     "give_ref": d, "price": 0, "fee": 0, "cash": 0, "dv": du, "du": round(du, 2), "immediate_du": 0,
                     "expected_du": eu, "p_fill": p, "p_basis": basis, "strategic": strategic_value(st),
                     "opportunity_cost": opp, "score": round(eu - opp + 0.1 * strategic_value(st) - cfg.asset_lock_penalty, 2),
                     "expires": tick + expiry_for(v.id), "expires_in": expiry_for(v.id),
                     "notes": notes + [f"damos {d} (pérdida {ds.loss_if_sold} P, coste de oportunidad {opp} P)"],
                     "uncertainty": f"ejecución NO garantizada ({basis})", "reason": "trueque duplicado → ausente"})
                break

    # 5. Reprecio de nuestras ofertas abiertas (sin guerra infinita) y retirada de las que ya no tienen sentido.
    #    Primero, seguridad: una oferta abierta que ahora rompería una página completa se retira YA.
    unsafe = pg.unsafe_open_offers(ours, team, counts, catalog, me["assets"])
    for c in unsafe:
        add(dict(c, blockers=[]))
    unsafe_ids = {c["offer"] for c in unsafe}
    for o in ours:
        c = classify(o)
        if not c or o.get("venue") not in venues or o.get("id") in unsafe_ids:
            continue
        kind, ref, price, asset, _ = c
        st = states.get(ref)
        age = tick - int(o.get("created_tick") or tick)
        if not st or age < cfg.min_offer_age_ticks:
            continue
        v = venues[o["venue"]]
        if kind in ("ask", "swap"):
            ev = tr.evaluate_own_open_offer(o, val, counts)  # el MISMO evaluador que usa la seguridad
            if ev["du"] is not None and ev["du"] < cfg.margin:
                add({"type": "cancel", "kind": "retirar venta" if kind == "ask" else "retirar trueque", "venue": v.id,
                     "offer": o["id"], "ref": ref, "price": price, "du": 0, "score": 5000,
                     "reason": f"reprecio: ΔU canónico {ev['du']} P < margen {cfg.margin} P ({ev['kind']})",
                     "notes": ["el valor de la copia cambió"], "uncertainty": ""})
                continue
        if kind == "ask":
            cp = competitive_sell_price(st, v, cfg, reprice_count(ref), price)
            if cp["price"] is not None and price - cp["price"] >= cfg.min_reprice_delta:
                p_old, _ = fill_probability("ask", price, st, v, cfg, actions)
                p_new, _ = fill_probability("ask", cp["price"], st, v, cfg, actions)
                gain = p_new * (cp["price"] - st.loss_if_sold) - p_old * (price - st.loss_if_sold)
                if gain > 0.5:
                    add({"type": "cancel", "kind": "repreciar venta", "venue": v.id, "offer": o["id"], "ref": ref,
                         "price": price, "du": round(gain, 2), "score": round(gain, 2),
                         "reason": f"reprecio: {price} → {cp['price']} P ({cp['reason']})",
                         "notes": [f"reprecio {reprice_count(ref) + 1}/{cfg.max_reprices_per_asset}"], "uncertainty": ""})
        elif kind == "bid" and st.best_bid and st.best_bid.price >= price:
            bp = calculate_bid_price(st, v, cfg, bid_cap + price, price, urgency)
            if bp["price"] and reprice_count(ref) < cfg.max_reprices_per_asset:
                add({"type": "cancel", "kind": "repreciar puja", "venue": v.id, "offer": o["id"], "ref": ref,
                     "price": price, "du": 0.5, "score": 0.5,
                     "reason": f"reprecio: puja rival {st.best_bid.price} P ≥ la nuestra {price} P; siguiente peldaño "
                               f"{bp['price']} P", "notes": [], "uncertainty": ""})

    out["opportunities"].sort(key=lambda o: (bool(o["blockers"]), -o["score"]))
    return out


# ------------------------------------------------------------------ informe

def _q(q):
    return f"{q.price} P ({q.venue})" if q else "—"


def intel_report(pl: dict, refs: Optional[list] = None, limit: int = 8) -> str:
    """Bloque legible: OBSERVED (libros), VERIFIED (comisiones, valor privado), ESTIMATE (mercado), HEURISTIC (fill)."""
    states = pl["states"]
    by_ref = defaultdict(list)
    for o in pl["opportunities"]:
        by_ref[o.get("ref") or next(iter(o.get("receive") or o.get("deliver") or {"?": 1}))].append(o)
    if refs is None:
        interesting = [r for r, s in states.items() if by_ref.get(r) or s.copies >= 2 or s.completes_page]
        refs = sorted(interesting, key=lambda r: (not states[r].completes_page,
                                                  -(max([o["score"] for o in by_ref.get(r, [])] or [0]))))[:limit]
    lines = ["=== MARKET INTELLIGENCE ===",
             "venues (VERIFIED /api/venues): " + "; ".join(
                 f"{k} {v['name']} {v['fee_bps'] / 100:g}% + {v['fee_per_card']} P/carta [{v['mechanism']}]"
                 + (f" (cambio anunciado {v['pending']})" if v.get("pending") else "") for k, v in pl["venues"].items())]
    for r in refs:
        s = states[r]
        lines += ["", f"{r} · {s.name} · {s.set} {s.rarity}",
                  f"  OUR VALUE (VERIFIED): " + (f"ganaría {s.gain_if_bought} P si la compro" if s.missing else
                                               f"{s.copies} copias · perdería {s.loss_if_sold} P al vender una"),
                  f"  PAGE: {'completa la página' if s.completes_page else '—'}",
                  f"  MARKET (OBSERVED): best bid {_q(s.best_bid)} · 2º {_q(s.second_bid)} · best ask {_q(s.best_ask)} "
                  f"· 2º {_q(s.second_ask)} · spread {s.spread} · {len(s.bids)} bids / {len(s.asks)} asks · "
                  f"trueques {len(s.swaps_give)} ofrecen / {len(s.swaps_want)} piden",
                  f"  MARKET VALUE (ESTIMATE): {s.market.value} P · confianza {s.market.confidence} · "
                  f"{'; '.join(s.market.evidence)} · tendencia {s.trend}",
                  f"  SCORES (HEURISTIC): demanda {s.demand} · oferta {s.supply} · escasez {s.scarcity} · liquidez {s.liquidity}"]
        ops = sorted(by_ref.get(r, []), key=lambda o: (bool(o["blockers"]), -o["score"]))
        if ops:
            b = ops[0]
            to = f" a {b['to']}" if b.get("to") else ""
            lines.append(f"  BEST ACTION: {b['kind'].upper()}{to} @ {b.get('price')} P en {b.get('venue')} · "
                         f"ΔU {b['du']} P · esperado {b.get('expected_du')} P (p {b.get('p_fill')}, {b.get('p_basis')})"
                         + (f" · BLOQUEADA: {'; '.join(b['blockers'])}" if b["blockers"] else ""))
            lines.append(f"  REASON: {b.get('reason')} · {'; '.join(b.get('notes') or [])}")
            for alt in ops[1:3]:
                lines.append(f"  ALTERNATIVE: {alt['kind']} @ {alt.get('price')} P en {alt.get('venue')} · esperado "
                             f"{alt.get('expected_du')} P")
        else:
            lines.append("  BEST ACTION: ninguna con ΔU ≥ margen")
    if pl.get("arbitrage"):
        lines += ["", "ARBITRAJE (informativo, no se ejecuta): " + "; ".join(
            f"{a['ref']} comprar {a['buy']} / vender {a['sell']} (+{a['gross']} P bruto)" for a in pl["arbitrage"])]
    return "\n".join(lines)
