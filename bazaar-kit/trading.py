"""Comercio entre equipos sin llamadas a la API: valoración marginal, comisiones, ofertas, recursos y reconciliación.

Hechos verificados con el servidor (tick 94, Team 15), no supuestos:
  * collection_value = suma por referencia de book * multiplicador * copy_marginals[k] para cada copia k (0, 1, 2+).
    Modelo 412.18 frente a 412.2 del servidor, sin páginas completas.
  * your_value de un activo poseído = marginal de la ÚLTIMA copia de esa referencia (todas las copias muestran lo mismo);
    sumarlos no da collection_value.  /api/me/value = marginal de la SIGUIENTE copia.
  * Comisión de El Rastro = ceil(precio * fee_bps / 10000) + fee_per_card * cartas movidas, y la paga quien acepta:
    cuatro liquidaciones (18->3, 40->4, 22->3, 9->2) cuadran al céntimo con nuestro efectivo como aceptantes.
  * Bono de página (verificado en el tick 129): /api/me/value de LAT-10, la carta que completa La Latina, es 177.1 =
    91.0 (rara, 70 x 1.3) + 0.25 x 344.5, la suma del valor unitario de las 10 cartas de página. Es decir, el valor
    marginal de la API YA incluye el bono; aquí se modela igual y se suma una sola vez por página completa.
Sin verificar: la base del master_bonus (épica y legendaria encima de una página completa). Una operación que
cambiaría un master se marca como incierta y no se ejecuta sola.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

OPEN_STATES = ("open", "queued", "accepted", "pending")


# ------------------------------------------------------------------ valoración

class Valuation:
    def __init__(self, catalog: dict, affinity: dict):
        self.marg = list((catalog.get("values") or {}).get("copy_marginals") or [])
        self.page_bonus = (catalog.get("values") or {}).get("page_bonus")
        self.cards, self.pages = {}, {}
        for s in catalog.get("sets", []):
            for c in s.get("cards", []):
                self.cards[c["id"]] = {**c, "set": s["id"], "released": bool(s.get("released"))}
            self.pages[s["id"]] = [c["id"] for c in s.get("cards", []) if c.get("page") and not c.get("hidden")]
        self.affinity = affinity or {}

    def unit(self, ref: str) -> Optional[float]:
        c = self.cards.get(ref)
        mult = self.affinity.get(c["set"]) if c else None
        return None if c is None or mult is None or c.get("book") is None else c["book"] * mult

    def m(self, k: int) -> float:
        return self.marg[min(k, len(self.marg) - 1)] if self.marg else 0.0

    def copy_value(self, ref: str, k: int) -> float:
        """Valor de la copia número k (0 = primera) de una referencia."""
        u = self.unit(ref)
        return 0.0 if u is None else u * self.m(k)

    def page_sum(self, set_id: str) -> float:
        return sum(self.unit(r) or 0.0 for r in self.pages.get(set_id, []))

    def total(self, counts: Counter) -> float:
        cards = sum(self.copy_value(r, k) for r, n in counts.items() for k in range(n))
        bonus = sum((self.page_bonus or 0.0) * self.page_sum(sid) for sid in self.complete_pages(counts))
        return cards + bonus

    def masters(self, counts: Counter) -> set:
        out = set()
        for sid in self.complete_pages(counts):
            extra = [c for c, x in self.cards.items() if x["set"] == sid and not x.get("page") and not x.get("hidden")]
            if extra and all(counts.get(c, 0) > 0 for c in extra):
                out.add(sid)
        return out

    def complete_pages(self, counts: Counter) -> set:
        return {s for s, req in self.pages.items() if req and all(counts.get(r, 0) > 0 for r in req)}

    def delta(self, counts: Counter, add: Counter, remove: Counter) -> tuple[float, list]:
        """V(después) - V(antes), copia a copia (lotes y repeticiones incluidos). Avisos de páginas afectadas."""
        after = Counter(counts)
        after.update(add)
        after.subtract(remove)
        if any(v < 0 for v in after.values()):
            raise ValueError("se entregarían copias que no tenemos")
        before_p, after_p = self.complete_pages(counts), self.complete_pages(+after)
        notes = [f"completa la página {s}: incluye bono {self.page_bonus * self.page_sum(s):.1f} P (una vez)"
                 for s in after_p - before_p]
        notes += [f"ROMPERÍA la página {s}: pierde el bono {self.page_bonus * self.page_sum(s):.1f} P"
                  for s in before_p - after_p]
        if self.masters(counts) != self.masters(+after):
            notes.append("CAMBIA un master: bono master no verificado")
        return round(self.total(+after) - self.total(counts), 2), notes

    def next_copy(self, counts: Counter, ref: str) -> float:
        """Lo mismo que /api/me/value: valor de UNA copia más, con el bono si completa una página."""
        return self.delta(counts, Counter({ref: 1}), Counter())[0]

    def calibrate(self, counts: Counter, server_value: Optional[float]) -> tuple[bool, float]:
        model = round(self.total(counts), 2)
        if server_value is None:
            return False, model
        return abs(model - float(server_value)) <= 0.5, model


def counts_of(assets: list) -> Counter:
    return Counter(a["ref"] for a in assets if a.get("kind") == "card")


# ------------------------------------------------------------------ comisiones

def fee(price: int, n_cards: int, venue: dict) -> int:
    bps, per = venue.get("fee_bps"), venue.get("fee_per_card")
    if type(bps) is not int or type(per) is not int or bps < 0 or per < 0:
        raise ValueError("comisiones del mercado desconocidas")
    return math.ceil(price * bps / 10000) + per * n_cards


# ------------------------------------------------------------------ ofertas

@dataclass
class Proposal:
    offer_id: int
    venue: Optional[str]
    maker: str
    cash_in: int                 # efectivo que recibimos si aceptamos
    cash_out: int                # efectivo que entregamos (sin comisión)
    receive: Counter             # referencias que recibimos
    deliver: Counter             # referencias que entregamos
    deliver_assets: list         # ids concretos de nuestras copias que entregaríamos
    n_cards: int
    expires_tick: Optional[int]
    source: str                  # "tablón" | "dirigida" | "conversación"
    price: int = 0               # base de la comisión (el efectivo que cambia de manos)


def _side(s) -> Optional[tuple]:
    if not isinstance(s, dict) or set(s) - {"cash", "assets", "types", "cards"}:
        return None
    cash = s.get("cash", 0)
    assets, types, cards = s.get("assets") or [], s.get("types") or [], s.get("cards") or []
    if type(cash) is not int or cash < 0 or not all(isinstance(x, list) for x in (assets, types, cards)):
        return None
    refs = []
    for t in list(types) + [f"card:{c}" for c in cards]:
        if not isinstance(t, str) or not t.startswith("card:"):
            return None  # sobres u otros tipos: fuera del alcance
        refs.append(t[5:])
    return cash, assets, refs


def parse_offer(o: dict, *, team: str, tick: int, own_venue: Optional[str], my_assets: list, locked: set,
                min_ticks_left: int = 1, max_cards: int = 4) -> tuple[Optional[Proposal], str]:
    """Lo que pasaría si ACEPTAMOS la oferta `o`. (None, motivo) si no es aceptable por estructura."""
    if o.get("maker") == team:
        return None, "es nuestra"
    if o.get("status") != "open":
        return None, f"estado {o.get('status')}"
    if o.get("to") not in (None, team):
        return None, "dirigida a otro equipo"
    if own_venue and o.get("venue") == own_venue:
        return None, "mercado propio"
    if o.get("expires_tick") is not None and o["expires_tick"] <= tick + min_ticks_left:
        return None, "caduca ya"
    g, w = _side(o.get("give")), _side(o.get("want"))
    if g is None or w is None:
        return None, "estructura no reconocida o con extras"
    g_cash, g_assets, g_refs = g
    w_cash, w_assets, w_refs = w
    if g_refs:
        return None, "nos ofrece tipos genéricos, no activos concretos"
    receive = Counter()
    for a in g_assets:
        if not isinstance(a, dict) or a.get("kind") != "card" or type(a.get("id")) is not int or not a.get("ref"):
            return None, "activo ofrecido sin identificar"
        receive[a["ref"]] += 1
    mine = {a["id"]: a for a in my_assets if a.get("kind") == "card"}
    deliver_ids = []
    for a in w_assets:  # nos pide copias concretas
        aid = a.get("id") if isinstance(a, dict) else a
        if aid not in mine or aid in locked:
            return None, f"pide el activo {aid}, que no tenemos libre"
        deliver_ids.append(aid)
    deliver = Counter(mine[i]["ref"] for i in deliver_ids)
    pool = {}
    for a in sorted(mine.values(), key=lambda a: a["id"]):
        if a["id"] not in locked and a["id"] not in deliver_ids:
            pool.setdefault(a["ref"], []).append(a["id"])
    for ref in w_refs:  # nos pide "cualquier copia": entregamos la última (la de menor valor marginal)
        if not pool.get(ref):
            return None, f"pide {ref}, que no tenemos libre"
        deliver_ids.append(pool[ref].pop())
        deliver[ref] += 1
    n = sum(receive.values()) + sum(deliver.values())
    if n == 0 or n > max_cards:
        return None, f"{n} cartas: fuera del tamaño de lote admitido"
    if g_cash and w_cash:
        return None, "efectivo en ambos lados"
    source = "conversación" if o.get("thread") else ("dirigida" if o.get("to") == team else "tablón")
    return Proposal(o["id"], o.get("venue"), o.get("maker"), g_cash, w_cash, receive, deliver, deliver_ids, n,
                    o.get("expires_tick"), source, price=max(g_cash, w_cash)), ""


@dataclass
class Evaluation:
    du: float            # excedente económico estimado
    dv: float            # variación del valor de colección (modelo verificado)
    cash: int            # variación de efectivo, comisión incluida
    fee: int
    notes: list
    blockers: list


def evaluate(p: Proposal, val: Valuation, counts: Counter, venue: dict, *, keep_one: bool = True,
             allow_last_copy: frozenset = frozenset()) -> Evaluation:
    """ΔU = efectivo recibido - entregado - comisión propia + V(después) - V(antes). Aceptamos: pagamos la comisión."""
    blockers, notes = [], []
    f = fee(p.price, p.n_cards, venue) if p.venue else 0
    dv, page_notes = val.delta(counts, p.receive, p.deliver)
    notes += page_notes
    if any("master" in n for n in page_notes):
        blockers.append("cambia un master: valor no determinable")
    for ref, k in p.deliver.items():
        left = counts.get(ref, 0) + p.receive.get(ref, 0) - k
        if keep_one and left < 1 and ref not in allow_last_copy:
            blockers.append(f"entregaría la última copia de {ref}")
        if val.unit(ref) is None:
            blockers.append(f"{ref}: sin valor conocido")
    for ref in p.receive:
        if val.unit(ref) is None:
            blockers.append(f"{ref}: sin valor conocido")
    cash = p.cash_in - p.cash_out - f
    return Evaluation(round(cash + dv, 2), dv, cash, f, notes, blockers)


# ------------------------------------------------------------------ recursos comprometidos

@dataclass
class Resources:
    reserved_cash: int = 0           # efectivo que pueden llevarse nuestras pujas abiertas
    locked_assets: set = field(default_factory=set)
    selling_refs: Counter = field(default_factory=Counter)
    buying_refs: Counter = field(default_factory=Counter)
    pending_cash: int = 0            # aceptaciones enviadas sin liquidar (este agente y el de vendedores)
    open_offers: list = field(default_factory=list)


def resources(my_offers: list, team: str, pendings: list) -> Resources:
    """Solo cuentan las ofertas ABIERTAS que el servidor devuelve ahora: una oferta caducada, cancelada o rechazada
    libera su reserva sola (el contador acumulado de la versión anterior no la liberaba nunca)."""
    r = Resources()
    for o in my_offers:
        if o.get("maker") != team or o.get("status") not in OPEN_STATES:
            continue
        r.open_offers.append(o)
        g, w = o.get("give") or {}, o.get("want") or {}
        r.reserved_cash += max(0, int(g.get("cash") or 0))  # como maker no pagamos comisión: paga el aceptante
        for a in g.get("assets") or []:
            aid = a["id"] if isinstance(a, dict) else a
            r.locked_assets.add(aid)
            if isinstance(a, dict) and a.get("ref"):
                r.selling_refs[a["ref"]] += 1
        for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]:
            if isinstance(t, str) and t.startswith("card:"):
                r.buying_refs[t[5:]] += 1
    for p in pendings:
        r.pending_cash += max(0, int(p.get("cost") or p.get("price") or 0))
        for aid in p.get("assets") or []:
            r.locked_assets.add(aid)
    return r


def free_cash(cash: int, reserve: int, res: Resources) -> int:
    return cash - reserve - res.reserved_cash - res.pending_cash


# ------------------------------------------------------------------ plan de oportunidades

@dataclass
class Config:
    reserve: int = 100
    margin: float = 2.0               # excedente mínimo por operación
    per_card: int = 40                # coste máximo por carta comprada (incluida comisión)
    max_spend: int = 100              # gasto confirmado máximo de la sesión (separado de reservas)
    listing_ticks: int = 8
    fill_prior: float = 0.3           # SUPUESTO: probabilidad de que una publicación se ejecute (sin datos propios)
    keep_one: bool = True
    allow_last_copy: frozenset = frozenset()
    min_ticks_left: int = 1


def idem_key(action: dict) -> str:
    core = {k: action.get(k) for k in ("type", "offer", "asset", "ref", "price", "give", "want")}
    return hashlib.sha1(json.dumps(core, sort_keys=True, default=list).encode()).hexdigest()[:16]


def plan(snap: dict, cfg: Config, *, pendings: list = (), spent: int = 0) -> dict:
    """Oportunidades ordenadas por excedente esperado (excedente x probabilidad de ejecución)."""
    me, catalog, tick = snap["me"], snap["catalog"], snap["clock"]["tick"]
    team = me["id"]
    val = Valuation(catalog, me.get("affinity") or {})
    counts = counts_of(me["assets"])
    # collection_value incluye activos que no son cartas (sobres sin abrir) con su your_value (verificado, tick 158)
    other = round(sum(float(a.get("your_value") or 0) for a in me["assets"] if a.get("kind") != "card"), 2)
    server = me.get("collection_value")
    ok, model = val.calibrate(counts, None if server is None else float(server) - other)
    model = round(model + other, 2)
    venue = next((v for v in (snap.get("venues") or {}).get("venues", []) if v.get("venue") == "rastro"), None)
    own_venue = (me.get("venue") or {}).get("venue") if isinstance(me.get("venue"), dict) else me.get("venue")
    my_offers = (snap.get("offers") or {}).get("offers", [])
    res = resources(my_offers, team, list(pendings))
    out = {"tick": tick, "team": team, "cash": me["cash"], "model_value": model, "reserve": cfg.reserve,
           "server_value": me.get("collection_value"), "valuation_verified": ok, "opportunities": [],
           "rejected": [], "notes": [], "free_cash": free_cash(me["cash"], cfg.reserve, res),
           "reserved_cash": res.reserved_cash, "pending_cash": res.pending_cash, "spent_session": spent,
           "budget_left": cfg.max_spend - spent, "open_offers": len(res.open_offers)}
    if not ok:
        out["notes"].append(f"El modelo de valor ({model}) no cuadra con collection_value ({me.get('collection_value')}): "
                            "no se ejecuta nada")
    if not venue or venue.get("status") != "open":
        out["notes"].append("El Rastro no está abierto")
        return out
    if venue.get("pending_fee"):
        out["notes"].append("Cambio de comisión anunciado: no se opera hasta que entre en vigor")
        return out
    buy_cap = max(0, min(out["free_cash"], out["budget_left"]))
    candidates = list((snap.get("board") or {}).get("offers", []))
    seen = {o["id"] for o in candidates}
    candidates += [o for o in my_offers if o.get("id") not in seen]  # ofertas dirigidas a nosotros
    for th in snap.get("team_threads") or []:
        candidates += [o for o in th.get("standing_offers") or [] if o.get("id") not in seen]
    best_ask, best_bid, rival_bid = {}, {}, {}
    for o in candidates:  # pujas de otros por cartas que NO tenemos: compiten con las nuestras
        g, w = o.get("give") or {}, o.get("want") or {}
        types = w.get("types") or []
        if o.get("maker") != team and o.get("status") == "open" and g.get("cash") and not g.get("assets") \
                and len(types) == 1 and str(types[0]).startswith("card:"):
            ref = types[0][5:]
            rival_bid[ref] = max(rival_bid.get(ref, 0), g["cash"])
    for o in candidates:
        p, why = parse_offer(o, team=team, tick=tick, own_venue=own_venue, my_assets=me["assets"],
                             locked=res.locked_assets, min_ticks_left=cfg.min_ticks_left)
        if p is None:
            if o.get("maker") != team:
                out["rejected"].append({"offer": o.get("id"), "why": why})
            continue
        if p.cash_out and len(p.receive) == 1 and not p.deliver and sum(p.receive.values()) == 1:
            ref = next(iter(p.receive))
            best_ask[ref] = min(best_ask.get(ref, 10 ** 9), p.cash_out)
        if p.cash_in and len(p.deliver) == 1 and not p.receive and sum(p.deliver.values()) == 1:
            ref = next(iter(p.deliver))
            best_bid[ref] = max(best_bid.get(ref, 0), p.cash_in)
        e = evaluate(p, val, counts, venue, keep_one=cfg.keep_one, allow_last_copy=cfg.allow_last_copy)
        if p.cash_out:
            per = (p.cash_out + e.fee) / max(1, sum(p.receive.values()))
            if p.cash_out + e.fee > buy_cap:
                e.blockers.append(f"coste {p.cash_out + e.fee} P > capacidad {buy_cap} P (reserva, reservas y gasto)")
            if per > cfg.per_card:
                e.blockers.append(f"{per:.1f} P por carta > límite {cfg.per_card} P")
        if not ok:
            e.blockers.append("valoración no verificada")
        if e.du < cfg.margin:
            continue
        kind = "vender" if p.cash_in and not p.receive else "comprar" if p.cash_out and not p.deliver else "intercambio"
        out["opportunities"].append({
            "type": "accept", "kind": kind, "offer": p.offer_id, "source": p.source, "maker": p.maker,
            "receive": dict(p.receive), "deliver": dict(p.deliver), "assets": p.deliver_assets,
            "price": p.price, "fee": e.fee, "cash": e.cash, "dv": e.dv, "du": e.du, "p_fill": 1.0,
            "score": e.du, "expires": p.expires_tick, "notes": e.notes, "blockers": e.blockers,
            "uncertainty": "ejecución inmediata si nadie la toma antes"})
    # Publicar duplicados: una copia por referencia y una sola publicación por activo/referencia.
    for ref, n in counts.items():
        if n < 2 or res.selling_refs.get(ref) or val.unit(ref) is None:
            continue
        loss = val.copy_value(ref, n - 1)
        if best_bid.get(ref):
            continue  # ya hay demanda ejecutable: se acepta en lugar de publicar
        copies = [a["id"] for a in sorted(me["assets"], key=lambda a: a["id"]) if a.get("ref") == ref
                  and a["id"] not in res.locked_assets]
        if not copies:
            continue
        comp = best_ask.get(ref)
        floor_ = math.ceil(loss + cfg.margin)
        price = max(floor_, comp - 1) if comp else max(floor_, math.ceil(val.cards[ref]["book"] * 0.9))
        basis = f"1 P por debajo de la venta comparable más barata ({comp} P)" if comp else \
            "sin comparables: 90 % del valor de catálogo (heurística)"
        du = price - loss  # como maker no pagamos comisión: la paga el comprador
        out["opportunities"].append({
            "type": "list", "kind": "publicar venta", "ref": ref, "asset": copies[-1], "price": price, "fee": 0,
            "cash": price, "dv": round(-loss, 2), "du": round(du, 2), "p_fill": cfg.fill_prior,
            "score": round(du * cfg.fill_prior, 2), "expires": tick + cfg.listing_ticks,
            "notes": [basis], "blockers": [] if ok else ["valoración no verificada"],
            "uncertainty": f"ejecución NO garantizada; probabilidad {cfg.fill_prior:.0%} es un supuesto"})
    # Pujar por cartas ausentes de páginas incompletas (sin bono no verificado, sin duplicar pujas).
    for s, req in val.pages.items():
        if not val.cards.get(req[0], {}).get("released") if req else True:
            continue
        missing = [r for r in req if counts.get(r, 0) == 0]
        for ref in missing:
            if res.buying_refs.get(ref) or val.unit(ref) is None:
                continue
            gain, notes = val.delta(counts, Counter({ref: 1}), Counter())
            cap = math.floor(min(gain - cfg.margin, cfg.per_card, buy_cap))
            if cap < 1:
                out["rejected"].append({"ref": ref, "why": f"capacidad {buy_cap} P / límite {cfg.per_card} P / valor "
                                                          f"{gain} P no permiten pujar"})
                continue
            comp = best_ask.get(ref)
            price = min(cap, comp - 1 if comp else math.floor(val.cards[ref]["book"] * 0.85))
            rival, p_fill = rival_bid.get(ref, 0), cfg.fill_prior
            extra = []
            if rival >= price:
                if rival + 1 <= cap:
                    price = rival + 1
                    extra.append(f"supera en 1 P la mejor puja rival ({rival} P)")
                else:
                    p_fill = cfg.fill_prior * 0.2
                    extra.append(f"hay una puja rival mayor ({rival} P) que no podemos superar: ejecución poco probable")
            if price < 1:
                continue
            out["opportunities"].append({
                "type": "bid", "kind": "publicar compra", "ref": ref, "price": price, "fee": 0, "cash": -price,
                "dv": gain, "du": round(gain - price, 2), "p_fill": p_fill,
                "score": round((gain - price) * p_fill, 2), "expires": tick + cfg.listing_ticks,
                "notes": notes + [f"falta en la página {s} ({len(missing)} ausentes)"] + extra,
                "blockers": [] if ok else ["valoración no verificada"],
                "uncertainty": f"ejecución NO garantizada; probabilidad {p_fill:.0%} es un supuesto"})
    for o in out["opportunities"]:
        o["key"] = idem_key(o)
    out["opportunities"].sort(key=lambda o: (bool(o["blockers"]), -o["score"], -o["du"]))
    return out


# ------------------------------------------------------------------ evidencia del servidor y reconciliación

def settlements_for(feed_events: list, team: str) -> list:
    """Liquidaciones públicas donde participamos, con flujos desde nuestro punto de vista."""
    out = []
    for e in feed_events:
        if e.get("type") != "settlement":
            continue
        p = e.get("payload") or {}
        if team not in (p.get("parties") or []):
            continue
        items_in = [i for i in p.get("items") or [] if i.get("to") == team]
        items_out = [i for i in p.get("items") or [] if i.get("frm") == team]
        price = p.get("price") or 0
        we_pay = bool(items_in) and not items_out  # compramos con efectivo
        out.append({"settlement": p.get("settlement"), "tick": p.get("tick", e.get("tick")), "venue": p.get("venue"),
                    "counterparty": next((x for x in p.get("parties") or [] if x != team), None),
                    "persona": p.get("persona"), "price": price, "fee": p.get("fee") or 0,
                    "in": [(i.get("kind"), i.get("ref"), i.get("id")) for i in items_in],
                    "out": [(i.get("kind"), i.get("ref"), i.get("id")) for i in items_out],
                    "cash_direction": "pagamos" if we_pay else ("cobramos" if items_out and not items_in else "mixto")})
    return out


def settled_offer(thread: dict) -> Optional[dict]:
    for m in reversed(thread.get("messages") or []):
        o = m.get("offer") or {}
        if o.get("status") == "settled":
            return o
    return None


def classify(settlement: dict, own_actions: list, starter_accepts: list) -> str:
    """propia (mercado) / propia (vendedores) / origen desconocido. Sin evidencia local, nunca 'propia'."""
    ids_in = {x[2] for x in settlement["in"]}
    ids_out = {x[2] for x in settlement["out"]}
    for a in own_actions:
        if a.get("status") in ("submitted", "settled") and (
                a.get("asset") in ids_in | ids_out or set(a.get("assets") or []) & (ids_in | ids_out)
                or (a.get("settlement") == settlement["settlement"])):
            return "propia (agente de mercado)"
    for d in starter_accepts:
        if d.get("tick") is not None and settlement["persona"] and abs(int(d["tick"]) - int(settlement["tick"])) <= 2 \
                and d.get("price") == settlement["price"]:
            return "propia (agente de vendedores)"
    return "origen desconocido (no registrada por los agentes de este ordenador)"


def journal_corrections(outcomes: list, threads: dict, existing: list) -> list:
    """Correcciones trazables (no se reescribe el original) cuando el precio anotado difiere del liquidado."""
    done = {(c.get("thread"), c.get("field")) for c in existing}
    out = []
    for o in outcomes:
        t = threads.get(o.get("thread"))
        if not t or o.get("status") != "deal":
            continue
        s = settled_offer(t)
        if not s:
            continue
        paid = (s.get("want") or {}).get("cash") if s.get("maker") == t.get("with") else (s.get("give") or {}).get("cash")
        if paid is not None and paid != o.get("close_price") and (o["thread"], "close_price") not in done:
            ours = s.get("maker") != t.get("with")
            out.append({"thread": o["thread"], "item": o.get("item"), "field": "close_price", "old": o.get("close_price"),
                        "new": paid, "evidence": f"oferta {s.get('id')} de {s.get('maker')} con estado settled",
                        "accepted_by": "el dealer aceptó nuestra oferta" if ours else
                        "alguien con nuestra clave aceptó la oferta del dealer (no consta en este registro)"})
            done.add((o["thread"], "close_price"))
    return out


def unexplained_activity(settlements: list, threads: list, own_actions: list, starter_accepts: list,
                         starter_threads: set, since_tick: int) -> list:
    """Actividad con nuestra clave que ningún agente de este ordenador registró: posible cliente concurrente.
    El bloqueo local no ve otros ordenadores ni otros clientes con la misma clave; esto sí."""
    flags = []
    for s in settlements:
        if s["tick"] >= since_tick and classify(s, own_actions, starter_accepts).startswith("origen desconocido"):
            flags.append(f"liquidación {s['settlement']} (tick {s['tick']}, {s['price']} P con {s['counterparty']})")
    for t in threads:
        if t.get("created_tick", 0) >= since_tick and t.get("kind") == "persona" and t["id"] not in starter_threads:
            flags.append(f"conversación #{t['id']} con {t.get('with')} abierta en el tick {t.get('created_tick')}")
    return flags


# ------------------------------------------------------------------ sobres

def pack_value(val: Valuation, counts: Counter, pack: dict, draws: int = 4000, seed: int = 7) -> tuple[float, str]:
    """Valor incremental esperado de abrir el sobre AHORA (copia a copia, con repeticiones dentro del sobre y bonos).
    SUPUESTO: dentro de cada rareza, cualquier carta de los barrios publicados es equiprobable; se ignoran las tiradas
    agotadas. Monte Carlo con semilla fija (reproducible)."""
    import random
    rng = random.Random(seed)
    pools = {}
    for slot in pack.get("slots", []):
        for r in slot:
            pools.setdefault(r, [c for c, x in val.cards.items() if x["released"] and x["rarity"] == r
                                 and not x.get("hidden")])
    base, acc = val.total(counts), 0.0
    for _ in range(draws):
        got = Counter()
        for slot in pack.get("slots", []):
            rarity = rng.choices(list(slot), weights=list(slot.values()))[0]
            if pools.get(rarity):
                got[rng.choice(pools[rarity])] += 1
        after = Counter(counts)
        after.update(got)
        acc += val.total(after) - base
    return round(acc / draws, 1), "cartas equiprobables dentro de cada rareza; tiradas agotadas ignoradas"


def pack_analysis(val: Valuation, counts: Counter, pack: dict, market_value: Optional[dict] = None,
                  p_sale: float = 0.3, draws: int = 4000, seed: int = 7) -> dict:
    """Monte Carlo del sobre con el inventario ACTUAL, separando:
    - RAW_COLLECTION_EV: ΔV de colección esperado (igual que pack_value);
    - STRATEGIC_EV: RAW + valor revendible esperado de los duplicados = P(venta) × max(0, mercado − valor de la copia),
      con P(venta) HEURISTIC (`p_sale`) y el valor de mercado de market_intel (sin dato de mercado, 0: no se inventa);
    - P(completar al menos una página nueva) y P(al menos un duplicado).
    SUPUESTO documentado: cartas equiprobables dentro de cada rareza; tiradas restantes NO verificadas → ignoradas."""
    import random
    rng = random.Random(seed)
    market_value = market_value or {}
    pools = {}
    for slot in pack.get("slots", []):
        for r in slot:
            pools.setdefault(r, [c for c, x in val.cards.items() if x["released"] and x["rarity"] == r
                                 and not x.get("hidden")])
    base, pages0 = val.total(counts), val.complete_pages(counts)
    raw = strat = 0.0
    page_hits = dup_hits = 0
    for _ in range(draws):
        got = Counter()
        for slot in pack.get("slots", []):
            rarity = rng.choices(list(slot), weights=list(slot.values()))[0]
            if pools.get(rarity):
                got[rng.choice(pools[rarity])] += 1
        after = Counter(counts)
        after.update(got)
        d = val.total(after) - base
        resale, dup = 0.0, False
        for ref, k in got.items():
            for j in range(k):
                n_before = counts.get(ref, 0) + j
                if n_before >= 1:
                    dup = True
                    resale += p_sale * max(0.0, (market_value.get(ref) or 0.0) - val.copy_value(ref, n_before))
        raw += d
        strat += d + resale
        page_hits += bool(val.complete_pages(after) - pages0)
        dup_hits += dup
    return {"raw_collection_ev": round(raw / draws, 2), "strategic_ev": round(strat / draws, 2),
            "p_new_page": round(page_hits / draws, 3), "p_duplicate": round(dup_hits / draws, 3),
            "assumptions": "cartas equiprobables dentro de cada rareza; tiradas restantes no verificadas (ignoradas); "
                           f"P(venta de un duplicado) {p_sale} HEURISTIC"}


# ------------------------------------------------------------------ caducidad de publicaciones

def expiry_ratio(observations: list, tick_seconds: float) -> tuple[float, str]:
    """Pedido/efectivo observado con este ritmo de ticks. Evidencia (60 s/tick): 4->1, 8->2, 20->5 ticks."""
    rs = sorted(o["requested"] / o["effective"] for o in observations
                if o.get("effective") and o.get("tick_seconds") == tick_seconds)
    if rs:
        return rs[len(rs) // 2], f"mediana de {len(rs)} publicaciones propias a {tick_seconds:.0f} s/tick"
    return max(1.0, tick_seconds / 15.0), (f"HIPÓTESIS sin observaciones a {tick_seconds:.0f} s/tick: el servidor parece "
                                           "contar expires_in_ticks en ticks de 15 s")


def listing_request(target_ticks: int, ratio: float) -> int:
    return max(1, math.ceil(target_ticks * ratio))


# ------------------------------------------------------------------ ofertas propias que violan la política

def _wanted_refs(side: dict) -> Counter:
    out = Counter()
    for t in list(side.get("types") or []) + [f"card:{c}" for c in side.get("cards") or []]:
        if isinstance(t, str) and t.startswith("card:"):
            out[t[5:]] += 1
    for a in side.get("assets") or []:
        if isinstance(a, dict) and a.get("ref"):
            out[a["ref"]] += 1
    return out


def evaluate_own_open_offer(o: dict, val: Valuation, counts: Counter) -> dict:
    """EVALUADOR CANÓNICO de una oferta NUESTRA (somos maker) si alguien la acepta: lo que cobramos y pagamos, las
    cartas que entregamos (`give`) Y las que recibimos (`want`), y ΔU = efectivo + V(después) − V(antes).
    Como maker no pagamos comisión (la paga quien acepta). Venta, puja y trueque usan exactamente esta cuenta:
    publicación, seguridad y cancelación no pueden discrepar sobre el mismo trueque."""
    g, w = o.get("give") or {}, o.get("want") or {}
    deliver = Counter(a["ref"] for a in g.get("assets") or [] if isinstance(a, dict) and a.get("ref"))
    receive = _wanted_refs(w)
    cash_in, cash_out = int(w.get("cash") or 0), int(g.get("cash") or 0)
    kind = ("trueque" if deliver and receive else "venta" if deliver else "puja" if receive else "otra")
    try:
        dv, notes = val.delta(counts, receive, deliver)
    except ValueError:
        dv, notes = None, ["entrega copias que ya no tenemos"]
    unknown = [r for r in list(receive) + list(deliver) if val.unit(r) is None]
    du = None if dv is None or unknown else round(cash_in - cash_out + dv, 2)
    return {"offer": o.get("id"), "kind": kind, "cash_in": cash_in, "cash_out": cash_out, "fee": 0,
            "receive": receive, "deliver": deliver, "dv": dv, "du": du, "notes": notes, "unknown": unknown}


def unsafe_own_offers(my_offers: list, team: str, val: Valuation, counts: Counter) -> list:
    """Publicaciones nuestras abiertas que venden la última copia, tienen ΔU < 0 si se llenan (con el evaluador
    canónico: un trueque cuenta la carta que RECIBIMOS) o repiten el mismo activo en varias ofertas.
    Candidatas a cancelar (nunca se cancelan solas: ver --cancel-unsafe)."""
    out, seen = [], {}
    for o in sorted(my_offers, key=lambda o: o.get("id", 0)):
        if o.get("maker") != team or o.get("status") != "open":
            continue
        g = o.get("give") or {}
        assets = [a for a in g.get("assets") or [] if isinstance(a, dict)]
        if not assets or g.get("cash"):
            continue
        ev = evaluate_own_open_offer(o, val, counts)
        why = []
        for a in assets:
            if a["id"] in seen:
                why.append(f"{a['ref']} (activo {a['id']}) ya está en la oferta {seen[a['id']]}")
            seen.setdefault(a["id"], o["id"])
        for ref, k in ev["deliver"].items():
            if counts.get(ref, 0) - k + ev["receive"].get(ref, 0) < 1:
                why.append(f"vende la última copia de {ref}")
        if ev["du"] is not None and ev["du"] < 0:
            got = f"{ev['cash_in']} P" + (f" + {dict(ev['receive'])}" if ev["receive"] else "")
            why.append(f"ΔU {ev['du']} P si se llena ({ev['kind']}: recibe {got}, Δvalor {ev['dv']} P)")
        why += [n for n in ev["notes"] if "ROMPERÍA" in n]
        if why:
            out.append({"type": "cancel", "kind": "cancelar publicación propia", "offer": o["id"],
                        "ref": ",".join(ev["deliver"]), "price": ev["cash_in"], "dv_if_filled": ev["dv"],
                        "du_if_filled": ev["du"], "why": why, "expires": o.get("expires_tick")})
    return out
