"""Campañas de negociación directa con otros equipos. Lógica pura (sin red); el coordinador envía las acciones.

Protocolo verificado (RULES.md, bazaar_sdk.py y el hilo real #135 con t04):
  * POST /api/threads {"with": "<id de equipo>", "venue": "rastro", "topic": {...pequeño}} abre la conversación.
  * Un mensaje lleva la propuesta en `offer` = {"give": {...}, "want": {...}} (NO en `price`, que es de vendedores).
    La contraparte la acepta con POST /api/offers/{id}/accept; paga la comisión quien acepta.
  * La conversación termina con un trato o tras 200 mensajes. No hay "final": true entre equipos.
Identidades: solo IDs de equipo que el servidor publica (leaderboard; `maker`/`actor` en /api/feed y en ofertas
dirigidas a nosotros; equipos que nos abrieron conversación). Los alias del tablón ("m3950d43b") nunca se usan para
contactar ni se intentan traducir. Una oferta pública es evidencia de intención, no del inventario de nadie.
Sin verificar: si una propuesta nueva en un hilo cancela la anterior; por eso se comprueba y se cancela explícitamente.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

import trading as tr

ACTIVE = ("detectada", "contactada", "propuesta", "esperando", "contraoferta", "aceptada", "ambigua")
BINDING = ("propuesta", "esperando", "contraoferta", "aceptada", "ambigua")


@dataclass
class CampaignConfig:
    kinds: tuple = ("collect", "sell", "swap")
    ticks: int = 30                  # duración de la campaña
    budget: int = 60                 # efectivo máximo que la campaña puede comprometer o gastar
    max_proposals: int = 3           # propuestas nuestras por conversación
    negotiation_ticks: int = 6       # por negociación, desde el contacto
    max_conversations: int = 2       # negociaciones de campaña activas a la vez
    reply_ticks: int = 2             # silencio tolerado antes de considerar caducada nuestra propuesta abierta
    evidence_ticks: int = 30         # antigüedad máxima de una oferta del feed como evidencia
    margin: float = 2.0
    buy_open_frac: float = 0.85      # primera oferta de compra respecto al precio publicado por ellos
    sell_markup: float = 1.15        # primera petición de venta respecto a su puja publicada
    concession: float = 0.5          # cada concesión recorre esta fracción de la distancia restante
    contact_prior: float = 0.3       # SUPUESTO: probabilidad de respuesta (no hay datos propios todavía)
    min_fit: float = 0.6             # no contactar si nuestra reserva queda a menos del 60 % de su precio público
                                     # (compra) o su puja a menos del 60 % de nuestro mínimo (venta): sería extremo


# ------------------------------------------------------------------ descubrimiento

def valid_team_ids(s: dict) -> set:
    ids = {t.get("team") for t in (s.get("leaderboard") or {}).get("teams", [])}
    return {i for i in ids if isinstance(i, str)} - {s["me"]["id"]}


def _single(o: dict):
    """('sells', ref, precio, activo) | ('buys', ref, precio, None) para ofertas de una carta contra efectivo."""
    g, w = o.get("give") or {}, o.get("want") or {}
    ga, wt = g.get("assets") or [], (w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]
    if len(ga) == 1 and not g.get("cash") and not g.get("types") and w.get("cash") and not w.get("assets") and not wt:
        a = ga[0]
        if isinstance(a, dict) and a.get("kind") == "card" and a.get("ref"):
            return "sells", a["ref"], int(w["cash"]), a.get("id")
    if g.get("cash") and not ga and len(wt) == 1 and str(wt[0]).startswith("card:") and not w.get("cash"):
        return "buys", wt[0][5:], int(g["cash"]), None
    return None


def evidence(s: dict, tick: int, cfg: CampaignConfig) -> list:
    """Intenciones públicas por equipo identificable en cualquier libro visible (más reciente primero).

    El feed y los libros de venues distintos de El Rastro también son evidencia válida para
    abrir una conversación directa. La conversación/liquidación sigue pasando por el canal
    de negociación configurado; las restricciones específicas de venue se aplican después,
    en el coordinador, antes de enviar una acción.
    """
    team, valid = s["me"]["id"], valid_team_ids(s)
    out = []
    seen = set()

    def observe(o, maker, observed_tick, source, *, require_open=False):
        venue = o.get("venue")
        if maker not in valid or o.get("maker") != maker or o.get("to") not in (None, team):
            return
        if not venue or (require_open and o.get("status") != "open"):
            return
        oid = o.get("id")
        if oid is None or oid in seen:
            return
        x = _single(o)
        if not x or (o.get("expires_tick") or 10 ** 9) <= tick:
            return
        seen.add(oid)
        out.append({"team": maker, "side": x[0], "ref": x[1], "price": x[2], "asset": x[3], "offer": oid,
                    "tick": observed_tick, "venue": venue, "source": source})

    for e in (s.get("feed") or {}).get("events", []):
        if e.get("type") != "offer.listed" or e.get("tick", 0) < tick - cfg.evidence_ticks:
            continue
        o = (e.get("payload") or {}).get("offer") or {}
        maker = o.get("maker")
        if maker != e.get("actor"):
            continue
        observe(o, maker, e.get("tick"), "feed offer.listed")
    # Read current public boards too: feed history may omit older listings, and many
    # team venues publish there without an equivalent recent Rastro feed event.
    for venue, board in (s.get("boards") or {}).items():
        for o in (board or {}).get("offers", []):
            if o.get("venue") != venue:
                continue
            observe(o, o.get("maker"), o.get("created_tick", tick), f"libro público {venue}", require_open=True)
    for o in (s.get("offers") or {}).get("offers", []):
        if o.get("to") == team and o.get("maker") in valid and o.get("status") == "open":
            observe(o, o["maker"], o.get("created_tick", tick), "oferta dirigida a nosotros", require_open=True)
    return sorted(out, key=lambda x: -(x["tick"] or 0))


# ------------------------------------------------------------------ oportunidades

def opportunities(s: dict, val: tr.Valuation, counts: Counter, locked: set, cfg: CampaignConfig,
                  directly_acceptable: set) -> list:
    """Negociaciones que merecen un contacto. Lo que ya es aceptable tal cual lo resuelve el módulo de mercado."""
    tick, venue = s["clock"]["tick"], _rastro(s)
    ev = evidence(s, tick, cfg)
    sells = {}  # (equipo, ref) -> evidencia de que venden
    buys = {}
    for x in ev:
        (sells if x["side"] == "sells" else buys).setdefault((x["team"], x["ref"]), x)
    free = Counter()
    for a in s["me"]["assets"]:
        if a.get("kind") == "card" and a["id"] not in locked:
            free[a["ref"]] += 1
    out = []
    if "collect" in cfg.kinds:
        for (team, ref), x in sells.items():
            if counts.get(ref) or val.unit(ref) is None or x["offer"] in directly_acceptable:
                continue
            gain, notes = val.delta(counts, Counter({ref: 1}), Counter())
            reserve = math.floor(gain - cfg.margin)  # si aceptan ellos, pagan ellos la comisión
            if reserve < 1:
                continue
            out.append(_opp("collect", team, x, receive=ref, reserve=reserve, du_est=round(gain - min(reserve, x["price"]), 2),
                            notes=notes + [f"{team} vende {ref} a {x['price']} P ({x['source']}, tick {x['tick']})"]))
    if "sell" in cfg.kinds:
        for (team, ref), x in buys.items():
            n = counts.get(ref, 0)
            if n < 2 or free.get(ref, 0) < 1 or x["offer"] in directly_acceptable or val.unit(ref) is None:
                continue
            loss = val.copy_value(ref, n - 1)
            reserve = math.ceil(loss + cfg.margin)  # mínimo si aceptan ellos (no pagamos comisión)
            asset = _free_copy(s, ref, locked)
            out.append(_opp("sell", team, x, deliver=ref, asset=asset, reserve=reserve,
                            du_est=round(max(reserve, x["price"]) - loss, 2),
                            notes=[f"{team} compra {ref} a {x['price']} P ({x['source']}, tick {x['tick']})",
                                   f"perdemos {loss:.1f} P de valor de colección"]))
    if "swap" in cfg.kinds:
        for (team, want_ref), xs in sells.items():
            if counts.get(want_ref) or val.unit(want_ref) is None:
                continue
            for (team2, give_ref), xb in buys.items():
                if team2 != team or counts.get(give_ref, 0) < 2 or free.get(give_ref, 0) < 1:
                    continue
                du, notes = val.delta(counts, Counter({want_ref: 1}), Counter({give_ref: 1}))
                if du < cfg.margin:
                    continue
                out.append(_opp("swap", team, xs, receive=want_ref, deliver=give_ref,
                                asset=_free_copy(s, give_ref, locked), reserve=math.floor(du - cfg.margin),
                                du_est=round(du, 2), notes=notes + [
                                    f"{team} vende {want_ref} ({xs['price']} P) y compra {give_ref} ({xb['price']} P)"]))
    kept = []
    for o in out:
        # cercanía entre nuestra reserva privada y su precio público (heurística declarada, no una probabilidad)
        fit = o["reserve"] / o["anchor"] if o["kind"] != "sell" else o["anchor"] / max(1, o["reserve"])
        if o["kind"] == "swap":
            fit = 1.0
        o["fit"] = round(min(1.0, fit), 2)
        if fit < cfg.min_fit:
            continue
        o["first_cash"] = min(o["reserve"], max(1, round(o["anchor"] * cfg.buy_open_frac))) if o["kind"] == "collect" else 0
        o["score"] = round(o["du_est"] * cfg.contact_prior * o["fit"], 2)
        kept.append(o)
    return sorted(kept, key=lambda o: (-o["score"], o["anchor"] if o["kind"] == "collect" else -o["anchor"]))


def _rastro(s):
    return next((v for v in (s.get("venues") or {}).get("venues", []) if v.get("venue") == "rastro"), {})


def _free_copy(s, ref, locked):
    ids = sorted(a["id"] for a in s["me"]["assets"] if a.get("ref") == ref and a["id"] not in locked)
    return ids[-1] if ids else None


def _opp(kind, team, x, receive=None, deliver=None, asset=None, reserve=0, du_est=0.0, notes=()):
    return {"kind": kind, "team": team, "receive": receive, "deliver": deliver, "asset": asset, "reserve": reserve,
            "anchor": x["price"], "evidence": {k: x[k] for k in ("source", "offer", "tick", "price", "side")},
            "du_est": du_est, "notes": list(notes)}


def neg_id(o):
    return f"{o['kind']}:{o['team']}:{o.get('receive') or '-'}:{o.get('deliver') or '-'}"


# ------------------------------------------------------------------ propuestas

def proposal(neg: dict, k: int, their_last: int | None) -> tuple[dict, str] | None:
    """Propuesta k (0, 1, 2...) estrictamente mejor para ellos que la anterior y nunca más allá de nuestra reserva.
    Devuelve (offer, texto) con el texto generado a partir de la estructura."""
    kind, reserve, anchor = neg["kind"], neg["reserve"], neg["anchor"]
    prev = neg.get("last_price")
    if kind == "collect":
        target = min(reserve, their_last if their_last is not None else anchor)
        price = min(reserve, max(1, round(anchor * neg.get("buy_open_frac", 0.85)))) if k == 0 else \
            min(reserve, prev + math.ceil(neg.get("concession", 0.5) * max(1, target - prev)))
        if prev is not None and price <= prev:
            return None
        return ({"give": {"cash": price}, "want": {"cards": [neg["receive"]]}},
                f"Hola, somos Team 15. Nos interesa {neg['receive']}. Ofrecemos {price} P por una copia.")
    if kind == "sell":
        floor_ = reserve
        target = max(floor_, their_last if their_last is not None else anchor)
        price = max(floor_, math.ceil(anchor * neg.get("sell_markup", 1.15))) if k == 0 else \
            max(floor_, prev - math.ceil(neg.get("concession", 0.5) * max(1, prev - target)))
        if k == 0:
            price = max(price, anchor + 1)
        if prev is not None and price >= prev:
            return None
        return ({"give": {"assets": [neg["asset"]]}, "want": {"cash": price}},
                f"Hola, somos Team 15. Tenemos una copia de {neg['deliver']}. La ofrecemos por {price} P.")
    # swap: dar una copia concreta y, si hace falta, añadir efectivo sin bajar del margen
    room = max(0, reserve)
    cash = [0, room // 3, (2 * room) // 3][min(k, 2)]
    if prev is not None and cash <= prev and k > 0:
        return None
    give = {"assets": [neg["asset"]]}
    if cash:
        give["cash"] = cash
    extra = f" y {cash} P" if cash else ""
    return ({"give": give, "want": {"cards": [neg["receive"]]}},
            f"Hola, somos Team 15. Ofrecemos una copia de {neg['deliver']}{extra} por una copia de {neg['receive']}.")


def text_matches(offer: dict, text: str) -> bool:
    """El texto solo puede mencionar lo que la estructura contiene (cartas y cantidades de efectivo)."""
    g, w = offer.get("give") or {}, offer.get("want") or {}
    for cash in (g.get("cash"), w.get("cash")):
        if cash and f"{cash} P" not in text:
            return False
    refs = list(w.get("cards") or [])
    return all(r in text for r in refs)


# ------------------------------------------------------------------ máquina de estados

def thread_view(thread: dict, team: str, other: str):
    """(nuestras propuestas, sus ofertas abiertas para nosotros, id del último mensaje, ¿esperamos respuesta?)."""
    ours, theirs, last_id, last_sender = [], [], 0, None
    for m in thread.get("messages") or []:
        last_id, last_sender = max(last_id, m.get("id") or 0), m.get("sender")
        o = m.get("offer") or {}
        if m.get("sender") == team and o:
            ours.append(o)
        elif m.get("sender") == other and o and o.get("to") == team and o.get("status") == "open":
            theirs.append(o)
    return ours, theirs, last_id, last_sender == team


def step(neg: dict, thread: dict | None, s: dict, val: tr.Valuation, counts: Counter, locked: set, cfg: CampaignConfig,
         campaign_left: int, free_cash: int, ending: bool) -> dict | None:
    """Siguiente acción de una negociación (o None si toca esperar). Nunca repite un mensaje ni excede la reserva."""
    team, tick = s["me"]["id"], s["clock"]["tick"]
    venue = _rastro(s)
    if neg["state"] in ("liquidada", "abandonada", "caducada"):
        return None
    if thread is None:
        if neg.get("thread") or ending or neg["state"] == "ambigua":
            return None
        return _act(neg, "team_open", f"contactar a {neg['team']}", 0)
    ours, theirs, _, waiting = thread_view(thread, team, neg["team"])
    my_open = [o for o in ours if o.get("status") == "open"]
    neg["rounds"] = len(ours)
    if len(my_open) > 1:  # una propuesta nueva no garantiza que el servidor cancele la anterior
        return _act(neg, "team_cancel", f"cancelar propuesta antigua #{my_open[0]['id']}", 0, offer=my_open[0]["id"])
    if ending:
        if my_open:
            return _act(neg, "team_cancel", "fin de campaña: retirar propuesta abierta", 0, offer=my_open[-1]["id"])
        return _act(neg, "team_close", "fin de campaña", 0)
    # 1. Su contraoferta: se valora con la estructura, nunca con el texto.
    for o in reversed(theirs):
        lock_wo_mine = set(locked) - set(a for x in my_open for a in _asset_ids(x))
        p, why = tr.parse_offer(o, team=team, tick=tick, own_venue=None, my_assets=s["me"]["assets"],
                                locked=lock_wo_mine)
        if p is None:
            neg.setdefault("ignored", []).append({"offer": o.get("id"), "why": why})
            continue
        if set(p.receive) - {neg.get("receive")} - {None} or set(p.deliver) - {neg.get("deliver")} - {None} or \
                sum(p.receive.values()) > 1 or sum(p.deliver.values()) > 1:
            neg.setdefault("ignored", []).append({"offer": o["id"], "why": "fuera del objetivo de la negociación"})
            continue
        e = tr.evaluate(p, val, counts, venue)
        neg["their_last"] = p.cash_out or p.cash_in or 0
        cost = p.cash_out + e.fee
        if e.du >= cfg.margin and not e.blockers and cost <= min(campaign_left, free_cash) + _mine_cash(my_open):
            return _act(neg, "team_accept", f"su contraoferta #{o['id']} deja ΔU {e.du} P (comisión {e.fee} P, la pagamos)",
                        e.du, offer=o["id"], assets=p.deliver_assets, cost=cost)
    # 2. Plazos y silencio.
    if tick - neg["start_tick"] >= cfg.negotiation_ticks:
        if my_open:
            return _act(neg, "team_cancel", "plazo agotado: retirar propuesta", 0, offer=my_open[-1]["id"])
        return _act(neg, "team_close", "plazo agotado sin acuerdo", 0)
    if my_open and waiting:
        return None  # el silencio no es un rechazo
    if my_open and not theirs and tick - neg.get("last_tick", tick) < cfg.reply_ticks:
        return None
    # 3. Nueva propuesta (o la primera).
    if neg["rounds"] >= cfg.max_proposals:
        if my_open:
            return None
        return _act(neg, "team_close", f"{cfg.max_proposals} propuestas sin acuerdo", 0)
    prop = proposal(neg, neg["rounds"], neg.get("their_last"))
    if prop is None:
        return _act(neg, "team_close", "no queda una propuesta mejor dentro de nuestra reserva", 0)
    offer, text = prop
    need = int(offer["give"].get("cash") or 0)
    if need > min(campaign_left, free_cash) + _mine_cash(my_open):
        return None  # sin capacidad ahora; se reintenta cuando se libere
    if offer["give"].get("assets") and offer["give"]["assets"][0] in (locked - set(a for x in my_open for a in _asset_ids(x))):
        return _act(neg, "team_close", "la copia comprometida en otra operación", 0)
    return _act(neg, "team_propose", text, neg["du_est"], offer=offer, text=text,
                price=need or int(offer["want"].get("cash") or 0), cancel_previous=[o["id"] for o in my_open])


def _asset_ids(o):
    return [a["id"] if isinstance(a, dict) else a for a in (o.get("give") or {}).get("assets") or []]


def _mine_cash(my_open):
    return sum(int((o.get("give") or {}).get("cash") or 0) for o in my_open)


def _act(neg, type_, reason, du, **kw):
    return {"type": type_, "module": "campaña", "kind": f"{neg['kind']} con {neg['team']}", "neg": neg["id"],
            "team": neg["team"], "thread": neg.get("thread"), "ref": neg.get("receive") or neg.get("deliver"),
            "reason": reason, "du": du, "blockers": [], **kw}


def new_negotiation(o: dict, tick: int, cfg: CampaignConfig) -> dict:
    return {"id": neg_id(o), "campaign": o["kind"], **o, "state": "detectada", "thread": None, "start_tick": tick,
            "last_tick": tick, "rounds": 0, "proposals": [], "received": [], "last_price": None,
            "buy_open_frac": cfg.buy_open_frac, "sell_markup": cfg.sell_markup, "concession": cfg.concession}


def sync(neg: dict, thread: dict | None, team: str, tick: int) -> str | None:
    """Actualiza el estado con lo que dice el servidor. Devuelve la transición (para la consola) o None."""
    old = neg["state"]
    if thread is None:
        if neg.get("thread") and old in ACTIVE:
            neg["state"] = "abandonada"
    else:
        ours, theirs, last_id, waiting = thread_view(thread, team, neg["team"])
        st = thread.get("status")
        if st == "deal":
            settled = next((o for o in ours + [m.get("offer") or {} for m in thread.get("messages") or []]
                            if o.get("status") == "settled"), None)
            neg["state"] = "liquidada"
            if settled:
                neg["settled_offer"] = settled.get("id")
                neg["settled_give"], neg["settled_want"] = settled.get("give"), settled.get("want")
                neg["settled_maker"] = settled.get("maker")
        elif st not in (None, "open"):
            neg["state"] = "abandonada"
        elif theirs and last_id != neg.get("last_msg_id"):
            neg["state"] = "contraoferta"
        elif any(o.get("status") == "open" for o in ours):
            neg["state"] = "esperando" if waiting else "propuesta"
        elif ours:  # nuestra propuesta caducó o se retiró: se puede proponer de nuevo si quedan rondas
            neg["state"] = "contactada"
            if old in ("esperando", "propuesta"):
                neg["expired_proposals"] = neg.get("expired_proposals", 0) + 1
        elif old in ("detectada", "ambigua"):
            neg["state"] = "contactada"
        neg["last_msg_id"] = last_id
        neg["rounds"] = len(ours)
    return f"{old} → {neg['state']}" if neg["state"] != old else None
