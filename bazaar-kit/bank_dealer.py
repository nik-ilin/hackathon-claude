"""Don Ernesto (dealer ``banco``): collateral-aware card sales and bounded negotiation.

All inputs come from the coordinator's current API snapshot. The module is pure: it
creates candidates and reports quota/pack evidence, but never calls the API.
"""
from __future__ import annotations

import math
from collections import Counter

import page_guard as pg
import trading as tr
import negotiation as neg
from negotiation import Decision

DEALER_ID = "banco"
MAX_COUNTERS = 4
MAX_TICKS = 8
OPENING_MARKUP = 0.10
CONCESSIONS = (0.50, 0.35, 0.20, 0.10)


def available(s: dict, dealer: dict | None = None) -> bool:
    dealer = dealer if dealer is not None else (s.get("dealers") or {}).get(DEALER_ID)
    me = s.get("me") or {}
    return bool(dealer and dealer.get("enabled", True) and dealer.get("status", "active") == "active"
                and (DEALER_ID in (me.get("unlocked") or []) or dealer.get("open_to_all")))


def buy_rows(dealer: dict) -> list[dict]:
    return [r for r in ((dealer.get("menu") or {}).get("buys") or []) if r.get("rarity")]


def eligible(ref: str, card: dict, dealer: dict) -> bool:
    if not card or not card.get("released") or card.get("hidden"):
        return False
    sid = card.get("set") or str(ref).split("-", 1)[0]
    for row in buy_rows(dealer):
        if row.get("rarity") != card.get("rarity"):
            continue
        sets = row.get("sets")
        if sets == "released" or sets is None or isinstance(sets, list) and sid in sets:
            return True
    return False


def _net_minimum(gross: int, dealer: dict) -> int:
    menu = dealer.get("menu") or {}
    bps = int(dealer.get("fee_bps", menu.get("fee_bps", 0)) or 0)
    per_card = int(dealer.get("fee_per_card", menu.get("fee_per_card", 0)) or 0)
    return max(0, gross - math.ceil(gross * bps / 10_000) - per_card)


def gross_floor(net: int, dealer: dict) -> int:
    """Gross request whose net proceeds cover the full economic floor."""
    p = max(1, int(net))
    while _net_minimum(p, dealer) < net:
        p += 1
    return p


def quota(s: dict, led: dict, dealer: dict | None = None) -> dict:
    """Conservative rolling hourly use from feed, ledger, open threads and offers.

    Open contacts count against our local cap as well as settled deals. This is stricter
    than the server's deal quota and prevents burning contacts on repeated dead ends.
    """
    dealer = dealer if dealer is not None else (s.get("dealers") or {}).get(DEALER_ID, {})
    menu = dealer.get("menu") or {}
    limit = int(menu.get("deals_per_team_per_hour", 0) or 0)
    team = (s.get("me") or {}).get("id")
    clock = s.get("clock") or {}
    now_h = clock.get("t_hours")
    seconds = float(clock.get("tick_seconds") or 30.0)
    tick = int(clock.get("tick") or 0)
    span = max(1, math.ceil(3600 / seconds))

    def recent(event: dict) -> bool:
        if now_h is not None and isinstance(event.get("t"), (int, float)):
            return 0 <= float(now_h) - float(event["t"]) < 1.0
        return tick - int(event.get("tick") or 0) < span

    opened, settled = set(), set()
    for event in (s.get("feed") or {}).get("events", []):
        if not recent(event):
            continue
        p = event.get("payload") or {}
        if event.get("type") == "thread.opened" and str(p.get("team")) == str(team) \
                and p.get("with") == DEALER_ID:
            opened.add(str(p.get("thread", event.get("id"))))
        if event.get("type") != "settlement":
            continue
        participants = {str(x).lower() for x in (p.get("parties") or [])}
        for k in ("team", "buyer", "seller", "with", "persona"):
            if p.get(k) is not None:
                participants.add(str(p[k]).lower())
        for item in p.get("items") or []:
            for k in ("frm", "to"):
                if item.get(k) is not None:
                    participants.add(str(item[k]).lower())
        if str(team).lower() in participants and DEALER_ID in participants:
            settled.add(str(p.get("settlement", event.get("id"))))

    for action in led.get("actions", []):
        if action.get("dealer") != DEALER_ID or int(action.get("tick") or 0) < tick - span:
            continue
        if action.get("type") == "dealer_sell_open":
            opened.add(str(action.get("thread") or f"action:{action.get('key') or action.get('tick')}"))
        if action.get("type") in ("dealer_sell_accept", "dealer_sell_counter") \
                and action.get("status") == "settled":
            settled.add(str(action.get("thread")))

    active_threads = [t for t in ((s.get("threads") or {}).get("open") or [])
                      if t.get("kind") == "persona" and t.get("with") == DEALER_ID]
    pending_offers = [o for o in ((s.get("offers") or {}).get("offers") or [])
                      if o.get("status") in pg.OPEN_STATES and
                      (o.get("maker") == DEALER_ID and o.get("to") == team or
                       o.get("maker") == team and o.get("thread") in {t.get("id") for t in active_threads})]
    opened.update(str(t.get("id")) for t in active_threads)
    used = max(len(opened), len(settled), len(active_threads))
    return {"limit": limit, "settled": len(settled), "opened": len(opened),
            "pending_threads": len(active_threads), "pending_offers": len(pending_offers),
            "used": used, "remaining": max(0, limit - used),
            "product_limits": {str(r.get("pack") or r.get("rarity")): r.get("per_team_per_hour")
                               for r in (menu.get("sells") or []) if r.get("per_team_per_hour") is not None}}


