"""Coordinador único de The Bazaar: una sola autoridad que observa, decide y envía, con un presupuesto compartido.

    ./run.sh coord                                            # análisis (por defecto): solo lecturas
    ./run.sh coord --execute --ticks 20 --max-spend 80 --reserve 100 --per-card 60

Cada tick: observa (una instantánea) -> reconcilia lo enviado con evidencia del servidor -> genera CANDIDATAS de todos
los módulos (mercado entre equipos, vendedores, seguridad) -> SELECCIONA como mucho una acción por clase de límite
(aceptar, mensaje, publicar/cancelar, abrir conversación) -> ENVÍA tras anotarla -> marca LIQUIDADA cuando el servidor lo
confirma -> imprime el RESULTADO OBSERVADO (efectivo, activos, métricas privadas antes y después).

Lo que puntúa según el Kickoff: negociación (duelos, escalera de vendedores = parte del rango de precio capturada,
valor ganado comerciando con equipos a nuestros valores), market-making y jueces. La conversión de excedente o de
métricas privadas a puntos NO está documentada: no se inventa. El orden entre clases de candidatas es una heurística
declarada, no un óptimo.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import campaigns as cp
import market_agent as ma
import market_intel as mi
import negotiation as neg
import trading as tr
from bazaar_sdk import Bazaar, BazaarError

VERSION = "coord-1.0"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEDGER = DATA / "coordinator_ledger.json"
# Caducidad: lo pedido frente a lo devuelto por el servidor en nuestras publicaciones (a 60 s/tick).
EXPIRY_EVIDENCE = [{"requested": 4, "effective": 1, "tick_seconds": 60.0, "source": "motor v1, oferta 1458"},
                   {"requested": 20, "effective": 5, "tick_seconds": 60.0, "source": "motor v2, oferta 1688"},
                   {"requested": 8, "effective": 2, "tick_seconds": 60.0, "source": "motor v2, oferta 2092"}]
CLASSES = {"accept": "aceptar", "dealer_accept": "aceptar", "dealer_counter": "mensaje", "list": "publicar",
           "bid": "publicar", "cancel": "publicar", "dealer_open": "conversación", "dealer_close": "conversación",
           "team_accept": "aceptar", "team_propose": "mensaje", "team_open": "conversación", "team_cancel": "publicar",
           "team_close": "cierre", "swap_list": "publicar"}
QUOTA = {"persona_quota", "cooloff", "sold_out", "locked"}


# ------------------------------------------------------------------ estado persistido

def load_ledger(team):
    led = ma.load_json(LEDGER)
    if led.get("team") not in (None, team):
        raise ValueError("El registro del coordinador pertenece a otro equipo")
    led.setdefault("team", team)
    for k, v in (("actions", []), ("spent_confirmed", 0), ("cash_received", 0), ("expiry_obs", []),
                 ("blocked", {}), ("class_tick", {}), ("threads", []), ("negotiations", {}), ("campaign", None)):
        led.setdefault(k, v)
    return led


def save(led):
    ma.save(LEDGER, led)


def metrics(s):
    sc = s["me"].get("score") or {}
    return {"tick": s["clock"]["tick"], "round": s["clock"].get("round"), "cash": s["me"]["cash"],
            "collection_value": s["me"].get("collection_value"), "cards": sum(tr.counts_of(s["me"]["assets"]).values()),
            **{k: sc.get(k) for k in ("score", "negotiating", "market", "neg_points", "ladder_points", "duel_points",
                                      "mm_points", "deals", "rank")}}


# ------------------------------------------------------------------ observación

def snapshot(reader):
    s = ma.snapshot_v2(reader)
    s["leaderboard"] = reader.call("leaderboard")  # IDs de equipo válidos para abrir conversaciones
    s["boards"] = {"rastro": s["board"]}
    for v in (s.get("venues") or {}).get("venues", []):  # El Duende (v02) y demás: GET /api/venues/{id}/offers
        if v.get("status") == "open" and v.get("venue") != "rastro" and v.get("owner") != s["me"]["id"]:
            try:
                s["boards"][v["venue"]] = reader.call("board", v["venue"])
            except BazaarError:
                s["boards"][v["venue"]] = {"offers": []}
    ids = set(s["me"].get("unlocked") or [])
    for lv in (s.get("levels") or {}).get("levels", []):
        if lv.get("kind") == "persona" and lv.get("open_to_all"):
            ids.add(lv["id"])
    s["dealers"] = {}
    for d in sorted(ids | {"chato"}):
        try:
            s["dealers"][d] = reader.call("dealer", d)
        except BazaarError:
            pass
    return s


def other_processes():
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = ("starter_agent.py", "market_agent.py", "coordinator.py")
    return [l.strip() for l in out.splitlines()
            if any(n in l for n in names) and "python" in l.lower() and int(l.split()[0]) != os.getpid()]


# ------------------------------------------------------------------ reconciliación

def reconcile(led, s, journal):
    """Pone al día las acciones con evidencia del servidor. Devuelve (nuevas liquidadas, ambiguas que bloquean)."""
    team, tick = s["me"]["id"], s["clock"]["tick"]
    sets = tr.settlements_for(s["feed"].get("events", []), team)
    market = [a for a in led["actions"] if a["type"] in ("accept", "list", "bid", "team_accept", "swap_list")]
    view = {"actions": market, "spent_confirmed": led["spent_confirmed"], "cash_received": led["cash_received"]}
    before = {id(a): a["status"] for a in led["actions"]}
    ma.reconcile_v2(view, s, sets)
    led["spent_confirmed"], led["cash_received"] = view["spent_confirmed"], view["cash_received"]
    threads = {t["id"]: t for t in s["threads"]["open"] + s["threads"]["deal"]}
    for a in led["actions"]:
        if a["type"] == "cancel" and a["status"] in ("submitted", "ambiguous", "intent"):
            mine = {o["id"] for o in s["offers"].get("offers", []) if o.get("status") == "open"}
            if a["offer"] not in mine:
                a["status"] = "settled"  # la obligación ya no existe en el servidor
            elif a["status"] != "submitted" and tick > a["tick"] + 2:
                a["status"] = "released"
        if a["type"] in ("dealer_accept", "dealer_counter") and a["status"] in ("submitted", "ambiguous", "intent"):
            t = threads.get(a["thread"])
            if t and t.get("status") == "deal":
                paid = neg.settled_price(t, a["dealer"])
                if paid is not None and not any(x.get("thread") == a["thread"] and x["status"] == "settled"
                                                for x in led["actions"] if x is not a):
                    a.update(status="settled", paid=paid, settled_tick=tick)
                    led["spent_confirmed"] += paid
                    journal.append("outcome", {"dealer": a["dealer"], "item": a["item"], "thread": a["thread"],
                                               "status": "deal", "close_price": paid, "settled": True,
                                               "context": "normal", "opening": a.get("opening"),
                                               "note": f"coordinador {VERSION}"})
                else:
                    a["status"] = "released" if a["type"] == "dealer_counter" else a["status"]
            elif t is None or t.get("status") != "open":
                a["status"] = "released"  # conversación terminada sin trato
            elif a["type"] == "dealer_counter" and tick > a["tick"] + 1:
                a["status"] = "released"  # superada por la respuesta del vendedor
    fresh = [a for a in led["actions"] if a["status"] == "settled" and before.get(id(a)) != "settled"]
    return fresh, [a for a in led["actions"] if a["status"] in ("intent", "ambiguous")]


# ------------------------------------------------------------------ candidatas

def dealer_exposure(s, dealers_mine):
    """Efectivo que un vendedor podría llevarse aceptando nuestra contraoferta vigente."""
    out = 0
    for t in s["threads"]["open"]:
        if t.get("kind") == "persona":
            for o in t.get("standing_offers") or []:
                if o.get("maker") == s["me"]["id"] and o.get("status") == "open":
                    out += int((o.get("give") or {}).get("cash") or 0)
    return out


def candidates(s, led, args, journal):
    team, tick = s["me"]["id"], s["clock"]["tick"]
    me, val = s["me"], tr.Valuation(s["catalog"], s["me"].get("affinity") or {})
    counts = tr.counts_of(me["assets"])
    pend = [{"cost": a.get("cost", 0), "assets": a.get("assets") or []} for a in led["actions"]
            if a["type"] in ("accept", "dealer_accept") and a["status"] in ("intent", "ambiguous", "submitted")]
    exposure = dealer_exposure(s, led["threads"])
    pend.append({"cost": exposure})
    cfg = tr.Config(reserve=args.reserve, margin=args.margin, per_card=args.per_card, max_spend=args.max_spend,
                    listing_ticks=args.listing_ticks, fill_prior=args.fill_prior,
                    allow_last_copy=frozenset(x for x in args.allow_last_copy.split(",") if x))
    if getattr(args, "engine", "basic") == "intel":
        icfg = mi.IntelConfig(reserve=args.reserve, margin=args.margin, per_card=args.per_card, max_spend=args.max_spend,
                              allow_last_copy=cfg.allow_last_copy, duende_venue=args.duende_venue,
                              duende_expiry_ticks=args.duende_expiry, target_expiry_ticks=args.listing_ticks,
                              history_window_ticks=args.history_window)
        ratio, _ = tr.expiry_ratio(led["expiry_obs"] + EXPIRY_EVIDENCE, s["clock"].get("tick_seconds") or 60.0)
        hist = mi.History(str(DATA / "market_history.jsonl"), args.history_window).load(s["clock"]["tick"])
        pl = mi.plan(s, icfg, pendings=pend, spent=led["spent_confirmed"], actions=led["actions"], history=hist,
                     expiry_ratio=ratio)
        books, _ = mi.build_books(s, mi.venues_from(s))
        hist.record(s["clock"]["tick"], books, mi.public_settlements(s["feed"].get("events", [])), time.time())
    else:
        pl = tr.plan(s, cfg, pendings=pend, spent=led["spent_confirmed"])
    cap = max(0, min(pl["free_cash"], pl["budget_left"]))
    out = []
    # 1. Seguridad: publicaciones propias que venden la última copia o por debajo del valor.
    for c in tr.unsafe_own_offers(s["offers"].get("offers", []), team, val, counts):
        c.update(module="seguridad", du=0.0, score=10 ** 6, blockers=[] if args.cancel_unsafe else
                 ["requiere --cancel-unsafe (puede ser una oferta de un compañero)"])
        out.append(c)
    # 2. Mercado entre equipos (sin duplicar lo que una negociación de campaña activa ya persigue).
    camp_in = {n.get("receive") for n in led.get("negotiations", {}).values() if n["state"] in cp.ACTIVE} - {None}
    camp_out = {n.get("deliver") for n in led.get("negotiations", {}).values() if n["state"] in cp.ACTIVE} - {None}
    for o in pl["opportunities"]:
        o = dict(o, module="mercado")
        gets = set(o.get("receive") or {}) | ({o["ref"]} if o["type"] == "bid" else set())
        gives = set(o.get("deliver") or {}) | ({o["ref"]} if o["type"] == "list" else set())
        if gets & camp_in or gives & camp_out:
            o["blockers"] = list(o["blockers"]) + ["la campaña ya negocia esa carta con un equipo"]
        if o["type"] == "accept":
            o["score"] = 10 ** 4 + o["du"]
        out.append(o)
    # 3. Vendedores: conversaciones abiertas que son nuestras (no se tocan las ajenas).
    mine_threads = set(led["threads"]) | {d.get("thread") for d in journal.records("decision")}
    open_dealer = [t for t in s["threads"]["open"] if t.get("kind") == "persona"]
    busy = {t["with"] for t in open_dealer}
    for t in open_dealer:
        if t["id"] not in mine_threads:
            out.append({"type": "info", "module": "vendedores", "kind": "conversación ajena", "thread": t["id"],
                        "ref": str(t.get("topic")), "du": 0, "score": -1, "blockers": ["no es nuestra: no se toca"]})
            continue
        item = neg.item_of(t.get("topic"))
        pol = neg.dealer_policy(t["with"], args.mode)
        value = val.next_copy(counts, item[5:]) if item and item.startswith("card:") else None
        ceiling = math.floor(min(args.per_card, cap + exposure, (value or 0) - args.margin))
        st = neg.state_from_thread(t, t["with"], tick, neg.Config())
        d = neg.decide_dealer(st, pol, ceiling, pol.max_ticks - neg.conversation_ticks_used(t, tick))
        kind = {"counter": "dealer_counter", "accept": "dealer_accept", "abandon": "dealer_close"}.get(d.action)
        if kind:
            out.append({"type": kind, "module": "vendedores", "kind": f"{t['with']}: {d.action}", "thread": t["id"],
                        "dealer": t["with"], "item": item, "ref": item, "price": d.price, "offer": d.offer_id,
                        "opening": st.opening, "ceiling": ceiling, "du": round((value or 0) - (d.price or 0), 2),
                        "score": 10 ** 5, "reason": d.reason, "blockers": [], "turns": st.turns,
                        "notes": [f"política {pol.name} modo {args.mode}"]})
    # 4. Vendedores: abrir una conversación por una carta ausente que venden.
    open_count = len(s["threads"]["open"])
    for did, dealer in s["dealers"].items():
        unlocked = did in (me.get("unlocked") or []) or dealer.get("open_to_all")
        sells = (dealer.get("menu") or {}).get("sells", [])
        for row in sells:
            if "rarity" not in row:
                continue
            for ref, c in val.cards.items():
                if not c["released"] or c["rarity"] != row["rarity"] or counts.get(ref) or c.get("hidden"):
                    continue
                value = val.next_copy(counts, ref)
                ceiling = math.floor(min(args.per_card, cap, value - args.margin))
                lp = row.get("list_price")
                blockers = []
                if not unlocked:
                    u = dealer.get("unlock") or {}
                    blockers.append(f"{did} no disponible aún (abre a todos en {u.get('open_to_all_at')}; antes con "
                                    f"{u.get('early_min_deals')} tratos negociados con {u.get('early_deals_with')})")
                if did in busy:
                    blockers.append(f"ya hay una conversación abierta con {did}")
                if led["blocked"].get(did, 0) > tick:
                    blockers.append(f"{did} bloqueado hasta el tick {led['blocked'][did]} (cupo o enfriamiento)")
                if open_count >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
                    blockers.append("sin conversaciones libres")
                if lp is None or ceiling < lp * neg.dealer_policy(did, args.mode).min_viable_frac:
                    blockers.append(f"máximo {ceiling} P frente a precio publicado {lp} P")
                out.append({"type": "dealer_open", "module": "vendedores", "kind": f"abrir con {did}", "dealer": did,
                            "ref": f"card:{ref}", "price": lp, "ceiling": ceiling, "du": round(value - (lp or 0), 2),
                            "score": 10 ** 3 + value - (lp or 0), "blockers": blockers,
                            "notes": [f"valor {value:.1f} P (con bono si completa página)",
                                      "precio real desconocido hasta su primera oferta",
                                      "cuenta para la escalera solo si cerramos por debajo de su apertura"]})
        for row in sells:
            if "pack" in row:
                pack = next((p for p in s["catalog"].get("packs", []) if p["id"] == row["pack"]), None)
                if pack:
                    ev, why = tr.pack_value(val, counts, pack)
                    out.append({"type": "info", "module": "vendedores", "kind": f"sobre {row['pack']} ({did})",
                                "ref": f"pack:{row['pack']}", "price": row.get("list_price"), "du": round(ev - row["list_price"], 1),
                                "score": -1, "blockers": [f"no se compra por rutina: valor esperado {ev} P frente a "
                                                          f"{row['list_price']} P publicado ({why}); la suerte no puntúa"]})
    return out, pl, exposure


def campaign_cfg(args):
    kinds = ("collect", "sell", "swap") if args.campaign in ("all", "none") else (args.campaign,)
    return cp.CampaignConfig(kinds=kinds, ticks=args.campaign_ticks, budget=args.campaign_budget,
                             max_proposals=args.max_proposals, negotiation_ticks=args.negotiation_ticks,
                             max_conversations=args.max_conversations, margin=args.margin)


def counterparty(t, team):
    return t.get("team") if t.get("with") == team else t.get("with")


def campaign_candidates(s, led, args, pl, execute):
    """Candidatas de la campaña activa (o una vista previa en análisis) y líneas de estado para la consola."""
    team, tick = s["me"]["id"], s["clock"]["tick"]
    if args.campaign == "none" and not led.get("campaign"):
        return [], []
    cfg = campaign_cfg(args)
    camp = led.get("campaign")
    if camp is None or camp.get("closed"):
        if args.campaign == "none":
            return [], []
        camp = {"id": f"c{tick}", "kinds": list(cfg.kinds), "start_tick": tick, "end_tick": tick + cfg.ticks,
                "budget": cfg.budget, "spent": 0, "received": 0, "before": metrics(s), "closed": False,
                "stats": {"contacts": 0, "replies": 0, "proposals": 0, "accepted": 0, "settled": 0, "cards_in": [],
                          "dups_sold": [], "surplus_est": 0.0, "ticks_to_close": []}}
        if execute:
            led["campaign"] = camp
    negs = led["negotiations"]
    val, counts = tr.Valuation(s["catalog"], s["me"].get("affinity") or {}), tr.counts_of(s["me"]["assets"])
    res = tr.resources(s["offers"].get("offers", []), team, [])
    team_threads = {t["id"]: t for t in s["threads"]["open"] + s["threads"]["deal"] if t.get("kind") == "team"}
    lines = []
    for n in negs.values():
        if n["state"] == "ambigua" and not n.get("thread"):  # ¿se abrió la conversación aunque se perdiera la respuesta?
            t = next((t for t in team_threads.values() if counterparty(t, team) == n["team"]
                      and (t.get("topic") or {}).get("card") in (n.get("receive"), n.get("deliver"))
                      and t.get("created_tick", 0) >= n["start_tick"]), None)
            if t:
                n["thread"] = t["id"]
        change = cp.sync(n, team_threads.get(n.get("thread")), team, tick) if n["state"] not in ("liquidada",) else None
        if change:
            lines.append(f"NEGOCIACIÓN {n['kind']} con {n['team']} ({n.get('receive') or n.get('deliver')}, "
                         f"hilo #{n.get('thread')}): {change}")
            if n["state"] == "contraoferta":
                camp["stats"]["replies"] += 1
            if n["state"] == "liquidada" and not n.get("counted"):
                n["counted"] = True
                camp["stats"]["settled"] += 1
                camp["stats"]["ticks_to_close"].append(tick - n["start_tick"])
                camp["stats"]["surplus_est"] += n.get("du_est", 0)
                if n.get("receive"):
                    camp["stats"]["cards_in"].append(n["receive"])
                if n.get("deliver"):
                    camp["stats"]["dups_sold"].append(n["deliver"])
                if n.get("settled_maker") == team:  # aceptaron NUESTRA propuesta: ellos pagan la comisión
                    paid = int((n.get("settled_give") or {}).get("cash") or 0)
                    got = int((n.get("settled_want") or {}).get("cash") or 0)
                    led["spent_confirmed"] += paid
                    led["cash_received"] += got
                    camp["spent"] += paid
                    camp["received"] += got
                lines.append(f"LIQUIDADA  [campaña] {n['kind']} con {n['team']} · oferta #{n.get('settled_offer')} · "
                             f"RESULTADO OBSERVADO {observed({'before': camp['before']}, s, [])}")
    for a in led["actions"]:  # aceptaciones nuestras liquidadas: suman al gasto de la campaña una sola vez
        if a["type"] == "team_accept" and a["status"] == "settled" and not a.get("campaign_counted"):
            a["campaign_counted"] = True
            camp["spent"] += a.get("cost") or 0
    ending = tick >= camp["end_tick"] or camp["spent"] >= camp["budget"]
    mine_threads = {n["thread"] for n in negs.values() if n.get("thread")}
    committed = sum(int((o.get("give") or {}).get("cash") or 0) for o in res.open_offers if o.get("thread") in mine_threads)
    camp_left = camp["budget"] - camp["spent"] - committed
    out = []
    locked = set(res.locked_assets)
    for n in negs.values():
        if n["state"] not in cp.ACTIVE:
            continue
        a = cp.step(n, team_threads.get(n.get("thread")), s, val, counts, locked, cfg, camp_left, pl["free_cash"], ending)
        if a:
            a["score"] = 10 ** 5 + (1000 if a["type"] == "team_accept" else 0)
            out.append(a)
    obligations = [o["id"] for o in res.open_offers if o.get("thread") in mine_threads]
    if ending:
        if not any(n["state"] in cp.ACTIVE for n in negs.values()) and execute and not camp.get("closed"):
            camp["closed"] = True
        lines.append(f"CAMPAÑA {camp['id']} terminando (tick {tick} / fin {camp['end_tick']}, gastado {camp['spent']}/"
                     f"{camp['budget']} P): no abre conversaciones; ofertas propias aún abiertas: {obligations or 'ninguna'}")
    else:
        active = [n for n in negs.values() if n["state"] in cp.ACTIVE]
        slots = min(cfg.max_conversations - len(active),
                    s["clock"]["limits"].get("max_open_threads_per_team", 6) - len(s["threads"]["open"]))
        acceptable = {o.get("offer") for o in pl["opportunities"] if o["type"] == "accept"}
        busy_refs = {n.get("receive") for n in active} | {n.get("deliver") for n in active} | \
            set(res.buying_refs) | set(res.selling_refs)
        busy_assets = {n.get("asset") for n in active if n.get("asset")}
        for o in cp.opportunities(s, val, counts, locked | busy_assets, cfg, acceptable):
            nid = cp.neg_id(o)
            blockers = []
            if nid in negs:
                continue  # un solo contacto por contraparte y objetivo
            if (o.get("receive") and o["receive"] in busy_refs) or (o.get("deliver") and o["deliver"] in busy_refs):
                blockers.append("ya hay una negociación activa por esa carta")
            if o.get("asset") in busy_assets:
                blockers.append("esa copia ya está comprometida")
            need = o.get("first_cash", 0)
            if need > min(camp_left, pl["free_cash"]):
                blockers.append(f"sin efectivo libre para la primera propuesta ({need} P; libre {pl['free_cash']} P, "
                                f"campaña {camp_left} P): no se contacta para no hacer perder el tiempo")
            if slots <= 0 and not blockers:
                blockers.append(f"sin capacidad: máximo {cfg.max_conversations} negociaciones de campaña a la vez "
                                "(contando las abiertas en este ciclo)")
            if not blockers:
                slots -= 1
                busy_refs |= {o.get("receive"), o.get("deliver")}
            out.append({"type": "team_open", "module": "campaña", "kind": f"{o['kind']} con {o['team']}",
                        "team": o["team"], "ref": o.get("receive") or o.get("deliver"), "price": o["anchor"],
                        "du": o["du_est"], "score": 10 ** 3 + o["score"], "blockers": blockers, "opp": o,
                        "reason": "; ".join(o["notes"]) + f" · reserva privada {o['reserve']} P (no se revela) · "
                                  f"cercanía reserva/precio {o['fit']} · prob. de respuesta {cfg.contact_prior:.0%} = "
                                  "SUPUESTO"})
    if not pl["valuation_verified"]:
        for c in out:
            c["blockers"] = list(c.get("blockers") or []) + ["valoración no verificada contra collection_value"]
    st = camp["stats"]
    lines.append(f"CAMPAÑA {camp['id']} ({','.join(camp['kinds'])}) ticks {camp['start_tick']}-{camp['end_tick']} · "
                 f"presupuesto {camp['budget']} P (gastado {camp['spent']}, comprometido {committed}) · contactos "
                 f"{st['contacts']} · respuestas {st['replies']} · propuestas {st['proposals']} · aceptadas "
                 f"{st['accepted']} · liquidadas {st['settled']} · cartas {st['cards_in']} · duplicados vendidos "
                 f"{st['dups_sold']} · cobrado {camp['received']} P" + ("" if execute else " · (vista previa)"))
    return out, lines


def _cards_of(c):
    """Cartas que una candidata adquiere o entrega (para no perseguir la misma por dos vías)."""
    if c["type"] in ("cancel", "team_cancel", "team_close", "dealer_close", "info"):
        return set()
    out = set(c.get("receive") or {}) | set(c.get("deliver") or {})
    o = c.get("opp") or {}
    out |= {o.get("receive"), o.get("deliver")} - {None}
    if c["type"] in ("bid", "list", "dealer_open", "swap_list") and c.get("ref"):
        out.add(c["ref"].split(":")[-1])
    if c.get("give_ref"):
        out.add(c["give_ref"])
    return out


def select(cands, led, tick, max_posts=1):
    """Como mucho una acción por clase de límite y tick (publicar: hasta `max_posts`, acotado por el servidor);
    dentro de cada clase, la de mayor puntuación."""
    chosen, used, posts = [], set(), 0
    done = led.get("class_count", {})
    for c in sorted((c for c in cands if not c.get("blockers") and c["type"] in CLASSES),
                    key=lambda c: -c.get("score", 0)):
        cls = CLASSES[c["type"]]
        if cls == "mensaje":  # el límite es un mensaje por conversación y tick
            cls = f"mensaje:{c.get('thread')}"
        if cls == "publicar":
            prev = done.get(cls, [None, 0])
            if posts + (prev[1] if prev[0] == tick else 0) >= max_posts:
                continue
        elif cls in used or led["class_tick"].get(cls) == tick:
            continue
        if c.get("thread") and any(x.get("thread") == c["thread"] for x in chosen):
            continue  # una sola acción por conversación y tick
        if _cards_of(c) & set().union(*(_cards_of(x) for x in chosen)) if chosen else False:
            continue  # la misma carta no se persigue por dos vías en el mismo tick
        chosen.append(c)
        used.add(cls)
        posts += cls == "publicar"
    return chosen


# ------------------------------------------------------------------ envío

def describe(c):
    what = c.get("ref") or f"recibe {c.get('receive')} entrega {c.get('deliver')}"
    ids = " ".join(f"{k} #{c[k]}" for k in ("offer", "thread") if c.get(k))
    return f"[{c['module']}] {c['kind']} {what} {('· ' + str(c['price']) + ' P') if c.get('price') else ''} {ids}".strip()


def send(reader, led, s, c, args, journal):
    tick = s["clock"]["tick"]
    key = tr.idem_key({**c, "type": c["type"], "tick_scope": c.get("thread")})
    if any(a.get("key") == key and a["status"] in ("intent", "ambiguous", "submitted") for a in led["actions"]):
        print(f"   (ya enviada antes, no se repite: {describe(c)})")
        return None
    rec = {"key": key, "type": c["type"], "module": c["module"], "kind": c["kind"], "tick": tick, "status": "intent",
           "offer": c.get("offer"), "asset": c.get("asset"), "assets": c.get("assets") or ([c["asset"]] if c.get("asset") else []),
           "ref": c.get("ref"), "price": c.get("price"), "cost": max(0, -(c.get("cash") or 0)) or (c.get("price") or 0
           if c["type"] == "dealer_accept" else 0), "du": c.get("du"), "thread": c.get("thread"),
           "dealer": c.get("dealer"), "item": c.get("item"), "opening": c.get("opening"), "ceiling": c.get("ceiling"),
           "reason": c.get("reason") or "; ".join(c.get("notes") or c.get("why") or []), "before": metrics(s),
           "version": VERSION}
    if c.get("neg") and c["neg"] in led["negotiations"]:
        led["negotiations"][c["neg"]]["prev_state"] = led["negotiations"][c["neg"]]["state"]
    if c["type"] in ("accept", "team_accept"):
        pool = s["board"].get("offers", []) + s["offers"].get("offers", []) + \
            [o for t in s["threads"]["open"] for o in t.get("standing_offers") or []]
        o = next((x for x in pool if x.get("id") == c["offer"]), {})
        rec["receive_assets"] = [x["id"] for x in (o.get("give") or {}).get("assets") or [] if isinstance(x, dict)]
    rec["venue"] = c.get("venue") or ("rastro" if c["type"] in ("list", "bid", "accept") else None)
    rec["to"] = c.get("to")
    led["actions"].append(rec)
    cls = CLASSES[c["type"]]
    led["class_tick"][cls] = tick
    cc = led.setdefault("class_count", {}).get(cls, [None, 0])
    led["class_count"][cls] = [tick, (cc[1] if cc[0] == tick else 0) + 1]
    save(led)  # antes de escribir en red
    ratio, basis = tr.expiry_ratio(led["expiry_obs"] + EXPIRY_EVIDENCE, s["clock"].get("tick_seconds") or 60.0)
    try:
        if c["type"] == "accept":
            resp = reader.api.accept(c["offer"], assets=c.get("assets") or None)
        elif c["type"] in ("list", "bid", "swap_list"):
            req = c.get("expires_in") or tr.listing_request(args.listing_ticks, ratio)
            rec["requested_ticks"] = req
            rec["expiry_basis"] = "recomendación del venue (configurable)" if c.get("expires_in") and \
                c.get("venue") == getattr(args, "duende_venue", "v02") else basis
            if c["type"] == "list":
                give, want = {"assets": [c["asset"]]}, {"cash": c["price"]}
            elif c["type"] == "swap_list":
                give, want = {"assets": [c["asset"]]}, {"cards": [c["ref"]]}
            else:
                give, want = {"cash": c["price"]}, {"cards": [c["ref"]]}
            resp = reader.api.list_offer(give, want, venue=c.get("venue") or "rastro", to=c.get("to"),
                                         expires_in_ticks=req)
            if resp.get("expires_tick") and resp.get("created_tick") is not None:
                eff = resp["expires_tick"] - resp["created_tick"]
                rec["effective_ticks"] = eff
                led["expiry_obs"].append({"requested": req, "effective": eff, "tick_seconds": s["clock"].get("tick_seconds"),
                                          "source": f"oferta {resp.get('id')}"})
        elif c["type"] == "cancel":
            resp = reader.api.cancel(c["offer"])
        elif c["type"] == "dealer_counter":
            resp = reader.api.say(c["thread"], neg.message(c.get("turns", 0), c["price"], c["ref"]), price=c["price"])
            journal.append("decision", {"dealer": c["dealer"], "item": c["item"], "thread": c["thread"], "tick": tick,
                                        "action": "counter", "price": c["price"], "reason": c.get("reason"),
                                        "mode": f"coord-{args.mode}"})
        elif c["type"] == "dealer_accept":
            resp = reader.api.accept(c["offer"])
            journal.append("decision", {"dealer": c["dealer"], "item": c["item"], "thread": c["thread"], "tick": tick,
                                        "action": "accept", "price": c["price"], "reason": c.get("reason"),
                                        "mode": f"coord-{args.mode}"})
        elif c["type"] == "dealer_close":
            resp = reader.api.close_thread(c["thread"])
        elif c["type"] == "team_open":
            neg = cp.new_negotiation(c["opp"], tick, campaign_cfg(args))
            neg["state"] = "ambigua"  # hasta saber si la conversación se abrió
            led["negotiations"][neg["id"]] = neg
            save(led)
            topic = {"campaign": neg["kind"], "card": neg.get("receive") or neg.get("deliver")}
            resp = reader.api.open_thread(c["team"], topic=topic, venue="rastro")
            neg.update(thread=resp.get("id"), state="contactada", last_tick=tick)
            rec["thread"] = resp.get("id")
            led["campaign"]["stats"]["contacts"] += 1
        elif c["type"] == "team_propose":
            neg = led["negotiations"][c["neg"]]
            assert cp.text_matches(c["offer"], c["text"]), "el texto no coincide con la estructura"
            neg["state"] = "ambigua"
            save(led)
            resp = reader.api.say(c["thread"], c["text"], offer=c["offer"])
            g, w = c["offer"]["give"], c["offer"]["want"]
            neg["last_price"] = int(g.get("cash") or 0) if neg["kind"] != "sell" else int(w.get("cash") or 0)
            neg["proposals"].append({"tick": tick, "offer": c["offer"], "text": c["text"]})
            neg.update(state="propuesta", last_tick=tick)
            led["campaign"]["stats"]["proposals"] += 1
        elif c["type"] == "team_accept":
            resp = reader.api.accept(c["offer"], assets=c.get("assets") or None)
            led["negotiations"][c["neg"]]["state"] = "aceptada"
            led["campaign"]["stats"]["accepted"] += 1
        elif c["type"] == "team_cancel":
            resp = reader.api.cancel(c["offer"])
        elif c["type"] == "team_close":
            resp = reader.api.close_thread(c["thread"])
            led["negotiations"][c["neg"]]["state"] = "abandonada"
        else:  # dealer_open
            kind, ref = c["ref"].split(":", 1)
            resp = reader.api.open_thread(c["dealer"], topic={"buy": {kind: ref}})
            rec["thread"] = resp.get("id")
            led["threads"].append(resp.get("id"))
    except BazaarError as e:
        rec["status"] = "rejected" if 400 <= e.status < 500 else "ambiguous"
        rec["error"] = f"{e.code}: {e.message}"
        if c.get("neg") and c["neg"] in led["negotiations"]:
            n = led["negotiations"][c["neg"]]
            n["state"] = "ambigua" if rec["status"] == "ambiguous" else ("abandonada" if c["type"] == "team_open"
                                                                         else n.get("prev_state", "contactada"))
        if e.code in QUOTA and c.get("dealer"):
            led["blocked"][c["dealer"]] = e.extra.get("until_tick") or tick + 20
        save(led)
        print(f"   RECHAZADA  {describe(c)} · {rec['error']}" if rec["status"] == "rejected" else
              f"   AMBIGUA    {describe(c)} · {rec['error']} · no se reenvía hasta reconciliar")
        return rec
    rec.update(status="submitted" if c["type"] not in ("dealer_open", "dealer_close", "team_open", "team_close",
                                                       "team_propose") else "settled",
               response={k: resp.get(k) for k in ("id", "status", "queued", "offer", "created_tick", "expires_tick",
                                                   "settles_at_tick") if k in resp},
               offer_id=resp.get("id") if c["type"] in ("list", "bid") else c.get("offer"))
    save(led)
    extra = f" · caduca en el tick {resp.get('expires_tick')} (pedidos {rec.get('requested_ticks')}, efectivos " \
            f"{rec.get('effective_ticks')})" if c["type"] in ("list", "bid") else ""
    print(f"   ENVIADA    {describe(c)} · respuesta {rec['response']}{extra}")
    return rec


def observed(a, s, flags):
    b, n = a.get("before") or {}, metrics(s)
    uncertain = []
    if b.get("round") != n.get("round"):
        uncertain.append("cambió la ronda")
    if flags:
        uncertain.append("hubo actividad no registrada con la clave")
    uncertain.append("el leaderboard se refresca cada pocos minutos")
    fields = ("cash", "collection_value", "cards", "score", "neg_points", "ladder_points", "rank")
    return " · ".join(f"{k} {b.get(k)}→{n.get(k)}" for k in fields) + " · atribución INCIERTA: " + "; ".join(uncertain)


# ------------------------------------------------------------------ ciclo

def cycle(reader, args, led, journal, execute):
    s = snapshot(reader)
    tick, team = s["clock"]["tick"], s["me"]["id"]
    if execute and "since_tick" not in led:
        led["since_tick"] = tick
    fresh, ambiguous = reconcile(led, s, journal)
    own = led["actions"] + ma.load_json(ma.LEDGER).get("actions", [])
    accepts = [d for d in journal.records("decision") if d.get("action") in ("accept", "counter")]
    threads_known = set(led["threads"]) | {d.get("thread") for d in journal.records("decision")}
    sets = tr.settlements_for(s["feed"].get("events", []), team)
    flags = tr.unexplained_activity(sets, s["threads"]["open"] + s["threads"]["deal"], own, accepts, threads_known,
                                    led.get("since_tick", tick))
    for a in fresh:
        print(f"   LIQUIDADA  [{a.get('module', '?')}] {a.get('kind', a['type'])} {a.get('ref') or a.get('item')} · "
              f"{a.get('paid', a.get('price'))} P · "
              f"tick {a.get('settled_tick', tick)}")
        print(f"   RESULTADO OBSERVADO {observed(a, s, flags)}")
    cands, pl, exposure = candidates(s, led, args, journal)
    camp_cands, camp_lines = campaign_candidates(s, led, args, pl, execute)
    cands += camp_cands
    m = metrics(s)
    print(f"\n[{VERSION} · tick {tick} · ronda {m['round']}] efectivo {m['cash']} P · libre {pl['free_cash']} P · "
          f"reservado en ofertas {pl['reserved_cash']} P · pendiente {pl['pending_cash'] - exposure} P · expuesto con "
          f"vendedores {exposure} P · gasto confirmado {led['spent_confirmed']}/{args.max_spend} P · score {m['score']} "
          f"(neg {m['neg_points']}, ladder {m['ladder_points']}, rank {m['rank']}) · valor colección "
          f"{pl['model_value']}/{pl['server_value']} {'OK' if pl['valuation_verified'] else 'NO VERIFICADO'}")
    if flags:
        print("   AVISO actividad no registrada por este ordenador: " + "; ".join(flags))
    for line in camp_lines:
        print(f"   {line}")
    shown = sorted(cands, key=lambda c: (bool(c.get("blockers")), -c.get("score", 0)))
    for c in shown[:args.show]:
        tag = "CANDIDATA " if not c.get("blockers") else "descartada"
        print(f"   {tag} {describe(c)} · ΔU {c.get('du')} P · {c.get('reason') or '; '.join(c.get('notes') or c.get('why') or [])}"
              + (f" · BLOQUEO: {'; '.join(c['blockers'])}" if c.get("blockers") else ""))
    if len(shown) > args.show:
        print(f"   … {len(shown) - args.show} más en data/coordinator_report.json")
    pl_json = {k: v for k, v in pl.items() if k != "states"}
    if pl.get("states") is not None:
        pl_json["states"] = {r: st.short() for r, st in pl["states"].items() if st.bids or st.asks or st.copies != 1}
    ma.save(DATA / "coordinator_report.json", {"tick": tick, "metrics": m, "plan": pl_json, "candidates": cands,
                                               "flags": flags})
    if pl.get("states") is not None and getattr(args, "intel", 0):
        print(mi.intel_report(pl, limit=args.intel))
    max_posts = min(getattr(args, "max_posts", 1), s["clock"].get("limits", {}).get("offers_per_team_per_tick", 1))
    chosen = select(cands, led, tick, max_posts)
    for c in cands:
        c.pop("opp", None) if c.get("type") != "team_open" else None
    if ambiguous:
        print(f"   BLOQUEADO: {len(ambiguous)} escritura(s) ambigua(s) sin reconciliar; no se envía nada")
        return 0
    if flags and execute and not args.allow_concurrent:
        print("   BLOQUEADO: actividad no registrada con la clave (usa --allow-concurrent si sabes quién es)")
        return 0
    if not chosen:
        print("   Sin oportunidades válidas: no se opera este tick.")
        return 0
    for c in chosen:
        print(f"   SELECCIONADA {describe(c)} · ΔU {c.get('du')} P · motivo: {c.get('reason') or '; '.join(c.get('notes') or c.get('why') or [])}")
    if not execute:
        print("   (análisis: no se envía nada)")
        return 0
    if s["clock"].get("paused") or s["clock"].get("doors", "open") != "open" or s["me"].get("frozen"):
        print("   Juego cerrado o equipo congelado: no se envía nada")
        return 0
    sent = 0
    for c in chosen:
        if send(reader, led, s, c, args, journal):
            sent += 1
    led.setdefault("history", []).append(m)
    save(led)
    return sent


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true", help="enviar acciones (por defecto solo análisis)")
    p.add_argument("--ticks", type=int, default=1, help="duración en ticks (1-120)")
    p.add_argument("--mode", choices=["score", "acquire"], default="score",
                   help="score: nunca aceptar el precio de apertura de un vendedor; acquire: sí, tras un intento breve")
    p.add_argument("--max-spend", type=int, default=80, help="gasto confirmado máximo de la sesión (P)")
    p.add_argument("--reserve", type=int, default=100, help="efectivo que nunca se gasta (P)")
    p.add_argument("--per-card", type=int, default=60, help="coste máximo por carta (P)")
    p.add_argument("--margin", type=float, default=2.0, help="excedente mínimo por operación (P)")
    p.add_argument("--listing-ticks", type=int, default=10, help="vigencia EFECTIVA deseada de las publicaciones")
    p.add_argument("--fill-prior", type=float, default=0.3, help="SUPUESTO de ejecución de una publicación")
    p.add_argument("--allow-last-copy", default="", help="refs cuya última copia se puede vender")
    p.add_argument("--cancel-unsafe", action="store_true", help="cancelar publicaciones propias que violan la política")
    p.add_argument("--allow-concurrent", action="store_true", help="operar aunque haya actividad no registrada")
    p.add_argument("--show", type=int, default=12, help="candidatas a mostrar por tick")
    p.add_argument("--engine", choices=["intel", "basic"], default="intel",
                   help="intel: inteligencia de mercado multi-venue (Day 2); basic: solo El Rastro (anterior)")
    p.add_argument("--duende-venue", default="v02", help="venue de El Duende")
    p.add_argument("--duende-expiry", type=int, default=120, help="expires_in_ticks en El Duende (recomendación oficial)")
    p.add_argument("--max-posts", type=int, default=4, help="publicaciones/cancelaciones por tick (≤ límite del servidor)")
    p.add_argument("--intel", type=int, default=6, help="cartas a detallar en el informe de inteligencia (0 = ninguno)")
    p.add_argument("--history-window", type=int, default=240, help="ventana del historial de mercado en ticks")
    p.add_argument("--campaign", choices=["none", "all", "collect", "sell", "swap"], default="none",
                   help="campaña de negociación directa con equipos: abre conversaciones, envía propuestas y acepta")
    p.add_argument("--campaign-ticks", type=int, default=30, help="duración de la campaña en ticks")
    p.add_argument("--campaign-budget", type=int, default=60, help="efectivo máximo de la campaña (P)")
    p.add_argument("--max-proposals", type=int, default=3, help="propuestas nuestras por conversación")
    p.add_argument("--negotiation-ticks", type=int, default=6, help="ticks máximos por negociación")
    p.add_argument("--max-conversations", type=int, default=2, help="negociaciones de campaña activas a la vez")
    args = p.parse_args()
    if not 1 <= args.ticks <= 120:
        p.error("--ticks entre 1 y 120")
    DATA.mkdir(exist_ok=True)
    api = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"],
                 wait_on_tick=False, retries=0)
    reader = ma.Reader(api)
    journal = neg.Journal(str(DATA))
    lock = neg.InstanceLock(str(DATA / "agent.lock"), {"version": VERSION})
    if args.execute:
        others = other_processes()
        if others:
            raise SystemExit("Hay otro agente en marcha; el coordinador debe ser la única autoridad:\n   " +
                             "\n   ".join(others) + "\nPáralo (Ctrl+C o kill -INT <pid>) y vuelve a lanzar.")
        holder = lock.acquire()
        if holder:
            raise SystemExit(f"Bloqueo ocupado por pid {holder.get('pid')} ({holder.get('version')})")
    try:
        me = api.me()
        led = load_ledger(me["id"])
        for i in range(args.ticks if args.execute else 1):
            cycle(reader, args, led, journal, args.execute)
            if i + 1 < args.ticks and args.execute:
                api.wait_tick()
    finally:
        if args.execute:
            lock.release()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, BazaarError) as e:
        raise SystemExit(str(e))
    except KeyboardInterrupt:
        raise SystemExit("\nParado. Lo enviado está anotado en data/coordinator_ledger.json y se reconcilia al relanzar.")
