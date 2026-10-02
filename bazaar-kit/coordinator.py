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

import market_agent as ma
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
           "bid": "publicar", "cancel": "publicar", "dealer_open": "conversación", "dealer_close": "conversación"}
QUOTA = {"persona_quota", "cooloff", "sold_out", "locked"}


# ------------------------------------------------------------------ estado persistido

def load_ledger(team):
    led = ma.load_json(LEDGER)
    if led.get("team") not in (None, team):
        raise ValueError("El registro del coordinador pertenece a otro equipo")
    led.setdefault("team", team)
    for k, v in (("actions", []), ("spent_confirmed", 0), ("cash_received", 0), ("expiry_obs", []),
                 ("blocked", {}), ("class_tick", {}), ("threads", [])):
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
    market = [a for a in led["actions"] if a["type"] in ("accept", "list", "bid")]
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
    pl = tr.plan(s, cfg, pendings=pend, spent=led["spent_confirmed"])
    cap = max(0, min(pl["free_cash"], pl["budget_left"]))
    out = []
    # 1. Seguridad: publicaciones propias que venden la última copia o por debajo del valor.
    for c in tr.unsafe_own_offers(s["offers"].get("offers", []), team, val, counts):
        c.update(module="seguridad", du=0.0, score=10 ** 6, blockers=[] if args.cancel_unsafe else
                 ["requiere --cancel-unsafe (puede ser una oferta de un compañero)"])
        out.append(c)
    # 2. Mercado entre equipos.
    for o in pl["opportunities"]:
        o = dict(o, module="mercado")
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


def select(cands, led, tick):
    """Como mucho una acción por clase de límite y tick; dentro de cada clase, la de mayor prioridad."""
    chosen, used = [], set()
    for c in sorted((c for c in cands if not c.get("blockers") and c["type"] in CLASSES),
                    key=lambda c: -c.get("score", 0)):
        cls = CLASSES[c["type"]]
        if cls in used or led["class_tick"].get(cls) == tick:
            continue
        chosen.append(c)
        used.add(cls)
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
    if c["type"] == "accept":
        o = next((x for x in s["board"].get("offers", []) + s["offers"].get("offers", []) if x.get("id") == c["offer"]), {})
        rec["receive_assets"] = [x["id"] for x in (o.get("give") or {}).get("assets") or [] if isinstance(x, dict)]
    led["actions"].append(rec)
    led["class_tick"][CLASSES[c["type"]]] = tick
    save(led)  # antes de escribir en red
    ratio, basis = tr.expiry_ratio(led["expiry_obs"] + EXPIRY_EVIDENCE, s["clock"].get("tick_seconds") or 60.0)
    try:
        if c["type"] == "accept":
            resp = reader.api.accept(c["offer"], assets=c.get("assets") or None)
        elif c["type"] in ("list", "bid"):
            req = tr.listing_request(args.listing_ticks, ratio)
            rec["requested_ticks"], rec["expiry_basis"] = req, basis
            give, want = ({"assets": [c["asset"]]}, {"cash": c["price"]}) if c["type"] == "list" else \
                ({"cash": c["price"]}, {"cards": [c["ref"]]})
            resp = reader.api.list_offer(give, want, venue="rastro", expires_in_ticks=req)
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
        else:  # dealer_open
            kind, ref = c["ref"].split(":", 1)
            resp = reader.api.open_thread(c["dealer"], topic={"buy": {kind: ref}})
            rec["thread"] = resp.get("id")
            led["threads"].append(resp.get("id"))
    except BazaarError as e:
        rec["status"] = "rejected" if 400 <= e.status < 500 else "ambiguous"
        rec["error"] = f"{e.code}: {e.message}"
        if e.code in QUOTA and c.get("dealer"):
            led["blocked"][c["dealer"]] = e.extra.get("until_tick") or tick + 20
        save(led)
        print(f"   RECHAZADA  {describe(c)} · {rec['error']}" if rec["status"] == "rejected" else
              f"   AMBIGUA    {describe(c)} · {rec['error']} · no se reenvía hasta reconciliar")
        return rec
    rec.update(status="submitted" if c["type"] not in ("dealer_open", "dealer_close") else "settled",
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
    m = metrics(s)
    print(f"\n[{VERSION} · tick {tick} · ronda {m['round']}] efectivo {m['cash']} P · libre {pl['free_cash']} P · "
          f"reservado en ofertas {pl['reserved_cash']} P · pendiente {pl['pending_cash'] - exposure} P · expuesto con "
          f"vendedores {exposure} P · gasto confirmado {led['spent_confirmed']}/{args.max_spend} P · score {m['score']} "
          f"(neg {m['neg_points']}, ladder {m['ladder_points']}, rank {m['rank']}) · valor colección "
          f"{pl['model_value']}/{pl['server_value']} {'OK' if pl['valuation_verified'] else 'NO VERIFICADO'}")
    if flags:
        print("   AVISO actividad no registrada por este ordenador: " + "; ".join(flags))
    shown = sorted(cands, key=lambda c: (bool(c.get("blockers")), -c.get("score", 0)))
    for c in shown[:args.show]:
        tag = "CANDIDATA " if not c.get("blockers") else "descartada"
        print(f"   {tag} {describe(c)} · ΔU {c.get('du')} P · {c.get('reason') or '; '.join(c.get('notes') or c.get('why') or [])}"
              + (f" · BLOQUEO: {'; '.join(c['blockers'])}" if c.get("blockers") else ""))
    if len(shown) > args.show:
        print(f"   … {len(shown) - args.show} más en data/coordinator_report.json")
    ma.save(DATA / "coordinator_report.json", {"tick": tick, "metrics": m, "plan": pl, "candidates": cands,
                                               "flags": flags})
    chosen = select(cands, led, tick)
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