def _best_alternative(candidates: list[dict], ref: str, asset_id, exclude_thread=None) -> dict | None:
    """Highest net cash from an already executable sale of this card."""
    rows = []
    for c in candidates or []:
        if c.get("blockers") or (exclude_thread is not None and c.get("thread") == exclude_thread):
            continue
        if c.get("type") == "accept" and c.get("cash", 0) > 0 and not c.get("receive") \
                and (c.get("deliver") or {}).get(ref, 0) > 0:
            rows.append({"net": int(c["cash"]), "kind": c.get("kind", "oferta ejecutable"),
                         "source": c.get("maker") or c.get("source") or "otro comprador"})
        elif c.get("type") == "dealer_sell_accept" and c.get("cash", 0) > 0 \
                and c.get("dealer") != DEALER_ID and c.get("asset") in (None, asset_id) \
                and str(c.get("item") or c.get("ref") or "").endswith(ref):
            rows.append({"net": int(c["cash"]), "kind": c.get("kind", "oferta de dealer ejecutable"),
                         "source": c.get("dealer")})
    return max(rows, key=lambda r: r["net"], default=None)


def _opening_ask(floor: int) -> int:
    return floor + max(2, math.ceil(floor * OPENING_MARKUP))


def sell_decision(state, floor: int, ticks_left: int, max_counters: int = MAX_COUNTERS) -> Decision:
    """Accept at our net floor; otherwise make four fixed, decreasing concessions.

    The schedule depends only on the private-value floor and prior offers. Dealer traits
    are deliberately not used as price or discount estimates.
    """
    live, turns = state.live, state.turns
    if live and live.price >= floor:
        return Decision("accept", f"oferta de Banco {live.price} P ≥ mínimo neto {floor} P", live.price, live.offer_id)
    if live and live.final:
        return Decision("abandon", f"oferta final de {live.price} P < mínimo neto {floor} P")
    if state.current is None:
        return Decision("wait" if ticks_left > 0 else "abandon", "Banco aún no ha presentado oferta")
    if state.awaiting_reply:
        return Decision("wait" if ticks_left > 0 else "abandon", "esperando respuesta de Banco")
    if ticks_left <= 0:
        return Decision("abandon", "límite temporal de la conversación de Banco agotado")
    if turns >= max_counters:
        return Decision("abandon", f"límite de {max_counters} contraofertas; Banco sigue bajo {floor} P")
    if turns == 0:
        price = _opening_ask(floor)
        return Decision("counter", f"apertura calculada desde el mínimo completo {floor} P; sin descuento supuesto", price)
    if state.last_ours is None:
        return Decision("abandon", "no hay base segura para continuar la negociación")
    factor = CONCESSIONS[min(turns - 1, len(CONCESSIONS) - 1)]
    gap = max(0, state.last_ours - floor)
    price = max(floor, floor + math.ceil(gap * (1.0 - factor)))
    if price >= state.last_ours:
        price = max(floor, state.last_ours - 1)
    return Decision("counter", f"concesión decreciente {factor:.0%}; nunca por debajo de {floor} P", price)


