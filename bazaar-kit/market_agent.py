"""Mercado entre equipos: análisis por defecto; --execute envía UNA acción por ciclo.

Solo El Rastro, cartas individuales, sin especulación ni mensajes libres.
Los compromisos de compra consumen un presupuesto persistente conservador.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import time

from bazaar_sdk import Bazaar, BazaarError
from negotiation import InstanceLock, Journal
import trading

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
VERSION = "market-1.0"


def cards_by_ref(catalog):
    return {c["id"]: {**c, "set": s["id"], "released": s.get("released", False)}
            for s in catalog["sets"] for c in s["cards"] if not c.get("hidden")}


def inventory(me, catalog):
    groups = defaultdict(list)
    for a in me["assets"]:
        if a.get("kind") == "card":
            groups[a["ref"]].append(a)
    pages = []
    for s in catalog["sets"]:
        if not s.get("released"):
            continue
        required = [c["id"] for c in s["cards"] if c.get("page") and not c.get("hidden")]
        missing = [r for r in required if not groups[r]]
        pages.append({"set": s["id"], "name": s["name"], "have": len(required) - len(missing),
                      "total": len(required), "missing": missing,
                      "affinity": me.get("affinity", {}).get(s["id"])})
    duplicates = {r: [a["id"] for a in sorted(aa, key=lambda a: a["id"])[1:]]
                  for r, aa in groups.items() if len(aa) > 1}
    return groups, pages, duplicates


def fee_upper(price, venue):
    """Cota conservadora: redondeo hacia arriba; el aceptante paga según el SDK."""
    bps, fixed = venue.get("fee_bps"), venue.get("fee_per_card")
    if type(bps) is not int or type(fixed) is not int or bps < 0 or fixed < 0:
        raise ValueError("Comisiones desconocidas: no se puede valorar la operación")
    return math.ceil(price * bps / 10000) + fixed


def side(s):
    if not isinstance(s, dict) or set(s) - {"cash", "assets", "types", "cards"}:
        return None
    if s.get("cards"):  # el esquema observado de respuesta usa types
        return None
    cash = s.get("cash", 0)
    if type(cash) is not int or cash < 0:
        return None
    assets, types = s.get("assets", []), s.get("types", [])
    if not isinstance(assets, list) or not isinstance(types, list):
        return None
    return cash, assets, types


def parse_offer(o, own_ids, team, tick):
    """Rechaza lotes, swaps, extras, propias, privadas ajenas y ofertas a punto de vencer."""
    if (o.get("id") in own_ids or o.get("maker") == team or o.get("status") != "open"
            or o.get("to") not in (None, team) or o.get("venue") != "rastro"
            or o.get("thread") is not None or o.get("expires_tick", -1) <= tick + 1):
        return None
    g, w = side(o.get("give")), side(o.get("want"))
    if g is None or w is None:
        return None
    gc, ga, gt = g
    wc, wa, wt = w
    if gc == 0 and len(ga) == 1 and not gt and wc > 0 and not wa and not wt:
        a = ga[0]
        if isinstance(a, dict) and a.get("kind") == "card" and type(a.get("id")) is int and a.get("ref"):
            return {"kind": "ask", "ref": a["ref"], "asset": a["id"], "price": wc, "offer": o["id"]}
    if gc > 0 and not ga and not gt and wc == 0 and not wa and len(wt) == 1:
        if isinstance(wt[0], str) and wt[0].startswith("card:"):
            return {"kind": "bid", "ref": wt[0][5:], "price": gc, "offer": o["id"]}
    return None


def committed(offers, team):
    locked, buying, cash = set(), set(), 0
    for o in offers:
        if o.get("maker") != team or o.get("status") not in ("open", "queued", "accepted", "pending"):
            continue
        g, w = o.get("give") or {}, o.get("want") or {}
        cash += max(0, g.get("cash", 0))
        locked.update(a["id"] if isinstance(a, dict) else a for a in g.get("assets", []))
        buying.update(t[5:] for t in w.get("types", []) if isinstance(t, str) and t.startswith("card:"))
    return locked, buying, cash


def plan(snapshot, values, *, budget=100, reserve=100, margin=2, committed_budget=0, per_card=40):
    me, catalog, clock = snapshot["me"], snapshot["catalog"], snapshot["clock"]
    groups, pages, duplicates = inventory(me, catalog)
    cards = cards_by_ref(catalog)
    venue = next((v for v in snapshot["venues"]["venues"] if v.get("venue") == "rastro"), None)
    result = {"version": VERSION, "tick": clock["tick"], "team": me["id"], "cash": me["cash"],
              "pages": pages, "duplicates": duplicates, "actions": [], "notes": []}
    result["collection"] = [{"ref": r, "name": c.get("name", r), "rarity": c.get("rarity"),
        "set": c["set"], "page": c.get("page", False), "copies": len(groups[r]),
        "assets": [a["id"] for a in groups[r]], "next_copy_value": values.get(r)}
        for r, c in cards.items() if c["released"]]
    if not venue or venue.get("status") != "open" or venue.get("owner") == me["id"]:
        result["notes"].append("El Rastro no está disponible para este equipo")
        return result
    fee_upper(1, venue)  # valida antes de calcular precios
    if venue.get("pending_fee"):
        result["notes"].append("Cambio de comisión anunciado: esperar a su entrada en vigor para operar")
        return result
    own = snapshot["offers"].get("offers", [])
    own_ids = {o["id"] for o in own if o.get("maker") == me["id"]}
    locked, buying, reserved = committed(own, me["id"])
    available = min(budget - committed_budget, me["cash"] - reserve - reserved)
    result["buy_capacity"] = max(0, available)
    parsed = [p for o in snapshot["board"].get("offers", [])
              if (p := parse_offer(o, own_ids, me["id"], clock["tick"])) and p["ref"] in cards]
    asks, bids = defaultdict(list), defaultdict(list)
    for p in parsed:
        (asks if p["kind"] == "ask" else bids)[p["ref"]].append(p)
    for aa in asks.values():
        aa.sort(key=lambda p: p["price"])
    for bb in bids.values():
        bb.sort(key=lambda p: p["price"], reverse=True)
    remaining = {p["set"]: len(p["missing"]) for p in pages}
    # Venta de UNA copia extra por referencia. Nunca se suma el valor de todas las copias.
    for ref, ids in duplicates.items():
        if any(a["id"] in locked for a in groups[ref]):
            continue
        a = next((a for a in groups[ref] if a["id"] == ids[-1]), None)
        c = cards.get(ref)
        if not a or not c or not c["released"]:
            continue
        marginals = catalog.get("values", {}).get("copy_marginals", [])
        affinity = me.get("affinity", {}).get(c["set"])
        if not marginals or affinity is None:
            continue
        loss = math.ceil(max(float(a.get("your_value", 0)),
                             c["book"] * affinity * marginals[min(len(groups[ref]) - 1, len(marginals) - 1)]))
        bid = bids[ref][0] if bids[ref] else None
        if bid and bid["price"] - fee_upper(bid["price"], venue) >= loss + margin:
            net = bid["price"] - fee_upper(bid["price"], venue)
            result["actions"].append({"action": "sell", "ref": ref, "asset": a["id"],
                "offer": bid["offer"], "price": bid["price"], "net_cash_min": net,
                "surplus": net - loss, "reason": "Vender duplicado a demanda existente; conservar una copia"})
        else:
            price = max(loss + margin, (asks[ref][0]["price"] - 1) if asks[ref] else c["book"])
            result["actions"].append({"action": "list", "ref": ref, "asset": a["id"], "price": price,
                "surplus": price - loss, "reason": "Oferta de venta competitiva; ingreso NO garantizado"})
    for ref, c in cards.items():
        if (not c["released"] or groups[ref] or ref in buying or ref not in values
                or (c.get("minted") == 0 and not asks[ref])):
            continue
        value = values[ref]
        if not math.isfinite(value) or value <= 0:
            continue
        cap = math.floor(min(available, per_card, value - margin))
        if cap < 1:
            continue
        ask = asks[ref][0] if asks[ref] else None
        if ask and ask["price"] + fee_upper(ask["price"], venue) <= cap:
            cost = ask["price"] + fee_upper(ask["price"], venue)
            result["actions"].append({"action": "buy", "ref": ref, "asset": ask["asset"],
                "offer": ask["offer"], "price": ask["price"], "cost_max": cost,
                "surplus": round(value - cost, 2), "page_missing": remaining.get(c["set"], 99) if c.get("page") else 99,
                "reason": "Carta ausente; valor marginal API menos precio y comisión conservadora"})
        else:
            # Oferta pública corta: el aceptante paga comisión; reservamos también un colchón.
            target = math.floor(0.85 * (ask["price"] if ask else c["book"]))
            if bids[ref]:
                target = max(target, bids[ref][0]["price"] + 1)
            if target > cap:
                continue  # no inmovilizar presupuesto en pujas muy alejadas de la referencia
            price = min(target, cap)
            while price > 0 and price + fee_upper(price, venue) > cap:
                price -= 1
            if price > 0:
                result["actions"].append({"action": "bid", "ref": ref, "price": price,
                    "cost_max": price + fee_upper(price, venue), "surplus": round(value - price - fee_upper(price, venue), 2),
                    "page_missing": remaining.get(c["set"], 99) if c.get("page") else 99,
                    "reason": "Propuesta pública por carta ausente; aceptación NO garantizada"})
    priority = {"sell": 0, "buy": 1, "list": 2, "bid": 3}
    result["actions"].sort(key=lambda a: (priority[a["action"]],
        0 if a.get("page_missing") == 1 else 1, -a["surplus"], a.get("page_missing", 99), a["ref"]))
    result["notes"] += ["El excedente de colección no es beneficio realizado en efectivo.",
        "No se añade un bonus de página no verificado al valor marginal de la API.",
        "Compra de duplicados para reventa desactivada: no hay salida garantizada."]
    return result


class Reader:
    def __init__(self, api):
        self.api = api
        self.last = 0.0

    def call(self, method, *args):
        time.sleep(max(0, 0.27 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        return getattr(self.api, method)(*args)

    def snapshot(self):
        return {name: self.call(method) for name, method in
                [("catalog", "catalog"), ("me", "me"), ("venues", "venues"),
                 ("board", "board"), ("offers", "my_offers"), ("clock", "clock")]}

    def values(self, snapshot):
        held = {a["ref"] for a in snapshot["me"]["assets"] if a.get("kind") == "card"}
        return {ref: float(self.call("value", ref)["your_value"])
                for ref, c in cards_by_ref(snapshot["catalog"]).items() if c["released"] and ref not in held}


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def reconcile(state, snapshot):
    """Escrituras ambiguas paran; solo evidencia de activos confirma una aceptación."""
    p = state.get("pending")
    if not p:
        return True
    a = p["action"]
    ids = {x["id"] for x in snapshot["me"]["assets"]}
    arrived = a["action"] == "buy" and a["asset"] in ids and a["asset"] not in p["before_ids"]
    departed = a["action"] == "sell" and a["asset"] not in ids and a["asset"] in p["before_ids"]
    cash_delta = snapshot["me"]["cash"] - p["cash_before"]
    if (arrived and cash_delta <= -a["price"]) or (departed and cash_delta >= a["net_cash_min"]):
        state.setdefault("settlements", []).append({"action": a, "observed_cash_delta": cash_delta,
            "tick": snapshot["clock"]["tick"], "note": "Corroborado por activo y efectivo; otros flujos pueden afectar delta"})
        state["pending"] = None
        return True
    return False


def execute_one(reader, state, path, action, args):
    # Refresca todo antes de mutar, y recalcula la misma oportunidad.
    s = reader.snapshot()
    if s["clock"].get("paused") or s["clock"].get("doors", "open") != "open":
        raise ValueError("Juego cerrado: no envío acciones")
    if s["me"].get("frozen"):
        raise ValueError("Equipo congelado: no envío acciones")
    if s["me"].get("open_threads"):
        raise ValueError("Hay conversaciones abiertas: termina o cierra el agente de negociación antes de operar el mercado")
    vals = {}
    if action["action"] in ("buy", "bid"):
        vals[action["ref"]] = float(reader.call("value", action["ref"])["your_value"])
    fresh = plan(s, vals, budget=args.max_spend, reserve=args.reserve, margin=args.margin,
                 per_card=getattr(args, "per_card", 40),
                 committed_budget=state.get("committed", 0))
    if action not in fresh["actions"]:
        raise ValueError("La oportunidad ha cambiado: ejecuta de nuevo el análisis")
    active = s["offers"].get("offers", [])
    limits = s["clock"].get("limits", {})
    if action["action"] in ("bid", "list"):
        if len(active) >= limits.get("max_open_offers_per_team", 0):
            raise ValueError("Sin capacidad de ofertas abiertas")
        if limits.get("offers_per_team_per_tick", 0) < 1:
            raise ValueError("No se permite publicar ofertas ahora")
    elif limits.get("accepts_per_team_per_tick", 0) < 1:
        raise ValueError("No se permite aceptar ahora")
    if state.get("last_action_tick") == s["clock"]["tick"]:
        raise ValueError("Ya se envió una acción este tick; espera al siguiente")
    debit = action.get("cost_max", 0)
    state["committed"] = state.get("committed", 0) + debit
    state["pending"] = {"action": action, "before_ids": [a["id"] for a in s["me"]["assets"]],
                        "cash_before": s["me"]["cash"], "tick": s["clock"]["tick"]}
    state["last_action_tick"] = s["clock"]["tick"]
    state["last_action_type"] = action["action"]
    save(path, state)  # antes de escribir en red; no repetir si se pierde la respuesta
    try:
        if action["action"] == "buy":
            response = reader.api.accept(action["offer"])
        elif action["action"] == "sell":
            response = reader.api.accept(action["offer"], assets=[action["asset"]])
        elif action["action"] == "list":
            response = reader.api.list_offer({"assets": [action["asset"]]}, {"cash": action["price"]},
                                             venue="rastro", expires_in_ticks=4)
        else:
            response = reader.api.list_offer({"cash": action["price"]}, {"cards": [action["ref"]]},
                                             venue="rastro", expires_in_ticks=4)
    except BazaarError as e:
        if 400 <= e.status < 500:
            state["committed"] -= debit
            state["pending"] = None
            save(path, state)
        raise
    state.setdefault("submitted", []).append({"action": action, "response": response, "tick": s["clock"]["tick"]})
    if action["action"] in ("list", "bid"):
        state["pending"] = None  # respuesta de publicación, NO compra/venta confirmada
    save(path, state)
    return response


def choose_action(actions, state):
    """Liquidez/compra inmediata primero; alternar publicaciones para no relegar el álbum."""
    immediate = [a for a in actions if a["action"] in ("buy", "sell")]
    if immediate:
        return immediate[0]
    preferred = "bid" if state.get("last_action_type") == "list" else "list"
    return next((a for a in actions if a["action"] == preferred), actions[0] if actions else None)


def run_cycle(reader, args, state_path):
    state = json.loads(state_path.read_text()) if state_path.exists() else {"committed": 0}
    s = reader.snapshot()
    if state.get("team") not in (None, s["me"]["id"]):
        raise ValueError("El registro pertenece a otro equipo")
    state["team"] = s["me"]["id"]
    reconciled = reconcile(state, s)
    if args.execute:
        save(state_path, state)
    values = reader.values(s)
    report = plan(s, values, budget=args.max_spend, reserve=args.reserve, margin=args.margin,
                  per_card=args.per_card,
                  committed_budget=state.get("committed", 0))
    if not reconciled:
        report["notes"].append("Escritura pendiente/ambigua: ejecución bloqueada hasta verificarla")
    save(DATA / "market_report.json", report)
    print(f"{VERSION} · {s['me']['name']} · {report['cash']} P · tick {report['tick']}")
    for page in report["pages"]:
        print(f"{page['name']}: {page['have']}/{page['total']} · faltan {', '.join(page['missing']) or 'ninguna'}")
    print("Duplicados vendibles conservando una copia:", report["duplicates"])
    actions = [a for a in report["actions"] if args.action in ("best", a["action"])]
    for a in actions[:12]:
        print(json.dumps(a, ensure_ascii=False))
    for note in report["notes"]:
        print(note)
    if args.execute:
        if not reconciled:
            raise ValueError("Operación anterior sin reconciliar; no se reenvía")
        if (DATA / "pending.json").exists():
            raise ValueError("Hay una compra del agente de vendedores pendiente de liquidación")
        if actions:
            execute_one(reader, state, state_path, choose_action(actions, state), args)
            print("UNA acción enviada. Oferta publicada o aceptación pendiente; no se afirma liquidación.")
        else:
            print("Sin oportunidades válidas")
            return False
    else:
        print("Solo lectura: no se han enviado ofertas, mensajes ni aceptaciones.")
    return bool(args.execute and actions)


# ====================================================================== motor v2 (trading.py)
# Valoración marginal verificada contra collection_value, swaps/lotes/ofertas dirigidas y de conversación, reservas
# calculadas desde las ofertas abiertas del servidor (una oferta caducada ya no consume presupuesto), gasto confirmado
# separado, idempotencia por clave, reconciliación con liquidaciones públicas y detección de actividad concurrente.
# El motor v1 de arriba se conserva intacto (lo usa --engine v1 y sus pruebas).

VERSION2 = "market-2.0"
LEDGER = DATA / "market_ledger.json"
REPORT2 = DATA / "trading_report.json"


def snapshot_v2(reader):
    s = reader.snapshot()
    s["feed"] = reader.call("feed", 400)
    s["threads"] = {st: reader.call("my_threads", st).get("threads", []) for st in ("open", "deal")}
    s["team_threads"] = [t for t in s["threads"]["open"] if t.get("kind") != "persona"]
    s["levels"] = reader.call("levels")
    return s


def load_ledger(team):
    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    if led.get("team") not in (None, team):
        raise ValueError("El registro de mercado pertenece a otro equipo")
    led.setdefault("team", team)
    for k, v in (("actions", []), ("scores", []), ("spent_confirmed", 0), ("cash_received", 0)):
        led.setdefault(k, v)
    return led


def local_history():
    """Acciones registradas en este ordenador: motor v2, motor v1 (market_state.json) y agente de vendedores."""
    own = list(load_json(LEDGER).get("actions", []))
    v1 = load_json(DATA / "market_state.json")
    for x in v1.get("submitted", []):
        a = x.get("action", {})
        own.append({"status": "submitted", "asset": a.get("asset"), "offer": a.get("offer"), "engine": "v1",
                    "type": a.get("action"), "ref": a.get("ref"), "price": a.get("price"), "tick": x.get("tick")})
    j = Journal(str(DATA))
    # aceptación nuestra, o contraoferta nuestra que el dealer aceptó (mismo precio, ±2 ticks): ambas son propias
    accepts = [d for d in j.records("decision") if d.get("action") in ("accept", "counter")]
    threads = {d.get("thread") for d in j.records() if d.get("thread") is not None}
    return own, accepts, threads, j, v1


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def pack_value_now(val, counts, catalog, pack_id):
    pack = next((p for p in catalog.get("packs", []) if p["id"] == pack_id), None)
    if not pack:
        return None
    mean = {}
    for slot in pack["slots"]:
        for r in slot:
            pool = [c for c, x in val.cards.items() if x["released"] and x["rarity"] == r and not x.get("hidden")]
            mean[r] = sum(val.copy_value(c, counts.get(c, 0)) for c in pool) / len(pool) if pool else 0.0
    return round(sum(p * mean[r] for slot in pack["slots"] for r, p in slot.items()), 1)


def diagnose(s, led):
    team, tick = s["me"]["id"], s["clock"]["tick"]
    own, accepts, starter_threads, journal, v1 = local_history()
    sets = trading.settlements_for(s["feed"].get("events", []), team)
    for x in sets:
        x["origin"] = trading.classify(x, own, accepts)
    deal_threads = {t["id"]: t for t in s["threads"]["deal"]}
    outcomes = [o for o in journal.records("outcome")]
    corrections = trading.journal_corrections(outcomes, deal_threads, journal.records("correction"))
    since = led.get("since_tick", tick)
    flags = trading.unexplained_activity(sets, s["threads"]["open"] + s["threads"]["deal"], own, accepts,
                                         starter_threads, since)
    val = trading.Valuation(s["catalog"], s["me"].get("affinity") or {})
    counts = trading.counts_of(s["me"]["assets"])
    for x in sets:
        packs = [i for i in x["in"] if i[0] == "pack"]
        if packs:
            x["note"] = f"sobre: valor esperado HOY con tus valores ~{pack_value_now(val, counts, s['catalog'], packs[0][1])} P"
    return {"settlements": sets, "corrections": corrections, "concurrent": flags, "since_tick": since,
            "v1_pending": v1.get("pending"), "v1_committed": v1.get("committed")}


def reconcile_v2(led, s, sets):
    """Pone al día las acciones propias con evidencia del servidor. Devuelve las que bloquean (ambiguas)."""
    team, tick = s["me"]["id"], s["clock"]["tick"]
    mine_open = {o["id"]: o for o in s["offers"].get("offers", []) if o.get("maker") == team}
    for a in led["actions"]:
        if a["status"] not in ("intent", "ambiguous", "submitted"):
            continue
        hit = None
        for x in sets:
            if x["tick"] < a["tick"]:
                continue
            ids_in, ids_out = {i[2] for i in x["in"]}, {i[2] for i in x["out"]}
            refs_in = [i[1] for i in x["in"]]
            if (set(a.get("assets") or []) & ids_out or set(a.get("receive_assets") or []) & ids_in
                    or (a["type"] == "list" and a.get("asset") in ids_out)
                    or (a["type"] == "bid" and a.get("ref") in refs_in and x["price"] == a.get("price"))):
                hit = x
                break
        if hit:
            a.update(status="settled", settlement=hit["settlement"], settled_tick=hit["tick"])
            if hit["cash_direction"] == "pagamos":
                led["spent_confirmed"] += hit["price"] + (hit["fee"] if a["type"] == "accept" else 0)
            elif hit["cash_direction"] == "cobramos":
                led["cash_received"] += hit["price"] - (hit["fee"] if a["type"] == "accept" else 0)
            continue
        if a["type"] in ("list", "bid"):
            if a.get("offer_id") in mine_open:
                a["status"] = "submitted"
                continue
            if a["status"] != "submitted":  # respuesta perdida: ¿se publicó?
                twin = next((o for o in mine_open.values() if o.get("created_tick", 0) >= a["tick"] and
                             ((a["type"] == "list" and any((x.get("id") if isinstance(x, dict) else x) == a.get("asset")
                                                           for x in o["give"].get("assets") or [])) or
                              (a["type"] == "bid" and f"card:{a.get('ref')}" in (o["want"].get("types") or [])))), None)
                if twin:
                    a.update(status="submitted", offer_id=twin["id"])
                    continue
            if tick > a["tick"] + 2:
                a["status"] = "released"  # caducada, cancelada o nunca publicada: libera reserva
        elif tick > a["tick"] + 3:
            a["status"] = "released"  # la aceptación no se liquidó (otro la tomó antes o fue rechazada)
    return [a for a in led["actions"] if a["status"] in ("intent", "ambiguous")]


def pendings_v2(led):
    return [{"cost": a.get("cost", 0), "assets": a.get("assets") or []} for a in led["actions"]
            if a["type"] == "accept" and a["status"] in ("intent", "ambiguous", "submitted")]


def trading_config(args):
    return trading.Config(reserve=args.reserve, margin=args.margin, per_card=args.per_card, max_spend=args.max_spend,
                          listing_ticks=args.listing_ticks, fill_prior=args.fill_prior,
                          allow_last_copy=frozenset(x for x in (args.allow_last_copy or "").split(",") if x))


KIND = {"sell": "vender", "buy": "comprar", "swap": "intercambio", "list": "publicar venta", "bid": "publicar compra"}


def print_report(s, pl, diag, led):
    me, val = s["me"], trading.Valuation(s["catalog"], s["me"].get("affinity") or {})
    counts = trading.counts_of(me["assets"])
    sc = me.get("score") or {}
    print(f"{VERSION2} · {me['name']} · tick {pl['tick']} · {me['cash']} P · {sum(counts.values())} cartas, "
          f"{len(counts)} distintas")
    print(f"Valor de colección: modelo {pl['model_value']} P · servidor {pl['server_value']} P · "
          f"{'VERIFICADO' if pl['valuation_verified'] else 'NO verificado: ejecución bloqueada'}")
    print(f"Puntuación {sc.get('score')} (rank {sc.get('rank')}) · neg_points {sc.get('neg_points')} · "
          f"ladder {sc.get('ladder_points')} · market {sc.get('market')}")
    if led["scores"]:
        f0 = led["scores"][0]
        print(f"   desde el tick {f0['tick']}: {f0['score']} -> {sc.get('score')} (el leaderboard se refresca cada pocos "
              "minutos; si hubo operaciones de otros clientes con la clave, la variación no es solo de este agente)")
    print("\n-- Diagnóstico de liquidaciones (feed público, últimas 400 entradas)")
    for x in diag["settlements"]:
        items = ", ".join(f"{k}:{r}" for k, r, _ in x["in"]) or "-"
        gone = ", ".join(f"{k}:{r}" for k, r, _ in x["out"]) or "-"
        print(f"   tick {x['tick']} · {x['counterparty']} · recibimos [{items}] · entregamos [{gone}] · {x['price']} P "
              f"({x['cash_direction']}) · comisión {x['fee']} P · {x['origin']}" + (f" · {x['note']}" if x.get("note") else ""))
    for c in diag["corrections"]:
        print(f"   CORRECCIÓN hilo #{c['thread']} {c['item']}: registrado {c['old']} P, liquidado {c['new']} P "
              f"({c['evidence']}; {c['accepted_by']})")
    if diag["concurrent"]:
        print("   ACTIVIDAD NO REGISTRADA por los agentes de este ordenador desde el tick "
              f"{diag['since_tick']}: " + "; ".join(diag["concurrent"]))
    print("\n-- Inventario y páginas")
    for sid, req in val.pages.items():
        if req and val.cards[req[0]]["released"]:
            have = sum(1 for r in req if counts.get(r))
            print(f"   {sid} x{(me.get('affinity') or {}).get(sid)}: {have}/{len(req)} · faltan "
                  f"{', '.join(r for r in req if not counts.get(r)) or 'ninguna'}")
    dups = {r: n for r, n in counts.items() if n > 1}
    print("   duplicados (pérdida al vender cada copia extra, de la última a la segunda): " + "; ".join(
        f"{r} x{n} -> {', '.join(str(round(val.copy_value(r, k), 1)) for k in range(n - 1, 0, -1))} P"
        for r, n in sorted(dups.items())))
    print(f"\n-- Capital: efectivo {me['cash']} P · reserva mínima {pl['reserve']} P · reservado en ofertas abiertas "
          f"{pl['reserved_cash']} P · aceptaciones pendientes {pl['pending_cash']} P · libre {pl['free_cash']} P · "
          f"gasto confirmado de la sesión {pl['spent_session']} P (presupuesto restante {pl['budget_left']} P)")
    if diag.get("v1_committed"):
        print(f"   (el motor v1 lleva 'committed' = {diag['v1_committed']} P acumulados que nunca libera; v2 no lo usa)")
    print("\n-- Oportunidades (excedente = efectivo neto + cambio de valor de colección; NO son puntos)")
    for o in pl["opportunities"][:12]:
        what = o.get("ref") or f"recibe {o.get('receive')} entrega {o.get('deliver')}"
        print(f"   {'BLOQUEADA ' if o['blockers'] else ''}{o['kind']} {what} · oferta {o.get('offer', '-')} · precio "
              f"{o['price']} P · comisión {o['fee']} P · valor {o['dv']:+} P · excedente {o['du']:+} P · prob. "
              f"{o['p_fill']:.0%} · caduca {o.get('expires')} · {'; '.join(o['notes'] + o['blockers'])} · {o['uncertainty']}")
    reasons = {}
    for r in pl["rejected"]:
        reasons[r["why"].split(":")[0]] = reasons.get(r["why"].split(":")[0], 0) + 1
    print(f"   descartadas: {reasons}")
    for n in pl["notes"]:
        print(f"   NOTA: {n}")
    acts = led["actions"]
    print(f"\n-- Operaciones: propuestas {len(pl['opportunities'])} · publicadas abiertas {pl['open_offers']} · "
          f"aceptaciones pendientes {sum(a['type'] == 'accept' and a['status'] in ('submitted', 'ambiguous', 'intent') for a in acts)} · "
          f"liquidadas (v2) {sum(a['status'] == 'settled' for a in acts)} · ambiguas {sum(a['status'] in ('ambiguous', 'intent') for a in acts)}")
    act = [lv for lv in (s.get("levels") or {}).get("levels", [])]
    if act:
        print("-- Otras vías (fuera de este módulo): " + "; ".join(f"{lv.get('name')} ({lv.get('state')})" for lv in act))


def execute_v2(reader, led, args, key):
    s = snapshot_v2(reader)
    team, tick = s["me"]["id"], s["clock"]["tick"]
    if s["clock"].get("paused") or s["clock"].get("doors", "open") != "open":
        raise ValueError("Juego cerrado: no envío acciones")
    if s["me"].get("frozen"):
        raise ValueError("Equipo congelado")
    if any(t.get("kind") == "persona" for t in s["threads"]["open"]):
        raise ValueError("Hay conversaciones con dealers abiertas: no se comparte el efectivo a ciegas (no las cierro)")
    if (DATA / "pending.json").exists() or load_json(DATA / "market_state.json").get("pending"):
        raise ValueError("Hay una aceptación pendiente de otro agente de este ordenador")
    sets = trading.settlements_for(s["feed"].get("events", []), team)
    if reconcile_v2(led, s, sets):
        raise ValueError("Hay una escritura ambigua sin reconciliar: no se reenvía nada")
    pl = trading.plan(s, trading_config(args), pendings=pendings_v2(led), spent=led["spent_confirmed"])
    a = next((o for o in pl["opportunities"] if o["key"] == key), None)
    if a is None or a["blockers"]:
        raise ValueError("La oportunidad ha cambiado o está bloqueada: vuelve a analizar")
    if any(x.get("key") == key and x["status"] in ("intent", "ambiguous", "submitted") for x in led["actions"]):
        raise ValueError("Esa acción ya se envió (idempotencia)")
    if led.get("last_action_tick") == tick:
        raise ValueError("Ya se envió una acción este tick")
    limits = s["clock"].get("limits", {})
    if a["type"] == "accept" and limits.get("accepts_per_team_per_tick", 0) < 1:
        raise ValueError("No se permite aceptar ahora")
    if a["type"] in ("list", "bid") and (limits.get("offers_per_team_per_tick", 0) < 1 or
                                         pl["open_offers"] >= limits.get("max_open_offers_per_team", 0)):
        raise ValueError("Sin capacidad para publicar")
    receive_assets = []
    if a["type"] == "accept":
        o = next((x for x in s["board"].get("offers", []) + s["offers"].get("offers", []) if x.get("id") == a["offer"]), {})
        receive_assets = [x["id"] for x in (o.get("give") or {}).get("assets") or [] if isinstance(x, dict)]
    rec = {"key": key, "type": a["type"], "kind": a["kind"], "offer": a.get("offer"), "asset": a.get("asset"),
           "assets": a.get("assets") or ([a["asset"]] if a.get("asset") else []), "receive_assets": receive_assets,
           "ref": a.get("ref"), "price": a["price"], "cost": max(0, -a["cash"]), "du": a["du"], "dv": a["dv"],
           "tick": tick, "status": "intent", "engine": "v2"}
    led["actions"].append(rec)
    led["last_action_tick"] = tick
    save(LEDGER, led)  # antes de escribir en red: un reinicio nunca repite una acción ambigua
    try:
        if a["type"] == "accept":
            resp = reader.api.accept(a["offer"], assets=a.get("assets") or None)
        elif a["type"] == "list":
            resp = reader.api.list_offer({"assets": [a["asset"]]}, {"cash": a["price"]}, venue="rastro",
                                         expires_in_ticks=args.listing_ticks)
        else:
            resp = reader.api.list_offer({"cash": a["price"]}, {"cards": [a["ref"]]}, venue="rastro",
                                         expires_in_ticks=args.listing_ticks)
    except BazaarError as e:
        rec["status"], rec["error"] = ("rejected" if 400 <= e.status < 500 else "ambiguous"), f"{e.code}: {e.message}"
        save(LEDGER, led)
        raise
    rec.update(status="submitted", response=resp, offer_id=resp.get("id") or resp.get("offer"))
    save(LEDGER, led)
    return rec


def run_cycle_v2(reader, args):
    s = snapshot_v2(reader)
    led = load_ledger(s["me"]["id"])
    if args.execute and "since_tick" not in led:
        led["since_tick"] = s["clock"]["tick"]
    diag = diagnose(s, led)
    blocking = reconcile_v2(led, s, trading.settlements_for(s["feed"].get("events", []), s["me"]["id"]))
    pl = trading.plan(s, trading_config(args), pendings=pendings_v2(led), spent=led["spent_confirmed"])
    if args.execute:
        sc = s["me"].get("score") or {}
        led["scores"].append({"tick": s["clock"]["tick"], "score": sc.get("score"), "neg_points": sc.get("neg_points"),
                              "collection_value": s["me"].get("collection_value"), "cash": s["me"]["cash"]})
        save(LEDGER, led)
    print_report(s, pl, diag, led)
    save(REPORT2, {"version": VERSION2, "plan": pl, "diagnosis": diag})
    if args.apply_corrections and diag["corrections"]:
        j = Journal(str(DATA))
        for c in diag["corrections"]:
            j.append("correction", c)  # el registro original no se toca
        print(f"Anotadas {len(diag['corrections'])} correcciones en data/negotiations.jsonl")
    if not args.execute:
        print("\nSolo lectura: no se han enviado ofertas, mensajes, aceptaciones ni cancelaciones.")
        return False
    if blocking:
        raise ValueError("Escritura ambigua sin reconciliar: no se reenvía")
    if diag["concurrent"] and not args.allow_concurrent:
        raise ValueError("Hay actividad con la clave que no registró ningún agente de este ordenador; revisa quién más "
                         "opera o usa --allow-concurrent")
    want = KIND.get(args.action)
    choice = next((o for o in pl["opportunities"] if not o["blockers"] and (want is None or o["kind"] == want)), None)
    if not choice:
        print("Sin oportunidades válidas")
        return False
    rec = execute_v2(reader, led, args, choice["key"])
    print(f"UNA acción enviada ({rec['kind']}, clave {rec['key']}): {rec['status']}. No se afirma liquidación.")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="envía como máximo una acción validada por ciclo")
    mode.add_argument("--dry-run", action="store_true", help="solo análisis (por defecto)")
    p.add_argument("--max-spend", type=int, default=100, help="techo persistente de compromisos de compra, P")
    p.add_argument("--reserve", type=int, default=100)
    p.add_argument("--per-card", type=int, default=40, help="coste máximo por carta incluidas comisiones")
    p.add_argument("--margin", type=int, default=2)
    p.add_argument("--cycles", type=int, default=1, help="ciclos autónomos (1-30); una acción como máximo por ciclo")
    p.add_argument("--action", choices=["best", "buy", "sell", "swap", "list", "bid"], default="best")
    p.add_argument("--engine", choices=["v2", "v1"], default="v2", help="v2: motor de trading.py (por defecto)")
    p.add_argument("--listing-ticks", type=int, default=8, help="v2: caducidad de las publicaciones")
    p.add_argument("--fill-prior", type=float, default=0.3, help="v2: probabilidad SUPUESTA de ejecutar una publicación")
    p.add_argument("--allow-last-copy", default="", help="v2: refs separadas por comas cuya última copia se puede vender")
    p.add_argument("--allow-concurrent", action="store_true", help="v2: operar aunque haya actividad no registrada")
    p.add_argument("--apply-corrections", action="store_true", help="v2: anotar correcciones de precio en el registro")
    args = p.parse_args()
    if min(args.max_spend, args.reserve, args.margin, args.per_card) < 0:
        p.error("Los límites deben ser no negativos")
    if not 1 <= args.cycles <= 30:
        p.error("--cycles debe estar entre 1 y 30")
    DATA.mkdir(exist_ok=True)
    api = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"],
                 wait_on_tick=False, retries=0)
    reader = Reader(api)
    state_path = DATA / "market_state.json"
    lock = InstanceLock(str(DATA / "agent.lock"), {"version": VERSION2 if args.engine == "v2" else VERSION})
    if args.execute:
        holder = lock.acquire()
        if holder:
            raise SystemExit(f"Otro agente está activo (pid {holder.get('pid')}); no se inicia una segunda instancia")
    try:
        for cycle in range(args.cycles):
            acted = run_cycle_v2(reader, args) if args.engine == "v2" else run_cycle(reader, args, state_path)
            if not acted or cycle + 1 == args.cycles:
                break
            reader.api.wait_tick()
    finally:
        if args.execute:
            lock.release()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, BazaarError) as e:
        raise SystemExit(str(e))