def _silver_pack_note(s: dict, val, counts: Counter, market_values: dict | None = None) -> str:
    packs = [a for a in (s.get("me") or {}).get("assets", []) if a.get("kind") == "pack"]
    catalog_packs = (s.get("catalog") or {}).get("packs", [])
    silver = []
    for asset in packs:
        aid = str(asset.get("pack") or asset.get("ref") or asset.get("name") or "").lower()
        pack = next((p for p in catalog_packs if str(p.get("id", "")).lower() == aid or
                     ("silver" in aid or "plata" in aid) and
                     ("silver" in str(p.get("name", "")).lower() or "plata" in str(p.get("name", "")).lower())), None)
        if pack and ("silver" in aid or "plata" in aid or "silver" in str(pack.get("name", "")).lower()
                     or "plata" in str(pack.get("name", "")).lower()):
            silver.append((asset, pack))
    if not silver:
        return "Silver pack: no hay una copia sin abrir en el inventario actual; no se abre ni se reserva capital."
    asset, pack = silver[0]
    ev = tr.pack_analysis(val, counts, pack, market_values or {}, draws=4000)
    # The dealer menu says what Banco sells, not that it buys unopened packs.
    bank_buys_pack = any(r.get("pack") for r in ((s.get("dealers") or {}).get(DEALER_ID, {}).get("menu") or {}).get("buys", []))
    return (f"Silver pack #{asset.get('id')}: abrir = RAW_COLLECTION_EV {ev['raw_collection_ev']} P, "
            f"P(página nueva) {ev['p_new_page']:.1%}, P(duplicado) {ev['p_duplicate']:.1%}; "
            f"supuesto: {ev['assumptions']}. "
            + ("Banco compra sobres según menú; comprobar oferta firme antes de vender." if bank_buys_pack else
               "Banco no anuncia compra de sobres; para venderlo hace falta una oferta de efectivo ejecutable. ")
            + "Evaluación informativa: no abre el sobre ni publica una venta.")


def plan(s: dict, led: dict, val, counts: Counter, *, margin: float, free_cash: int,
         budget_left: int, open_count: int, committed: set, alternatives: list[dict], market_values: dict | None = None) -> tuple[list[dict], dict | None]:
    """Create only affordable, unlocked sale-thread candidates for assets Banco currently buys."""
    dealer = (s.get("dealers") or {}).get(DEALER_ID)
    if not available(s, dealer):
        return [], None
    team, tick = s["me"]["id"], int(s["clock"].get("tick") or 0)
    q = quota(s, led, dealer)
    menu = dealer.get("menu") or {}
    cash_room = max(0, min(int(free_cash), int(budget_left)))
    active = [t for t in ((s.get("threads") or {}).get("open") or [])
              if t.get("kind") == "persona" and t.get("with") == DEALER_ID]
    own = [a for a in s["me"].get("assets", []) if a.get("kind") == "card"]
    blocked_ids = set(committed)
    for t in ((s.get("threads") or {}).get("open") or []):
        if t.get("kind") == "persona" and "sell" in (t.get("topic") or {}):
            blocked_ids.update(neg.sell_assets_of(t.get("topic")))
    committed_counts = Counter(a.get("ref") for a in (s.get("me") or {}).get("assets", [])
                               if a.get("id") in blocked_ids and a.get("kind") == "card")
    surplus_left = Counter({r: pg.tradeable_surplus(r, tr.counts_of(s["me"].get("assets", [])), s.get("catalog") or {},
                                                    committed_counts)
                           for r in {a.get("ref") for a in own if a.get("ref")}})
    reserved_refs = Counter()
    viable, out, reasons = [], [], []
    for a in own:
        ref = a.get("ref")
        cinfo = val.cards.get(ref)
        if not ref or not eligible(ref, cinfo, dealer):
            continue
        if a.get("id") in blocked_ids:
            reasons.append(f"{ref}: copia {a.get('id')} comprometida")
            continue
        if reserved_refs[ref] >= surplus_left[ref]:
            continue
        lost = -val.delta(counts, Counter(), Counter({ref: 1}))[0]
        min_net = max(1, math.ceil(lost + margin - 1e-9))
        floor = gross_floor(min_net, dealer)
        alt = _best_alternative(alternatives, ref, a.get("id"))
        blockers = []
        if active:
            blockers.append("ya hay una conversación activa con Banco")
        if q["remaining"] <= 0:
            blockers.append(f"cuota local de contactos agotada ({q['used']}/{q['limit']} en la última hora observada)")
        if open_count >= int((s.get("clock", {}).get("limits") or {}).get("max_open_threads_per_team", 6)):
            blockers.append("sin hueco de conversación")
        if alt and alt["net"] >= floor:
            blockers.append(f"otro comprador ejecutable ofrece {alt['net']} P netos ({alt['source']}); supera el mínimo {floor} P")
        candidate = {"type": "dealer_sell_open", "module": "banco", "kind": f"vender {ref} a Don Ernesto",
                     "dealer": DEALER_ID, "asset": a["id"], "ref": f"card:{ref}", "card": ref,
                     "floor": floor, "price": _opening_ask(floor), "cash": 0,
                     "du": round(-lost, 2), "score": 1_000 + int(dealer.get("level") or 0) * 100 - lost,
                     "blockers": blockers,
                     "notes": [f"pérdida marginal completa {lost:.2f} P; mínimo neto {min_net} P; fee API {floor-min_net} P",
                               "el menú confirma elegibilidad, no un precio de compra; no se infiere descuento de traits",
                               "nunca se compra a terceros suponiendo una futura oferta rentable de Banco"],
                     "reason": f"venta sujeta a oferta estructurada ≥ {floor} P netos"}
        page_blocks = pg.guard_candidate(candidate, s, committed)
        candidate["blockers"].extend(page_blocks)
        viable.append(candidate)
        reserved_refs[ref] += 1
        if not blockers and not page_blocks:
            out.append(candidate)
        else:
            reasons.extend(candidate["blockers"])

    products = []
    for row in menu.get("sells", []):
        if row.get("pack"):
            products.append(f"{row.get('name') or row['pack']}: lista {row.get('list_price')} P; "
                            f"apertura publicada {row.get('opening_ask', 'sin dato')} P; "
                            f"cuota {row.get('per_team_per_hour', 'sin dato')}/equipo/h")
        elif row.get("rarity"):
            products.append(f"{row['rarity']}: lista {row.get('list_price', 'sin dato')} P "
                            "(referencia, no oferta firme)")
    summary = {"dealer": DEALER_ID, "name": dealer.get("name", "Don Ernesto"), "tick": tick,
               "quota": q, "cash_room": cash_room, "buy_menu": buy_rows(dealer), "sell_menu": products,
               "eligible_assets": len(viable), "candidates": sum(c.get("type") != "info" for c in out),
               "silver_pack": _silver_pack_note(s, val, counts, market_values),
               "status": "operación de venta viable" if out else "banco desbloqueado, sin operación viable",
               "reasons": list(dict.fromkeys(reasons))[:8]}
    if not out:
        why = reasons or ([f"sin cartas elegibles en inventario (Banco compra: {', '.join(r.get('rarity', '?') for r in buy_rows(dealer)) or 'ninguna'})"]
                          if not viable else ["no quedan activos libres y disponibles"])
        products_unaffordable = [x for x in products if cash_room <= 0]
        if products_unaffordable:
            why += [f"efectivo/presupuesto utilizable {cash_room} P: productos de Banco no accesibles; no se reserva capital"]
        summary["reasons"] = list(dict.fromkeys(why))[:8]
        out.append({"type": "info", "module": "banco", "kind": summary["status"], "score": -1,
                    "blockers": summary["reasons"], "notes": [summary["silver_pack"]] + products,
                    "reason": "; ".join(summary["reasons"])})
    return out, summary


def thread_candidate(s: dict, thread: dict, val, counts: Counter, *, margin: float,
                     alternatives: list[dict], led: dict) -> dict:
    """Continue an existing sale thread without converting banker traits into price concessions."""
    ids = neg.sell_assets_of(thread.get("topic"))
    mine = {a.get("id"): a for a in (s.get("me") or {}).get("assets", [])}
    if len(ids) != 1 or ids[0] not in mine or mine[ids[0]].get("kind") != "card":
        return {"type": "dealer_close", "module": "banco", "kind": "cerrar Banco: activo ya no disponible",
                "thread": thread["id"], "dealer": DEALER_ID, "blockers": [], "score": 300_000,
                "reason": "la copia ya no está en nuestro inventario"}
    asset, ref = mine[ids[0]], mine[ids[0]].get("ref")
    dealer = (s.get("dealers") or {}).get(DEALER_ID, {})
    cinfo = val.cards.get(ref)
    if not eligible(ref, cinfo, dealer):
        return {"type": "dealer_close", "module": "banco", "kind": "cerrar Banco: menú ya no acepta la carta",
                "thread": thread["id"], "dealer": DEALER_ID, "blockers": [], "score": 300_000,
                "reason": "la elegibilidad de la carta desapareció del menú actual"}
    lost = -val.delta(counts, Counter(), Counter({ref: 1}))[0]
    floor = gross_floor(max(1, math.ceil(lost + margin - 1e-9)), dealer)
    alt = _best_alternative(alternatives, ref, asset.get("id"), exclude_thread=thread.get("id"))
    if alt and alt["net"] >= floor:
        return {"type": "dealer_close", "module": "banco", "kind": "cerrar Banco: mejor comprador ejecutable",
                "thread": thread["id"], "dealer": DEALER_ID, "blockers": [], "score": 300_000,
                "reason": f"otra oferta ejecutable paga {alt['net']} P netos ({alt['source']}) frente al mínimo {floor} P"}
    usage = quota(s, led, dealer)
    if usage["settled"] >= usage["limit"]:
        return {"type": "dealer_close", "module": "banco", "kind": "cerrar Banco: cuota horaria usada",
                "thread": thread["id"], "dealer": DEALER_ID, "blockers": [], "score": 300_000,
                "reason": f"{usage['settled']}/{usage['limit']} liquidaciones observadas en la última hora"}
    now_tick = int((s.get("clock") or {}).get("tick") or 0)
    st = neg.state_from_thread(thread, DEALER_ID, now_tick, neg.Config(), side="sell")
    left = MAX_TICKS - neg.conversation_ticks_used(thread, now_tick)
    d = sell_decision(st, floor, left)
    kind = {"counter": "dealer_sell_counter", "accept": "dealer_sell_accept", "abandon": "dealer_close"}.get(d.action)
    if not kind:
        return {"type": "info", "module": "banco", "kind": "Banco: " + d.action, "thread": thread["id"],
                "dealer": DEALER_ID, "blockers": [], "score": -1, "reason": d.reason}
    c = {"type": kind, "module": "banco", "kind": f"Don Ernesto: {d.action} {ref}", "thread": thread["id"],
         "dealer": DEALER_ID, "item": f"card:{ref}", "ref": f"card:{ref}", "asset": asset["id"],
         "price": d.price, "offer": d.offer_id, "opening": st.opening, "floor": floor,
         "cash": d.price if kind == "dealer_sell_accept" else 0,
         "du": round((d.price or 0) - lost, 2), "score": 250_000 if kind == "dealer_sell_accept" else 110_000,
         "turns": st.turns, "side": "sell", "blockers": [], "reason": d.reason,
         "notes": [f"pérdida marginal completa {lost:.2f} P; suelo {floor} P; concesiones fijas, sin usar traits"]}
    if kind == "dealer_sell_accept":
        off = neg.find_offer(thread, d.offer_id)
        c["blockers"] = neg.sell_offer_problems(off, dealer=DEALER_ID, asset_id=asset["id"], floor=floor)
    return c
