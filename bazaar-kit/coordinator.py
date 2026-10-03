"""Coordinador único de The Bazaar: una sola autoridad que observa, decide y envía, con un presupuesto compartido.

    ./run.sh coord                                            # análisis (por defecto): solo lecturas
    ./run.sh coord --execute --ticks 20 --max-spend 80 --reserve 100 --per-card 60
    ./run.sh coord --deny-teams t12,t13,t14 --deny-margin 15 --pilar-sell SAL:1.25 --pilar-last-copy --ladder-fill
    ./run.sh coord ... --ladder-fill --ladder-calibrated [--ladder-profile perfil.json]   # escalera calibrada (opt-in)

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
import capital as ca
import market_agent as ma
import market_intel as mi
import negotiation as neg
import news_watch as nw
import intelligence as intel_mod
import ladder_calibrated as lcal
import page_campaign as pc
import ladder_plus as lplus
import page_guard as pg
import performance as perf
import team_sale as ts
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
           "team_close": "cierre", "swap_list": "publicar", "dealer_sell_open": "conversación",
           "dealer_sell_counter": "mensaje", "dealer_sell_accept": "aceptar"}
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

SLOW_TTL = {"catalog": 30, "levels": 10, "leaderboard": 10, "dealer": 5, "venues": 2}  # ticks
INVALIDATE = ("release", "level", "persona", "venue", "patch", "unlock", "catalog", "fee")


class SlowCache:
    """Caché por ticks de recursos que cambian despacio (catálogo, niveles, metadatos de vendedores y venues, equipos).
    Lo que cambia deprisa (me, ofertas, tablones, hilos, reloj, feed) se lee SIEMPRE. Un evento del feed de lanzamiento,
    nivel, vendedor, venue o comisión invalida toda la caché."""

    def __init__(self):
        self.data, self.last_tick = {}, None

    def get(self, reader, tick, method, *a):
        key, ttl = (method,) + a, SLOW_TTL.get(method, 0)
        hit = self.data.get(key)
        if hit is not None and ttl and tick - hit[0] < ttl:
            return hit[1]
        v = reader.call(method, *a)
        self.data[key] = (tick, v)
        return v

    def invalidate_from(self, events, tick):
        since, self.last_tick = self.last_tick, tick
        if since is None:
            return False
        for e in events or []:
            kind = str(e.get("type") or e.get("kind") or e.get("event") or "").lower()
            if int(e.get("tick") or 0) >= since and any(k in kind for k in INVALIDATE):
                self.data.clear()
                return True
        return False


def snapshot(reader, cache=None):
    cache = cache if cache is not None else SlowCache()
    clock = reader.call("clock")
    tick = int(clock.get("tick") or 0)
    feed = reader.call("feed", 400)
    cache.invalidate_from(feed.get("events", []), tick)
    s = {"clock": clock, "feed": feed, "catalog": cache.get(reader, tick, "catalog"), "me": reader.call("me"),
         "venues": cache.get(reader, tick, "venues"), "board": reader.call("board"), "offers": reader.call("my_offers")}
    s["threads"] = {st: reader.call("my_threads", st).get("threads", []) for st in ("open", "deal")}
    s["team_threads"] = [t for t in s["threads"]["open"] if t.get("kind") != "persona"]
    s["levels"] = cache.get(reader, tick, "levels")
    s["leaderboard"] = cache.get(reader, tick, "leaderboard")  # IDs de equipo válidos para abrir conversaciones
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
            s["dealers"][d] = cache.get(reader, tick, "dealer", d)
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
                # un hilo solo liquida UNA compra: el dealer_open del mismo hilo (marcado settled al abrir) no cuenta
                if paid is not None and not any(x.get("thread") == a["thread"] and x["status"] == "settled"
                                                and x["type"] in ("dealer_accept", "dealer_counter")
                                                for x in led["actions"] if x is not a):
                    a.update(status="settled", paid=paid, settled_tick=tick)
                    led["spent_confirmed"] += paid
                    journal.append("outcome", {"dealer": a["dealer"], "item": a["item"], "thread": a["thread"],
                                               "status": "deal", "close_price": paid, "settled": True,
                                               "context": "normal", "opening": a.get("opening"),
                                               "note": f"coordinador {VERSION}"})
                elif a["type"] == "dealer_accept" and tick > a["tick"] + 3:
                    # hilo en "deal" sin oferta marcada settled: la carta en el inventario es la evidencia
                    ref = (a.get("item") or "")[5:] if str(a.get("item") or "").startswith("card:") else None
                    held = ref and any(x.get("ref") == ref for x in s["me"]["assets"])
                    if held:
                        a.update(status="settled", paid=a.get("price"), settled_tick=tick,
                                 note="liquidada por inventario (hilo deal sin oferta settled)")
                        led["spent_confirmed"] += int(a.get("price") or 0)
                    else:
                        a["status"] = "released"
                else:
                    a["status"] = "released" if a["type"] == "dealer_counter" else a["status"]
            elif t is None or t.get("status") != "open":
                a["status"] = "released"  # conversación terminada sin trato
            elif a["type"] == "dealer_counter" and tick > a["tick"] + 1:
                a["status"] = "released"  # superada por la respuesta del vendedor
        if a["type"] in ("dealer_sell_accept", "dealer_sell_counter") and a["status"] in ("submitted", "ambiguous",
                                                                                          "intent"):
            t = threads.get(a["thread"])  # VENTA a un vendedor (--dealer-sell-dups): cobramos su give.cash
            if t and t.get("status") == "deal":
                got = neg.settled_sell_price(t, a["dealer"])
                if got is not None and not any(x.get("thread") == a["thread"] and x["status"] == "settled"
                                               and x["type"] in ("dealer_sell_accept", "dealer_sell_counter")
                                               for x in led["actions"] if x is not a):
                    a.update(status="settled", price=got, received=got, settled_tick=tick)
                    led["cash_received"] += got
                    journal.append("outcome", {"dealer": a["dealer"], "item": a["item"], "thread": a["thread"],
                                               "status": "deal", "side": "sell", "close_price": got, "settled": True,
                                               "context": "normal", "opening": a.get("opening"),
                                               "note": f"coordinador {VERSION}"})
                else:
                    a["status"] = "released" if a["type"] == "dealer_sell_counter" else a["status"]
            elif t is None or t.get("status") != "open":
                a["status"] = "released"
            elif a["type"] == "dealer_sell_counter" and tick > a["tick"] + 1:
                a["status"] = "released"
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


NEWS_CAL: dict = {}  # --news-db: calibración de fuentes (news_watch.calibrate), una vez por proceso
INTEL_STATE = {"intel": None}  # la capa de inteligencia la abre el ciclo; nunca ejecuta operaciones


def capital_cfg(args):
    return ca.CapitalConfig(hard_reserve=args.reserve, dealer_idle_liquidity=getattr(args, "dealer_liquidity", 40),
                            min_cancel_gain=getattr(args, "min_cancel_gain", 2.0),
                            capital_rebalance_threshold=getattr(args, "rebalance_threshold", 20),
                            stale_age_ticks=getattr(args, "stale_age", 30),
                            tactical_cash_buffer=getattr(args, "tactical_buffer", 30),
                            max_passive_cash_fraction=getattr(args, "max_passive_frac", 0.5),
                            dealer_cash_buffer_mode=getattr(args, "dealer_buffer_mode", "active_max"))


def deny_cfg(args):
    """--deny-teams / --deny-margin (opt-in): (equipos denegados, excedente mínimo o None)."""
    return lplus.parse_deny_teams(getattr(args, "deny_teams", "")), getattr(args, "deny_margin", None)


def calibrated_on(args):
    """--ladder-calibrated (opt-in): escalera calibrada con los hilos reales (ladder_calibrated.py)."""
    return bool(getattr(args, "ladder_calibrated", False))


_CAL_PROFILES = {}


def cal_profiles(args):
    """Perfiles medidos + --ladder-profile FICHERO.json (se lee una vez por ruta)."""
    path = getattr(args, "ladder_profile", None)
    if path not in _CAL_PROFILES:
        _CAL_PROFILES[path] = lcal.load_profiles(path)
    return _CAL_PROFILES[path]


def pilar_cfg(args):
    """--pilar-sell SET[:MULT] (opt-in); --ladder-fill / --ladder-calibrated sin --pilar-sell venden a Pilar cualquier
    barrio."""
    cfg = lplus.parse_pilar_sell(getattr(args, "pilar_sell", None))
    if cfg is None and (getattr(args, "ladder_fill", False) or calibrated_on(args)):
        cfg = lplus.PilarConfig(sets=frozenset())
    if cfg is not None:
        cfg.from_tick, cfg.until_tick = getattr(args, "pilar_from_tick", None), getattr(args, "pilar_until_tick", None)
        cfg.dealer = getattr(args, "pilar_id", None) or lplus.PILAR
        cfg.last_copy = bool(getattr(args, "pilar_last_copy", False))
        for k, a in (("open_ask", "pilar_open"), ("step", "pilar_step")):
            if getattr(args, a, None) is not None:
                setattr(cfg, k, int(getattr(args, a)))
    return cfg


def dealer_context(s, led, journal, val, counts, args):
    """Escalera (tratos que puntúan por vendedor), nuestras negociaciones activas con su máximo ECONÓMICO (sin capital)
    y el mejor máximo económico de una apertura viable (para la liquidez de vendedores cuando no hay ninguna activa)."""
    deal = [t for t in s["threads"]["deal"] if t.get("kind") == "persona"]
    open_dealer = [t for t in s["threads"]["open"] if t.get("kind") == "persona"]
    outcomes = journal.records("outcome")
    names = {n for n in set(s.get("dealers") or {}) | {t.get("with") for t in open_dealer + deal} if n}
    if pilar_cfg(args) is not None:  # opt-in: las ventas negociadas también llenan la escalera de cada vendedor
        buys = [t for t in deal if not lplus.is_sell_topic(t.get("topic"))]
        ladder = {d: neg.qualifying_deals(d, buys, [o for o in outcomes if o.get("side") != "sell"]) +
                  lplus.qualifying_sell_deals(d, deal, outcomes) for d in sorted(names)}
    else:
        ladder = {d: neg.qualifying_deals(d, deal, outcomes) for d in sorted(names)}
    mine = set(led["threads"]) | {d.get("thread") for d in journal.records("decision")}
    active = []
    for t in open_dealer:
        if t["id"] not in mine or "sell" in (t.get("topic") or {}):
            continue  # las conversaciones de VENTA (--dealer-sell-dups) no son compras: van por sell_thread_candidates
        item = neg.item_of(t.get("topic"))
        value = val.next_copy(counts, item[5:]) if item and item.startswith("card:") else None
        active.append({"thread": t, "item": item, "value": value,
                       "econ": math.floor(min(args.per_card, (value or 0) - args.margin))})
    viable = 0
    for did, dealer in (s.get("dealers") or {}).items():
        if not (did in (s["me"].get("unlocked") or []) or dealer.get("open_to_all")) or led["blocked"].get(did, 0) > \
                s["clock"]["tick"]:
            continue
        for row in (dealer.get("menu") or {}).get("sells", []):
            lp = row.get("list_price")
            if "rarity" not in row or not lp:
                continue
            for ref, c in val.cards.items():
                if c["released"] and c["rarity"] == row["rarity"] and not counts.get(ref) and not c.get("hidden"):
                    econ = math.floor(min(args.per_card, val.next_copy(counts, ref) - args.margin))
                    if econ >= lp * neg.dealer_policy(did, args.mode).min_viable_frac:
                        viable = max(viable, econ)
    return ladder, open_dealer, mine, active, viable


def candidates(s, led, args, journal):
    team, tick = s["me"]["id"], s["clock"]["tick"]
    me, val = s["me"], tr.Valuation(s["catalog"], s["me"].get("affinity") or {})
    counts = tr.counts_of(me["assets"])
    ccfg = capital_cfg(args)
    fill = bool(getattr(args, "ladder_fill", False))
    cal = calibrated_on(args)
    ladder_on = bool(getattr(args, "dealer_ladder", False)) or fill or cal  # --ladder-fill usa la escalera de #8
    lc = ladder_cfg(args)
    if getattr(args, "news_sell", False):  # sell_thread_candidates sigue las ventas abiertas por una noticia
        s["news_floors"] = led.get("news_floor") or {}
    s["mirror_dealers"] = mirror_dealers(s, led, args)
    pend_actions = [a for a in led["actions"]
                    if a["type"] in ("accept", "dealer_accept", "team_accept") and a["status"] in ("intent", "ambiguous", "submitted")]
    pend = [{"cost": a.get("cost", 0), "assets": a.get("assets") or []} for a in pend_actions if a["type"] != "team_accept"]
    exposure = dealer_exposure(s, led["threads"])
    # Las contraofertas a vendedores ya aparecen en /api/me/offers (con `thread`): no contarlas dos veces.
    thread_cash = sum(int((o.get("give") or {}).get("cash") or 0) for o in s["offers"].get("offers", [])
                      if o.get("maker") == team and o.get("thread") and o.get("status") in tr.OPEN_STATES)
    pend.append({"cost": max(0, exposure - thread_cash)})
    # Capital: la liquidez de vendedores que aún no está expuesta se aparta ANTES de planificar el mercado, así ninguna
    # puja pasiva (ni compra de mercado) puede consumirla. La reserva dura sigue siendo intocable.
    ladder, open_dealer, mine_threads, active, viable = dealer_context(s, led, journal, val, counts, args)
    if ccfg.dealer_cash_buffer_mode == "off":
        target = 0
    elif ccfg.dealer_cash_buffer_mode == "fixed":
        target = ccfg.dealer_idle_liquidity
    else:
        target = ca.dealer_liquidity_target([a["econ"] for a in active], min(ccfg.dealer_idle_liquidity, viable))
    if fill:  # --ladder-fill: la caja del siguiente trato de cada escalera incompleta va antes que las pujas pasivas
        target = max(target, ladder_fill_need(s, led, args, lc, val, counts, ladder, active))
    dealer_need = max(0, target - exposure)
    market_reserve = args.reserve + dealer_need
    # Capital ANTES de planificar: las pujas abiertas cuentan enteras (obligaciones reales, nunca × P(ejecución)).
    res0 = tr.resources(s["offers"].get("offers", []), team, pend)
    budget0 = args.max_spend - led["spent_confirmed"]
    view = ca.capital_view(me["cash"], args.reserve, max(0, res0.reserved_cash - thread_cash), exposure,
                           max(0, res0.pending_cash - max(0, exposure - thread_cash)), target, budget0,
                           ccfg.tactical_cash_buffer, ccfg.max_passive_cash_fraction)
    cfg = tr.Config(reserve=market_reserve, margin=args.margin, per_card=args.per_card, max_spend=args.max_spend,
                    listing_ticks=args.listing_ticks, fill_prior=args.fill_prior,
                    allow_last_copy=frozenset(x for x in args.allow_last_copy.split(",") if x))
    icfg = None
    if getattr(args, "engine", "basic") == "intel":
        icfg = mi.IntelConfig(reserve=market_reserve, margin=args.margin, per_card=args.per_card,
                              max_spend=args.max_spend, allow_last_copy=cfg.allow_last_copy,
                              duende_venue=args.duende_venue, duende_expiry_ticks=args.duende_expiry,
                              target_expiry_ticks=args.listing_ticks, history_window_ticks=args.history_window)
        ratio, _ = tr.expiry_ratio(led["expiry_obs"] + EXPIRY_EVIDENCE, s["clock"].get("tick_seconds") or 60.0)
        hist = mi.History(str(DATA / "market_history.jsonl"), args.history_window).load(s["clock"]["tick"])
        pl = mi.plan(s, icfg, pendings=pend, spent=led["spent_confirmed"], actions=led["actions"], history=hist,
                     expiry_ratio=ratio, passive_cap=view.free_market_cash)
        books, _ = mi.build_books(s, mi.venues_from(s))
        hist.record(s["clock"]["tick"], books, mi.public_settlements(s["feed"].get("events", [])), time.time())
    else:
        pl = tr.plan(s, cfg, pendings=pend, spent=led["spent_confirmed"])
    scored = ca.score_open_bids(s, pl["states"], icfg, led["actions"]) if icfg and pl.get("states") else []
    used = set()  # pujas ya asignadas a una cancelación de rebalanceo en este tick
    out = []
    # 0. PRIORIDAD 1 — protección de páginas completas: una oferta propia abierta que rompería una página completa se
    #    retira sin esperar a --cancel-unsafe (restricción dura, no depende de la valoración).
    protect = pg.unsafe_open_offers(s["offers"].get("offers", []), team, counts, s["catalog"], me["assets"])
    for c in protect:
        out.append(dict(c, module="seguridad", blockers=[]))
    # 0b. Un activo físico en dos obligaciones de entrega: se resuelve ya, de forma conservadora.
    for c in pg.exposure_conflicts(s["offers"].get("offers", []), team, pend_actions, me["assets"]):
        out.append(dict(c, module="seguridad"))
    # 0c. Una sola vía por carta buscada (varias pujas o trueques por la misma carta = duplicados y últimas copias).
    swap_du = lambda o: tr.evaluate_own_open_offer(o, val, counts)["du"]
    for c in pg.duplicate_pursuit_cancels(s["offers"].get("offers", []), team, swap_du):
        if c["offer"] not in {x["offer"] for x in out}:
            out.append(dict(c, module="seguridad"))
    protected_offers = {c["offer"] for c in out}
    # 1. Seguridad: publicaciones propias que venden la última copia o con ΔU < 0 (evaluador canónico).
    for c in tr.unsafe_own_offers(s["offers"].get("offers", []), team, val, counts):
        if c["offer"] in protected_offers:
            continue
        c.update(module="seguridad", du=0.0, score=10 ** 6, blockers=[] if args.cancel_unsafe else
                 ["requiere --cancel-unsafe (puede ser una oferta de un compañero)"])
        out.append(c)
    # 2. Mercado entre equipos (sin duplicar lo que una negociación de campaña activa ya persigue).
    camp_in = {n.get("receive") for n in led.get("negotiations", {}).values() if n["state"] in cp.ACTIVE} - {None}
    camp_out = {n.get("deliver") for n in led.get("negotiations", {}).values() if n["state"] in cp.ACTIVE} - {None}
    for o in pl["opportunities"]:
        if o["type"] == "cancel" and o.get("offer") in protected_offers:
            continue  # ya está la cancelación de seguridad
        o = dict(o, module="mercado")
        gets = set(o.get("receive") or {}) | ({o["ref"]} if o["type"] == "bid" else set())
        gives = set(o.get("deliver") or {}) | ({o["ref"]} if o["type"] == "list" else set())
        if gets & camp_in or gives & camp_out:
            o["blockers"] = list(o["blockers"]) + ["la campaña ya negocia esa carta con un equipo"]
        if o["type"] == "accept":
            o["score"] = 10 ** 4 + o["du"]
        out.append(o)
    # 3. Vendedores: conversaciones abiertas que son nuestras (no se tocan las ajenas). SECURE/OPTIMIZE por escalera.
    diag = {}
    rel = ca.releasable(scored, ccfg)
    cal_plan = ladder_plan(s, led, args, val, counts, ladder, view.free_dealer_cash + rel) if cal else None
    planned = {(o.dealer, o.mode, o.ref) for o in (cal_plan or {}).get("picks", [])}
    for t in open_dealer:
        if t["id"] not in mine_threads:
            out.append({"type": "info", "module": "vendedores", "kind": "conversación ajena", "thread": t["id"],
                        "ref": str(t.get("topic")), "du": 0, "score": -1, "blockers": ["no es nuestra: no se toca"]})
        elif "sell" in (t.get("topic") or {}):
            out += news_floor_guard(sell_thread_candidates(s, t, args, lc, val, counts), led, args)
    for a in active:
        t, item, value = a["thread"], a["item"], a["value"]
        did = t["with"]
        n_q = len(ladder.get(did, []))
        mode = neg.ladder_mode(n_q)
        camp = getattr(args, "page_campaign", "none")
        if item and item.startswith("card:") and camp in val.pages and item[5:] in val.pages[camp] \
                and not counts.get(item[5:]):
            mode = "SECURE"  # carta de la campaña de página: no se arriesga por ahorrar 1-3 P
        pol = neg.policy_for(did, args.mode, 0 if mode == "SECURE" else n_q)
        avail = view.free_dealer_cash + exposure  # lo que el vendedor puede cobrar YA (incluida nuestra oferta vigente)
        ceiling = max(0, math.floor(min(a["econ"], avail + rel)))  # capital liberable cancelando pujas débiles
        st = neg.state_from_thread(t, did, tick, neg.Config())
        if st.opening is not None and item:  # memoria: su precio de apertura por carta (para no reabrir en balde)
            led.setdefault("dealer_openings", {})[f"{did}|{item}"] = {"opening": st.opening, "tick": tick}
        if ladder_on:  # --dealer-ladder: política observada (sustituye a decide_dealer, también en SECURE)
            pol = neg.ladder_profile(did, lc, args.mode)
            notes_mode = "escalera observada"
            rarity = val.cards.get(item[5:], {}).get("rarity") if item and item.startswith("card:") else None
            if cal:  # --ladder-calibrated: apertura extrema, paso corto, paciencia hasta final:true
                key, cp_ = lcal.profile_for(cal_profiles(args), did, "buy", rarity or (item or "")[5:])
                left = cp_.max_ticks - neg.conversation_ticks_used(t, tick)
                mirror = is_mirror(s, args, t, did)
                d = lcal.decide_buy(st, cp_, ceiling, left, tick, name=f"calibrada {key}" + (" espejo" if mirror else ""),
                                    mirror=mirror)
                notes_mode = f"escalera calibrada {key} ({cp_.obs})" + (
                    " · FAROL: dice «final» sin final:true, seguimos regateando" if lcal.bluff_final(t, did) else "")
            else:
                left = pol.max_ticks - neg.conversation_ticks_used(t, tick)
                d = neg.decide_ladder(st, pol, ceiling, left, rarity, tick)
        else:
            notes_mode = None
            left = pol.max_ticks - neg.conversation_ticks_used(t, tick)
            d = neg.decide_dealer(st, pol, ceiling, left)
        kind = {"counter": "dealer_counter", "accept": "dealer_accept", "abandon": "dealer_close"}.get(d.action)
        ttl = (st.live.expires - tick) if st.live and st.live.expires is not None else None
        blockers, notes = [], [f"política {pol.name} modo {args.mode} · escalera {n_q}/{neg.LADDER_SLOTS} → {mode}"
                               + (f" · {notes_mode}" if notes_mode else "")]
        if kind in ("dealer_accept", "dealer_counter") and d.price and d.price > avail:
            need = d.price - avail
            cancels, _, why = ca.rebalance(scored, need, (value or 0) - d.price, ccfg, f"{did} {item}", used)
            out += [dict(c, module="capital") for c in cancels]
            used |= {c["offer"] for c in cancels}
            if kind == "dealer_accept" or not cancels:
                blockers.append(f"capital: faltan {need} P · {why}" +
                                ("; se acepta cuando el servidor confirme las cancelaciones" if cancels else ""))
            else:
                notes.append(why)
        if kind == "dealer_accept":
            off = neg.find_offer(t, d.offer_id)
            blockers += neg.validate_offer(off, dealer=did, team=team, thread_id=t["id"], item=item, ceiling=a["econ"],
                                           cash=me["cash"], now_tick=tick,
                                           resolve_asset=lambda x: f"card:{x.get('ref')}" if isinstance(x, dict) else None) \
                if off else ["oferta del vendedor no encontrada en el hilo"]
        if kind:
            out.append({"type": kind, "module": "vendedores", "kind": f"{did}: {d.action} [{mode}]", "thread": t["id"],
                        "dealer": did, "item": item, "ref": item, "price": d.price, "offer": d.offer_id,
                        "opening": st.opening, "ceiling": ceiling, "du": round((value or 0) - (d.price or 0), 2),
                        "dv": value, "cash": -(d.price or 0), "ladder_mode": mode,
                        "score": neg.dealer_accept_priority(mode, n_q, ttl) if kind == "dealer_accept" else 10 ** 5,
                        "reason": d.reason, "blockers": blockers, "turns": st.turns, "notes": notes,
                        **({"ladder": True} if ladder_on else {})})
        diag[did] = dealer_diag(did, ladder.get(did, []), mode, t, st, ceiling, a["econ"], avail, left, ttl, d,
                                blockers, pol)
    # 4. Vendedores: abrir una conversación por una carta ausente que venden.
    open_count = len(s["threads"]["open"])
    busy = {t["with"] for t in open_dealer}
    busy_items = {neg.item_of(t.get("topic")) for t in open_dealer} - {None}  # una carta, un vendedor a la vez
    for did, dealer in s["dealers"].items():
        unlocked = did in (me.get("unlocked") or []) or dealer.get("open_to_all")
        sells = (dealer.get("menu") or {}).get("sells", [])
        n_q = len(ladder.get(did, []))
        mode = neg.ladder_mode(n_q)
        bonus = (5000 if n_q == neg.LADDER_SLOTS - 1 else 2000) if mode == "SECURE" else 0  # prioridad, no valor
        camp_set = getattr(args, "page_campaign", "none")
        for row in sells:
            if "rarity" not in row:
                continue
            for ref, c in val.cards.items():
                if not c["released"] or c["rarity"] != row["rarity"] or counts.get(ref) or c.get("hidden"):
                    continue
                value = val.next_copy(counts, ref)
                ceiling = math.floor(min(args.per_card, view.free_dealer_cash + rel, value - args.margin))
                lp = row.get("list_price")
                blockers = []
                viable = (neg.ladder_profile(did, lc, args.mode) if ladder_on else neg.dealer_policy(did, args.mode)
                          ).min_viable_frac
                prefer = lc.route.get(c["rarity"]) if ladder_on else None
                if prefer and prefer != did and dealer_available(s, prefer) and any(
                        r.get("rarity") == c["rarity"] for r in (s["dealers"][prefer].get("menu") or {}).get("sells", [])):
                    blockers.append(f"enrutado a {prefer}: cierra {c['rarity']} más barato (escalera observada)")
                if not unlocked:
                    u = dealer.get("unlock") or {}
                    blockers.append(f"{did} no disponible aún (abre a todos en {u.get('open_to_all_at')}; antes con "
                                    f"{u.get('early_min_deals')} tratos negociados con {u.get('early_deals_with')})")
                if did in busy:
                    blockers.append(f"ya hay una conversación abierta con {did}")
                if f"card:{ref}" in busy_items:
                    blockers.append(f"ya negociamos {ref} con otro vendedor (dos cierres = un duplicado)")
                if led["blocked"].get(did, 0) > tick:
                    blockers.append(f"{did} bloqueado hasta el tick {led['blocked'][did]} (cupo o enfriamiento)")
                if open_count >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
                    blockers.append("sin conversaciones libres")
                if lp is None or ceiling < lp * viable:
                    blockers.append(f"máximo {ceiling} P frente a precio publicado {lp} P")
                seen_open = (led.get("dealer_openings") or {}).get(f"{did}|card:{ref}")
                if seen_open and ceiling < seen_open["opening"] * neg.dealer_policy(did, args.mode).min_viable_frac:
                    blockers.append(f"{did} abrió a {seen_open['opening']} P por {ref} (tick {seen_open['tick']}); nuestro "
                                    f"máximo {ceiling} P no llega: se reabre cuando valga más (p. ej. última carta)")
                out.append({"type": "dealer_open", "module": "vendedores", "kind": f"abrir con {did} [{mode}]",
                            "dealer": did, "ref": f"card:{ref}", "price": lp, "ceiling": ceiling,
                            "du": round(value - (lp or 0), 2),
                            "score": 10 ** 3 + bonus + value - (lp or 0) +
                            (lplus.fill_priority(did, c["rarity"]) if fill and n_q < neg.LADDER_SLOTS else 0) +
                            (3 * 10 ** 4 if (did, "buy", ref) in planned else 0) +
                            (5 * 10 ** 4 if camp_set not in (None, "none") and c.get("set") == camp_set else 0),
                            "blockers": blockers, "ladder_mode": mode,
                            "notes": [f"valor {value:.1f} P (con bono si completa página)",
                                      "precio real desconocido hasta su primera oferta",
                                      f"escalera {n_q}/{neg.LADDER_SLOTS}: cuenta solo si cerramos por debajo de su apertura"]})
        # 4b. --dealer-sell-menu (opt-in): abrir una VENTA de un excedente que ellos compran según su propio menú.
        # Es el canal que llena la escalera (paga cuota del rango capturado, no valor privado) y el único que no
        # necesita capital. Sin el flag no cambia nada (evita competir con --dealer-sell-dups/--pilar-sell/
        # --ladder-calibrated, que ya cubren la venta de duplicados con su propia calibración).
        for row in (dealer.get("menu") or {}).get("buys", []) if getattr(args, "dealer_sell_menu", False) else ():
            rarity = row.get("rarity")
            if not rarity:
                continue
            ok_sets = row.get("sets")
            for a in me["assets"]:
                ref = a.get("ref")
                if a.get("kind") != "card" or not ref:
                    continue
                c = val.cards.get(ref)
                if not c or c["rarity"] != rarity:
                    continue
                if isinstance(ok_sets, list) and ref.split("-")[0] not in ok_sets:
                    continue
                lost, page_notes = val.delta(counts, Counter(), Counter({ref: 1}))
                floor = neg.price_floor(lost, args.margin)
                blockers = []
                if any("ROMPERÍA" in n for n in page_notes) and ref not in cfg.allow_last_copy:
                    blockers.append(f"rompería una página completa: suelo {floor} P "
                                    f"(usa --allow-last-copy {ref} si de verdad interesa)")
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
                notes = [f"entregamos {abs(lost):.1f} P de valor privado: suelo {floor} P",
                         f"nivel {dealer.get('level')}: la escalera sólo cuenta los 3 mejores tratos de cada nivel",
                         "cuenta para la escalera sólo si cerramos por encima de su puja de apertura"] + page_notes
                if did == "picaros":
                    notes.append("ofertas con truco: validar los assets de cada oferta antes de aceptar (POST /api/flags)")
                out.append({"type": "dealer_sell_open", "module": "vendedores", "kind": f"vender {ref} a {did}",
                            "dealer": did, "ref": f"sell:{a['id']}", "asset": a["id"], "card": ref,
                            "price": None, "ceiling": floor, "du": 0.0,
                            "score": 10 ** 3 + (dealer.get("level") or 0) * 100 - abs(lost),
                            "blockers": blockers, "notes": notes})
        if did not in diag:
            opens = [c for c in out if c["type"] == "dealer_open" and c["dealer"] == did]
            best = max(opens, key=lambda c: (not c["blockers"], c["score"]), default=None)
            diag[did] = dealer_diag(did, ladder.get(did, []), mode, None, None, best and best["ceiling"], None,
                                    view.free_dealer_cash, None, None, None, best["blockers"] if best else
                                    ["no vende nada que nos falte"], None, best)
        for row in sells:
            if "pack" in row:
                pack = next((p for p in s["catalog"].get("packs", []) if p["id"] == row["pack"]), None)
                if pack:
                    mv = {r: st.market.value for r, st in (pl.get("states") or {}).items() if st.market.value}
                    pa = tr.pack_analysis(val, counts, pack, mv, draws=1500)
                    ev = pa["raw_collection_ev"]
                    out.append({"type": "info", "module": "vendedores", "kind": f"sobre {row['pack']} ({did})",
                                "ref": f"pack:{row['pack']}", "price": row.get("list_price"), "du": round(ev - row["list_price"], 1),
                                "pack_analysis": pa,
                                "score": -1, "blockers": [f"no se compra por rutina: RAW_COLLECTION_EV {ev} P · "
                                                          f"STRATEGIC_EV {pa['strategic_ev']} P · P(página nueva) "
                                                          f"{pa['p_new_page']} · P(duplicado) {pa['p_duplicate']} frente "
                                                          f"a {row['list_price']} P publicado ({pa['assumptions']}); "
                                                          "la suerte no puntúa"]})
    # 5. Oportunidad inmediata de mercado bloqueada SOLO por capital: se liberan pujas débiles (una por tick).
    blocked = [c for c in out if c["type"] == "accept" and ca.capacity_only(c) and ca.cost_of(c) <= view.budget_left]
    for c in sorted(blocked, key=lambda c: -c.get("du", 0))[:1]:
        need = ca.cost_of(c) - view.free_market_cash
        cancels, _, why = ca.rebalance(scored, need, c.get("du", 0), ccfg, f"comprar {c.get('receive')}", used)
        out += [dict(x, module="capital") for x in cancels]
        used |= {x["offer"] for x in cancels}
        c["blockers"] = list(c["blockers"]) + [why + ("; se ejecuta cuando el servidor confirme" if cancels else "")]
    # 6. Pujas obsoletas (razones duras siempre; blandas solo con capital de mercado escaso) y exceso de capital
    #    pasivo por encima del límite (se retiran las peores, no las más nuevas).
    stale = [c for c in ca.stale_bid_cancels(scored, view, ccfg, args.margin) if c["offer"] not in used]
    out += [dict(c, module="capital") for c in stale]
    used |= {c["offer"] for c in stale}
    out += [dict(c, module="capital") for c in ca.excess_cancels(scored, view, ccfg, used)]
    # 7. Venta táctica de duplicados a una contraparte real (p. ej. LAT-10 ~86 P).
    pl["tactical_sales"] = tactical_sales(s, led, args, val, pl, out)
    # 8. Compras dirigidas ordenadas por un humano.
    out += directed_buys(s, led, args, val, counts, view)
    # 9. Escalera opt-in: vender duplicados comunes a un vendedor y no pujar por lo que ya negociamos con uno.
    if getattr(args, "dealer_sell_dups", False) and not cal:
        out += sell_open_candidates(s, led, args, lc, val, counts, busy, open_count)
    if cal:  # --ladder-calibrated: ventas planificadas a la Abuela y al Chato (Pilar va por pilar_candidates)
        out += calibrated_sell_opens(s, led, cal_plan, busy, open_count)
    if getattr(args, "news_sell", False):  # --news-sell (opt-in): vender a un vendedor con demanda viva en las noticias
        out += news_sell_candidates(s, led, args, busy, open_count)
    if getattr(args, "dedupe_bids", False):
        out += dealer_bid_cancels(s, out, open_dealer, mine_threads)
    # 10. Campaña de completar página (objetivo actual: Malasaña).
    pl["page_campaign"] = page_campaign_step(s, led, args, val, counts, pl, view, scored, ccfg, used, out)
    # 10. --pilar-sell / --ladder-fill (opt-in): abrir una venta a Pilar; la copia en venta no sale por otra vía.
    pilar, pilar_report, selling_assets = pilar_candidates(s, led, args, val, counts, ladder, cal_plan)
    for c in out if selling_assets else ():
        if c["type"] not in pg.NON_DELIVERING and not str(c["type"]).startswith("dealer_sell") and \
                set(pg.delivery_of(c, s)[1]) & selling_assets:
            c["blockers"] = list(c.get("blockers") or []) + ["esa copia está en una conversación de venta a un vendedor"]
    if getattr(args, "fever_priority", False):  # --fever-priority (opt-in): ventas del barrio en fiebre, primero
        fever_priority(pilar, s, args)
    out += pilar
    out = dedupe_cancels(out)
    pl["capital"], pl["dealer_diag"], pl["open_bids"] = view.as_dict(), diag, scored
    pl["ladder"] = {d: len(x) for d, x in ladder.items()}
    if pilar_report is not None:
        pl["pilar"] = pilar_report
    if cal_plan is not None:
        pl["ladder_cal"] = {"filled": cal_plan["filled"], "free": cal_plan["free"], "gain": cal_plan["gain"],
                            "cash": cal_plan["cash"], "budget": cal_plan["budget"],
                            "picks": [lcal.describe(o) for o in cal_plan["picks"]]}
    return out, pl, exposure


def ladder_cfg(args):
    """Parámetros de la escalera (--dealer-ladder / --dealer-sell-dups); los de vendedores nuevos de nivel 3 y los de
    venta se ajustan por línea de órdenes."""
    lc = neg.LadderConfig()
    if getattr(args, "ladder_fill", False):
        lplus.top3_ladder_config(lc)  # --ladder-fill: aperturas de ESTRATEGIA_TOP3 (los flags --ladder-* mandan)
    for k in ("new_open", "new_gap_frac", "new_counters", "new_ticks", "new_max_frac", "sell_margin", "sell_open"):
        v = getattr(args, f"ladder_{k}", None)
        if v is not None:
            setattr(lc, k, v)
    return lc


def dealer_available(s, did):
    dealer = (s.get("dealers") or {}).get(did)
    return dealer is not None and (did in (s["me"].get("unlocked") or []) or bool(dealer.get("open_to_all")))


def sell_floor(val, counts, ref, lc):
    loss = -val.delta(counts, Counter(), Counter({ref: 1}))[0]
    return loss, math.ceil(loss + lc.sell_margin)


def sell_open_candidates(s, led, args, lc, val, counts, busy, open_count):
    """Abrir una conversación de venta de una copia sobrante con el vendedor que compra esa rareza."""
    me, tick = s["me"], s["clock"]["tick"]
    committed = committed_ids(s, led)
    committed_refs = pg.refs_of_assets(committed, me["assets"])
    selling = {a for t in s["threads"]["open"] if t.get("kind") == "persona"
               for a in neg.sell_assets_of(t.get("topic"))}
    out = []
    sell_route = dict(lc.sell_route)
    pilar_rule = getattr(args, "pilar_sell", None)
    pilar_set, pilar_factor = None, None
    pilar_window = (getattr(args, "pilar_from_tick", None), getattr(args, "pilar_until_tick", None))
    if pilar_rule:
        try:
            pilar_set, pilar_factor = pilar_rule.split(":", 1)
            pilar_factor = float(pilar_factor)
            if not pilar_set or pilar_factor <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("--pilar-sell debe tener formato SET:FACTOR positivo, por ejemplo SAL:1.25")
        if dealer_available(s, "pilar"):
            for rarity in ("uncommon", "rare", "epic"):
                sell_route[rarity] = "pilar"
    if pilar_rule and ((pilar_window[0] is not None and s["clock"]["tick"] < pilar_window[0]) or
                       (pilar_window[1] is not None and s["clock"]["tick"] > pilar_window[1])):
        sell_route = {r: d for r, d in sell_route.items() if d != "pilar"}
    for rarity, did in sell_route.items():
        expected = lc.sell_expected.get(did, {}).get(rarity)
        allow_last = {x.strip() for x in (getattr(args, "allow_last_copy", "") or "").split(",") if x.strip()}
        for ref, n in sorted(counts.items()):
            c = val.cards.get(ref)
            # duplicados, o la ÚLTIMA copia solo si está en --allow-last-copy (page_guard sigue protegiendo páginas)
            if not c or c["rarity"] != rarity or (n < 2 and ref not in allow_last) or val.unit(ref) is None:
                continue
            if did == "pilar" and (not pilar_set or c.get("set") != pilar_set):
                continue
            if pg.tradeable_surplus(ref, counts, s["catalog"], committed_refs) < 1:
                continue  # page_guard: solo copias por encima del mínimo protegido
            ids = [a["id"] for a in sorted(me["assets"], key=lambda a: a["id"]) if a.get("ref") == ref
                   and a["id"] not in committed and a["id"] not in selling]
            if not ids:
                continue
            loss, floor = sell_floor(val, counts, ref, lc)
            if did == "pilar" and pilar_factor is not None:
                expected = math.ceil(17.5 * pilar_factor)
                if expected < floor:
                    continue
            blockers = []
            if not dealer_available(s, did):
                blockers.append(f"{did} no disponible")
            if did in busy:
                blockers.append(f"ya hay una conversación abierta con {did}")
            if led["blocked"].get(did, 0) > tick:
                blockers.append(f"{did} bloqueado hasta el tick {led['blocked'][did]} (cupo o enfriamiento)")
            if open_count >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
                blockers.append("sin conversaciones libres")
            if expected is not None and expected < floor:
                blockers.append(f"su final observado ({expected} P) no llega al suelo de {floor} P")
            got = expected if expected is not None else floor
            out.append({"type": "dealer_sell_open", "module": "vendedores", "kind": f"vender a {did}", "dealer": did,
                        "ref": f"card:{ref}", "asset": ids[-1], "price": lc.sell_open, "floor": floor,
                        "du": round(got - loss, 2), "score": 500 + got - loss, "blockers": blockers,
                        "notes": [f"duplicado {rarity} ({n} copias), pierde {loss:.2f} P, suelo {floor} P",
                                  f"final observado de {did}: {expected} P" if expected else "sin final observado",
                                  "cuenta para la escalera si cerramos por encima de su apertura"]})
    return out


def sell_thread_candidates(s, t, args, lc, val, counts):
    """Siguiente paso en una conversación de VENTA nuestra a un vendedor."""
    tick, did = s["clock"]["tick"], t["with"]
    base = {"module": "vendedores", "thread": t["id"], "dealer": did, "blockers": []}
    pc = pilar_cfg(args)
    pilar = pc is not None and did == pc.dealer  # --pilar-sell: escalera propia de Pilar (pedir alto, bajar de 2 en 2)
    cal = calibrated_on(args) and (pilar or did in lcal.LADDER_DEALERS)
    news = getattr(args, "news_sell", False) and str(next(iter(neg.sell_assets_of(t.get("topic"))), "")) in \
        (s.get("news_floors") or {})
    if not pilar and not cal and not news and not getattr(args, "dealer_sell_dups", False):
        return [dict(base, type="info", kind=f"{did}: venta", ref=str(t.get("topic")), du=0, score=-1,
                     blockers=["conversación de venta: requiere --dealer-sell-dups"])]
    ids = neg.sell_assets_of(t.get("topic"))
    mine = {a["id"]: a for a in s["me"]["assets"]}
    if len(ids) != 1 or ids[0] not in mine:
        return [dict(base, type="dealer_close", kind=f"{did}: cerrar venta", ref=str(ids), du=0, score=10 ** 5,
                     reason="la copia ya no está en nuestras manos")]
    asset_id, ref = ids[0], mine[ids[0]]["ref"]
    st = neg.state_from_thread(t, did, tick, neg.Config(), side="sell")
    if cal:  # --ladder-calibrated: suelo = valor privado; objetivo = mejor trato observado; solo final:true cierra
        loss, floor = pilar_floor(val, counts, ref)
        key, cp_ = sell_profile(args, s, "pilar" if pilar else did, did, ref, val)
        mirror = is_mirror(s, args, t, did)
        d = lcal.decide_sell(st, cp_, floor, cp_.max_ticks - neg.conversation_ticks_used(t, tick), tick,
                             name=f"calibrada {key}" + (" espejo" if mirror else ""), mirror=mirror)
    elif pilar:
        loss, floor = pilar_floor(val, counts, ref)
        d = lplus.decide_sell(st, floor, lplus.first_ask(floor, pc), pc,
                              pc.max_ticks - neg.conversation_ticks_used(t, tick))
    else:
        loss, floor = sell_floor(val, counts, ref, lc)
        d = neg.decide_ladder_sell(st, lc, floor, lc.sell_ticks - neg.conversation_ticks_used(t, tick), args.mode)
    kind = {"counter": "dealer_sell_counter", "accept": "dealer_sell_accept", "abandon": "dealer_close"}.get(d.action)
    if not kind:
        return []
    c = dict(base, type=kind, kind=f"{did}: venta {d.action}", item=f"card:{ref}", ref=f"card:{ref}", price=d.price,
             offer=d.offer_id, opening=st.opening, floor=floor, du=round((d.price or 0) - loss, 2), score=10 ** 5,
             reason=d.reason, turns=st.turns, side="sell", notes=[f"venta escalera, suelo {floor} P"])
    if cal:
        c["notes"] = [f"--ladder-calibrated {key}: suelo = valor privado {loss:.1f} P → {floor} P; objetivo "
                      f"{cp_.target_price(st.opening, 'sell') if st.opening else '?'} P; {cp_.obs}"]
        if lcal.bluff_final(t, did):
            c["notes"].append("FAROL: dice «final» sin final:true; seguimos regateando")
        if kind == "dealer_sell_accept":
            c["score"] = 3 * 10 ** 5
    elif pilar:
        c["notes"] = [f"--pilar-sell: suelo = valor privado {loss:.1f} P → {floor} P; primera petición "
                      f"{lplus.first_ask(floor, pc)} P, pasos de {pc.step} P"]
        if kind == "dealer_sell_accept":
            c["score"] = 3 * 10 ** 5  # cerrar una venta que llena la escalera de nivel 3 antes que otras aceptaciones
    if kind != "dealer_close":
        c["asset"] = asset_id  # page_guard la revisa: entrega esta copia
    if kind == "dealer_sell_accept":
        o = neg.find_offer(t, d.offer_id) or {}
        c["blockers"] = neg.sell_offer_problems(o, dealer=did, asset_id=asset_id, floor=floor)
        c["cash"] = d.price
    return [c]


def pilar_floor(val, counts, ref):
    """--pilar-sell: suelo = valor privado de la copia que perdemos (aceptar su final si lo cubre)."""
    loss = -val.delta(counts, Counter(), Counter({ref: 1}))[0]
    return loss, max(1, math.ceil(loss - 1e-9))


def sell_profile(args, s, name, did, ref, val):
    """Perfil calibrado de VENTA de `ref` a un vendedor (Pilar: variante SAL/RET según su menú)."""
    card = val.cards.get(ref) or {}
    fav = lcal.is_fav(name, card.get("set"), (s.get("dealers") or {}).get(did))
    return lcal.profile_for(cal_profiles(args), name, "sell", card.get("rarity"), fav)


def dealer_buys(dealer, rarity, set_id):
    """¿Compra el vendedor esa rareza (y barrio, si su fila los enumera)?"""
    for row in ((dealer or {}).get("menu") or {}).get("buys", []):
        if row.get("rarity") == rarity and (not isinstance(row.get("sets"), list) or set_id in row["sets"]):
            return True
    return False


def ladder_plan(s, led, args, val, counts, ladder, budget):
    """--ladder-calibrated: qué tratos llenan mejor los huecos de la escalera (3 por vendedor; Pilar pesa más), con la
    captura esperada al precio objetivo de cada perfil, el excedente a valores privados y la caja disponible."""
    me, tick, cat = s["me"], s["clock"]["tick"], s["catalog"]
    pc = pilar_cfg(args)
    allow = {x.strip().upper() for x in str(getattr(args, "allow_last_copy", "") or "").split(",") if x.strip()}
    committed = committed_ids(s, led)
    selling = {a for t in s["threads"]["open"] if t.get("kind") == "persona" for a in neg.sell_assets_of(t.get("topic"))}
    dealers = {d: (s.get("dealers") or {}).get(d) for d in lcal.LADDER_DEALERS}
    usable = sorted(d for d, row in dealers.items() if row is not None and dealer_available(s, d)
                    and led["blocked"].get(d, 0) <= tick)
    opts = []
    for ref in sorted(counts):
        card = val.cards.get(ref) or {}
        if card.get("hidden") or not card.get("rarity"):
            continue
        set_id = card.get("set", "")
        free = [i for i in pg.tradeable_assets(ref, counts, cat, me["assets"], committed) if i not in selling]
        if not free:
            continue  # page_guard: mantiene una página completa (o ya está comprometida)
        loss, floor = pilar_floor(val, counts, ref)
        for d in usable:
            if not dealer_buys(dealers[d], card["rarity"], set_id):
                continue
            if d == lplus.PILAR and pc and not pc.covers(set_id):
                continue  # --pilar-sell SET: Pilar solo esos barrios
            if counts[ref] < 2 and not (ref.upper() in allow or set_id.upper() in allow or (
                    d == lplus.PILAR and pc and lplus.last_copy_allowed(ref.upper(), set_id.upper(), allow, pc))):
                continue  # última copia: --allow-last-copy REF|SET (y a Pilar también --pilar-last-copy)
            key, prof = sell_profile(args, s, d, d, ref, val)
            price = prof.expected_price("sell")
            if price >= floor:
                opts.append(lcal.Option(d, "sell", ref, free[-1], price, 0, round(price - loss, 2),
                                        prof.capture(int(prof.open_obs), price, "sell"), key))
    for d in usable:
        for row in (dealers[d].get("menu") or {}).get("sells", []):
            if "rarity" not in row:
                continue
            key, prof = lcal.profile_for(cal_profiles(args), d, "buy", row["rarity"])
            price = prof.expected_price("buy")
            for ref, c in val.cards.items():
                if not c["released"] or c["rarity"] != row["rarity"] or counts.get(ref) or c.get("hidden"):
                    continue
                value = val.next_copy(counts, ref)
                if value - args.margin >= price and price <= args.per_card:
                    opts.append(lcal.Option(d, "buy", ref, None, price, price, round(value - price, 2),
                                            prof.capture(int(prof.open_obs), price, "buy"), key))
    filled = {d: len(ladder.get(d, [])) for d in lcal.LADDER_DEALERS}
    cash = int(getattr(args, "ladder_budget", None) if getattr(args, "ladder_budget", None) is not None else budget)
    res = lcal.plan(opts, filled, cash, neg.LADDER_SLOTS)
    res.update(filled=filled, budget=cash, options=len(opts))
    return res


def calibrated_sell_opens(s, led, cal_plan, busy, open_count):
    """--ladder-calibrated: abrir la venta planificada a la Abuela o al Chato (una conversación por vendedor)."""
    tick, out = s["clock"]["tick"], []
    for o in (cal_plan or {}).get("picks", []):
        if o.mode != "sell" or o.dealer == lplus.PILAR:
            continue
        blockers = []
        if o.dealer in busy:
            blockers.append(f"ya hay una conversación abierta con {o.dealer}")
        if led["blocked"].get(o.dealer, 0) > tick:
            blockers.append(f"{o.dealer} bloqueado hasta el tick {led['blocked'][o.dealer]} (cupo o enfriamiento)")
        if open_count >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
            blockers.append("sin conversaciones libres")
        if tick - int((led.get("pilar_sell") or {}).get(str(o.asset), -10 ** 9)) < 30:
            blockers.append("esa copia ya se ofreció hace menos de 30 ticks")
        out.append({"type": "dealer_sell_open", "module": "vendedores", "kind": f"vender a {o.dealer} [calibrada]",
                    "dealer": o.dealer, "ref": f"card:{o.ref}", "asset": o.asset, "price": o.price,
                    "du": o.du, "score": 10 ** 3 + 2000 + 100 * o.gain + o.du, "blockers": blockers,
                    "notes": [f"--ladder-calibrated: {lcal.describe(o)}",
                              "cuenta para la escalera si cobramos más que su primera puja"]})
    return out


def mirror_dealers(s, led, args):
    """--chato-mirror: vendedores que reflejan nuestra concesión (Chato v3). `on` = siempre; `auto` = versión ≥ 3 vista
    en /api/dealers o en persona.updated del feed (se recuerda en el ledger); `off` (defecto) = nunca."""
    mode = getattr(args, "chato_mirror", "off") or "off"
    if mode == "off":
        return set()
    known = led.setdefault("dealer_versions", {})
    out = set()
    for did in lcal.MIRROR_MIN_VERSION:
        v = lcal.dealer_version(did, (s.get("dealers") or {}).get(did), s["feed"].get("events", []))
        if v is not None:
            known[did] = max(int(known.get(did) or 0), v)
        if mode == "on" or lcal.mirrors(did, known.get(did)):
            out.add(did)
    return out


def is_mirror(s, args, t, did):
    """Este hilo va en modo espejo: vendedor detectado o, con `auto`, el propio vendedor lo ha dicho en el hilo."""
    mode = getattr(args, "chato_mirror", "off") or "off"
    return did in (s.get("mirror_dealers") or ()) or (mode == "auto" and lcal.mirror_said(t, did))


def news_sell_candidates(s, led, args, busy, open_count):
    """--news-sell: una noticia de demanda viva (fuente fiable dentro de su ventana, o fila explícita en el menú actual
    del vendedor) abre una venta a ese vendedor de una copia que page_guard deja salir; suelo = valor privado +
    --news-margin (news_watch.sell_suggestions). Una conversación por vendedor; sin el flag no se llama."""
    tick = s["clock"]["tick"]
    committed = committed_ids(s, led)
    selling = {a for t in s["threads"]["open"] if t.get("kind") == "persona" for a in neg.sell_assets_of(t.get("topic"))}
    offers = [o for o in s["offers"].get("offers", [])] + [{"maker": s["me"]["id"], "status": "open",
                                                            "give": {"assets": sorted(committed | selling)}}]
    rel = nw.reliability(NEWS_CAL.get("cal"))
    sugg = nw.sell_suggestions(s.get("news") or [], s.get("dealers") or {}, s["me"], s["catalog"], offers, tick,
                               s["clock"].get("tick_seconds"), rel, getattr(args, "news_margin", 2.0),
                               nw.news_ticks(s["feed"].get("events", [])))
    out, opened = [], set()
    for g in sugg:
        did = g["dealer"]
        blockers = []
        if not dealer_available(s, did):
            blockers.append(f"{did} no disponible")
        if did in busy or did in opened:
            blockers.append(f"ya hay una conversación abierta con {did}")
        if led["blocked"].get(did, 0) > tick:
            blockers.append(f"{did} bloqueado hasta el tick {led['blocked'][did]} (cupo o enfriamiento)")
        if open_count >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
            blockers.append("sin conversaciones libres")
        if tick - int((led.get("pilar_sell") or {}).get(str(g["asset"]), -10 ** 9)) < 30:
            blockers.append("esa copia ya se ofreció hace menos de 30 ticks")
        if not blockers:
            opened.add(did)
        out.append({"type": "dealer_sell_open", "module": "noticias", "kind": f"vender a {did} [noticia]",
                    "dealer": did, "ref": f"card:{g['ref']}", "asset": g["asset"], "price": g["ask"],
                    "floor": g["floor"], "news_floor": g["floor"], "du": round(g["ask"] - g["value"], 2),
                    "score": 10 ** 3 + 1500 + g["ask"] - g["value"], "blockers": blockers,
                    "notes": [g["text"], f"noticia #{g['news_id']} [{g['source']}] fiabilidad {g['reliability']}"
                              + (" · confirmada en el menú" if g["confirmed_by_menu"] else "")]})
    return out


def fever_priority(cands, s, args):
    """--fever-priority: con una fiebre ACTIVA de /api/schedule (p. ej. Pilar +25 % por SAL) las aperturas de venta de
    ese barrio a ese vendedor pasan delante; si la fiebre empieza dentro de --fever-wait horas de juego, se esperan."""
    sched = s.get("schedule")
    if sched is None:
        return cands
    now_h, tick = nw.schedule_now(sched, s["clock"]), s["clock"]["tick"]
    fv = [nw.fever_state(f, now_h, tick) for f in nw.fevers(sched, s["catalog"])]
    cards = {c["id"]: sid["id"] for sid in s["catalog"].get("sets", []) for c in sid.get("cards", [])}
    wait = getattr(args, "fever_wait", 1.0)
    for c in cands:
        if c.get("type") != "dealer_sell_open":
            continue
        set_id = cards.get(str(c.get("ref") or "")[5:])
        for f in fv:
            if f["dealer"] != c.get("dealer") or f["set"] != set_id:
                continue
            if f["state"] == "activa":
                c["score"] = c.get("score", 0) + 10 ** 4
                c["notes"] = list(c.get("notes") or []) + [f"FIEBRE {set_id} activa hasta t~{f['end_tick']}: "
                                                          f"{f['dealer']} paga +{f['pct']:g} % sobre book"]
            elif f["state"] == "próxima" and f["hours_to_start"] is not None and f["hours_to_start"] <= wait:
                c["blockers"] = list(c.get("blockers") or []) + [f"esperar a la fiebre {set_id} (+{f['pct']:g} %, "
                                                                f"empieza en t~{f['start_tick']})"]
    return cands


def news_floor_guard(cands, led, args):
    """--news-sell: en una venta abierta por noticia, no se acepta ni se contraoferta por debajo de su suelo."""
    floors = led.get("news_floor") or {}
    if not getattr(args, "news_sell", False) or not floors:
        return cands
    for c in cands:
        f = floors.get(str(c.get("asset")))
        if f is None or c["type"] not in ("dealer_sell_accept", "dealer_sell_counter"):
            continue
        c["floor"] = max(c.get("floor") or 0, f)
        if (c.get("price") or 0) < f:
            c["blockers"] = list(c.get("blockers") or []) + [f"por debajo del suelo de la noticia ({f} P)"]
    return cands


def pilar_candidates(s, led, args, val, counts, ladder, cal_plan=None):
    """--pilar-sell: abrir UNA venta a Pilar (una carta por hilo) mientras falten tratos negociados con ella; los hilos
    abiertos los lleva sell_thread_candidates. Devuelve (candidatas, informe, activos en ventas abiertas).
    Con --ladder-calibrated: precio esperado del perfil medido (SAL/RET aparte) y, si el plan reparte cartas entre
    vendedores, solo las que el plan asigna a Pilar."""
    cfg = pilar_cfg(args)
    cal = calibrated_on(args)
    planned = {o.ref for o in (cal_plan or {}).get("picks", []) if o.dealer == lplus.PILAR and o.mode == "sell"}
    others = {o.ref for o in (cal_plan or {}).get("picks", []) if o.dealer != lplus.PILAR and o.mode == "sell"}
    if cfg is None:
        return [], None, set()
    did, tick, me = cfg.dealer, s["clock"]["tick"], s["me"]
    dealer = (s.get("dealers") or {}).get(did)
    rows = [r for r in ((dealer or {}).get("menu") or {}).get("buys", []) if "rarity" in r]
    rarities = {r["rarity"] for r in rows} or set(lplus.PILAR_RARITIES)
    n_q = len(ladder.get(did, []))
    allow = {x.strip().upper() for x in str(getattr(args, "allow_last_copy", "") or "").split(",") if x.strip()}
    by_id = {a["id"]: a for a in me["assets"] if a.get("kind") == "card"}
    selling = {a for t in s["threads"]["open"] if t.get("kind") == "persona" for a in neg.sell_assets_of(t.get("topic"))}
    busy_refs = {by_id[a]["ref"] for a in selling if a in by_id}
    report = {"dealer": did, "qualifying": n_q, "target": cfg.target_deals, "sets": sorted(cfg.sets) or ["*"],
              "mult": cfg.mult, "open": cfg.open_ask, "step": cfg.step, "window": [cfg.from_tick, cfg.until_tick],
              "next": None}
    blockers = []
    if not dealer_available(s, did):
        blockers.append(f"{did} no está en juego o no está desbloqueado")
    if n_q >= cfg.target_deals:
        blockers.append(f"objetivo cumplido: {n_q} tratos negociados con {did}")
    if not cfg.in_window(tick):
        blockers.append(f"fuera de la ventana --pilar-from-tick/--pilar-until-tick ({cfg.from_tick}-{cfg.until_tick})")
    if any(t.get("with") == did for t in s["threads"]["open"]):
        blockers.append(f"ya hay una conversación abierta con {did}")
    if led["blocked"].get(did, 0) > tick:
        blockers.append(f"{did} bloqueado hasta el tick {led['blocked'][did]} (cupo o enfriamiento)")
    if len(s["threads"]["open"]) >= s["clock"]["limits"].get("max_open_threads_per_team", 6):
        blockers.append("sin conversaciones libres")
    committed = committed_ids(s, led)
    tried = led.get("pilar_sell") or {}
    best = None
    for ref in sorted(counts):
        card = val.cards.get(ref) or {}
        if card.get("rarity") not in rarities or card.get("hidden") or not cfg.covers(card.get("set", "")):
            continue
        if ref in busy_refs:
            continue  # un hilo por carta
        if counts[ref] < 2 and not lplus.last_copy_allowed(ref.upper(), card.get("set", "").upper(), allow, cfg):
            continue  # última copia: solo con --pilar-last-copy o --allow-last-copy REF|SET
        free = [i for i in pg.tradeable_assets(ref, counts, s["catalog"], me["assets"], committed)
                if i not in selling and tick - int(tried.get(str(i), -10 ** 9)) >= 30]
        if not free:
            continue  # page_guard: la copia mantiene una página completa (o ya está comprometida)
        loss, floor = pilar_floor(val, counts, ref)
        if cal:
            if ref in others or (planned and ref not in planned):
                continue  # el plan la reserva para otro vendedor (o Pilar ya tiene sus cartas planificadas)
            _, cp_ = sell_profile(args, s, lplus.PILAR, did, ref, val)
            exp = cp_.expected_price("sell")
        else:
            exp = lplus.expected_price(card, cfg, next((r for r in rows if r["rarity"] == card.get("rarity")), None))
        if exp < floor:
            continue
        if best is None or exp - loss > best[0]:
            best = (exp - loss, ref, free[-1], floor, exp, loss)
    out = []
    if best is None:
        blockers.append("ninguna copia vendible: barrio/rareza fuera del modo, última copia sin permiso, página "
                        "completa o precio esperado por debajo de nuestro valor privado")
    else:
        surplus, ref, aid, floor, exp, loss = best
        first = lplus.first_ask(floor, cfg)
        if cal:
            _, cp_ = sell_profile(args, s, lplus.PILAR, did, ref, val)
            first = max(floor, int(cp_.first * cp_.open_obs + 0.5))
        report["next"] = {"ref": ref, "asset": aid, "floor": floor, "expected": exp, "first_ask": first}
        bonus = 5000 if n_q == neg.LADDER_SLOTS - 1 else 2000
        out.append({"type": "dealer_sell_open", "module": "vendedores", "kind": f"vender a {did}", "dealer": did,
                    "ref": f"card:{ref}", "asset": aid, "price": exp, "floor": floor, "du": round(surplus, 2),
                    "score": 10 ** 3 + bonus + surplus, "blockers": blockers, "ladder_mode": neg.ladder_mode(n_q),
                    "notes": [f"--pilar-sell: esperado ~{exp} P; pediremos {first} P y bajaremos "
                              f"de {cfg.step} en {cfg.step}; mínimo {floor} P (valor privado {loss:.1f} P)" if not cal
                              else f"--ladder-calibrated: esperado ~{exp} P (mejor trato observado); pediremos ~{first} "
                                   f"P; solo final:true o el objetivo cierran; mínimo {floor} P (valor privado "
                                   f"{loss:.1f} P)",
                              f"escalera {did} {n_q}/{cfg.target_deals}: cuenta solo si cobramos más que su apertura"]})
    report["blockers"] = blockers
    return out, report, selling


def ladder_fill_need(s, led, args, lc, val, counts, ladder, active):
    """--ladder-fill: caja para el siguiente trato de cada vendedor de COMPRA con la escalera incompleta y sin
    conversación activa (las activas ya cuentan con su máximo). Así las pujas pasivas no se la comen."""
    tick, need = s["clock"]["tick"], 0
    busy = {a["thread"].get("with") for a in active}
    for did, dealer in (s.get("dealers") or {}).items():
        if did in busy or len(ladder.get(did, [])) >= neg.LADDER_SLOTS or led["blocked"].get(did, 0) > tick \
                or not dealer_available(s, did):
            continue
        best = None
        for row in (dealer.get("menu") or {}).get("sells", []):
            list_p = row.get("list_price")
            if not list_p or lplus.fill_priority(did, row.get("rarity")) <= 0:
                continue
            for ref, c in val.cards.items():
                if c["released"] and c["rarity"] == row["rarity"] and not counts.get(ref) and not c.get("hidden"):
                    econ = math.floor(min(args.per_card, val.next_copy(counts, ref) - args.margin))
                    if econ >= list_p * neg.ladder_profile(did, lc, args.mode).min_viable_frac:
                        best = min(econ, list_p) if best is None else min(best, econ, list_p)
        need += best or 0
    return need


def ladder_cal_lines(r):
    head = ("ESCALERA CALIBRADA · tratos que puntúan " +
            " ".join(f"{d} {r['filled'].get(d, 0)}/{neg.LADDER_SLOTS}" for d in lcal.LADDER_DEALERS) +
            f" · plan: +{r['gain']} (peso × captura) con {r['cash']} P de {r['budget']} P")
    return [head] + [f"  · {x}" for x in r["picks"]] if r["picks"] else [head + " · nada planificable"]


def pilar_line(r):
    line = (f"{r['dealer'].upper()} VENTA · escalera {r['qualifying']}/{r['target']} · barrios {','.join(r['sets'])} "
            f"× {r['mult']} · pedir {r['open']} bajando {r['step']} · ventana "
            f"{r['window'][0] if r['window'][0] is not None else '—'}-{r['window'][1] if r['window'][1] is not None else '—'}")
    if r.get("next"):
        n = r["next"]
        line += f" · siguiente {n['ref']} (copia {n['asset']}): pedir {n['first_ask']}, mínimo {n['floor']}"
    if r.get("blockers"):
        line += " · no abre: " + "; ".join(r["blockers"])
    return line


def dealer_bid_cancels(s, cands, open_dealer, mine_threads):
    """--dedupe-bids: una puja pasiva por una carta que ya negociamos con un vendedor son dos vías para una necesidad
    (dos cierres = un duplicado): se bloquea la puja nueva y se propone cancelar la abierta. Las pujas duplicadas entre
    sí ya las retira siempre page_guard.duplicate_pursuit_cancels."""
    refs = set()
    for t in open_dealer:
        item = neg.item_of(t.get("topic")) or ""
        if t["id"] in mine_threads and item.startswith("card:"):
            refs.add(item[5:])
    out = []
    for c in cands:
        if c.get("type") == "bid" and c.get("ref") in refs and not c.get("manual_order"):
            c["blockers"] = list(c.get("blockers") or []) + [f"ya negociamos {c['ref']} con un vendedor"]
    for b in tr.own_bids(s["offers"].get("offers", []), s["me"]["id"]):
        if b["ref"] in refs:
            out.append({"type": "cancel", "module": "mercado", "kind": "cancelar puja (la compra va por un vendedor)",
                        "offer": b["offer"], "ref": b["ref"], "venue": b.get("venue"), "price": b["price"], "du": 0,
                        "score": 2 * 10 ** 4, "blockers": [],
                        "reason": f"negociamos {b['ref']} con un vendedor: dos vías darían un duplicado"})
    return out


def tactical_sales(s, led, args, val, pl, out):
    """Para cada objetivo `--sale-target REF=PRECIO`: verifica copias físicas, protege la copia de página, identifica
    comprador y propone ACEPTAR o CONTRAOFERTA dirigida. Sustituye a las candidatas genéricas sobre esa misma oferta
    y suspende la venta pública de esa carta mientras haya negociación (la copia excedente es para el comprador)."""
    reports = []
    venues = mi.venues_from(s)
    committed = committed_ids(s, led)
    for ref, target in ts.parse_targets(getattr(args, "sale_target", None) or []):
        st = (pl.get("states") or {}).get(ref)
        market = {}
        p_list = 0.2
        if st is not None:
            market = {"value": st.market.value, "confidence": st.market.confidence,
                      "best_bid": st.best_bid.price if st.best_bid else None,
                      "best_ask": st.best_ask.price if st.best_ask else None,
                      "ask_fee": venues[st.best_ask.venue].fee(st.best_ask.price, 1) if st.best_ask and
                      st.best_ask.venue in venues else 0}
            v = venues.get(args.duende_venue) if hasattr(args, "duende_venue") else None
            if v is not None:
                anchor = ts.next_ask(ts.SaleConfig(ref, target, margin=args.margin), 0, None, [])
                p_list, _ = mi.fill_probability("ask", anchor, st, v, mi.IntelConfig(margin=args.margin), led["actions"])
        rep = ts.assess(s, ts.SaleConfig(ref, target, margin=args.margin), val, committed, led["actions"],
                        INTEL_STATE.get("intel"), market, p_list, venues)
        reports.append(rep)
        own_offers = {c["offer"] for c in rep["candidates"] if c["type"] == "accept"}
        negotiating = rep["state"] != "BLOCKED" and rep.get("buyer")
        for c in out:
            if c["type"] == "accept" and c.get("offer") in own_offers:
                c["blockers"] = list(c.get("blockers") or []) + ["sustituida por la venta táctica (copia excedente elegida)"]
            if negotiating and c["type"] == "list" and c.get("ref") == ref and not c.get("to"):
                c["blockers"] = list(c.get("blockers") or []) + [f"venta táctica de {ref} en curso con {rep['buyer']}"]
        out += [dict(c, module="venta táctica") for c in rep["candidates"]]
    return reports


def parse_directed_buys(specs):
    """REF@EQUIPO=PRECIO → [(ref, team, price)]."""
    out = []
    for x in specs or []:
        try:
            left, price = str(x).split("=", 1)
            ref, team = left.split("@", 1)
            out.append((ref.strip(), team.strip(), int(price)))
        except ValueError:
            raise ValueError(f"--directed-buy mal formado: {x!r} (formato REF@EQUIPO=PRECIO)")
    return out


def directed_buys(s, led, args, val, counts, view):
    """Compras dirigidas ORDENADAS POR UN HUMANO (`--directed-buy REF@EQUIPO=PRECIO`): una puja estructurada con `to`
    (el equipo la acepta y se liquida sola; la comisión la paga quien acepta). Se envía UNA vez: si ya hay una
    abierta, se envió antes o ya tenemos la carta, no se repite. Sin `--override-value` exige ΔU ≥ margen; con él,
    salta SOLO esa regla. La reserva dura, el presupuesto y las obligaciones abiertas siguen mandando."""
    out = []
    team = s["me"]["id"]
    for ref, buyer_from, price in parse_directed_buys(getattr(args, "directed_buy", None)):
        gain = val.next_copy(counts, ref) if ref in val.cards else None
        du = round(gain - price, 2) if gain is not None else None
        blockers = []
        if gain is None:
            blockers.append(f"{ref} no está en el catálogo")
        elif du < args.margin and not getattr(args, "override_value", False):
            blockers.append(f"ΔU {du} P < margen (valor {gain} P, precio {price} P): requiere --override-value")
        if counts.get(ref) and not getattr(args, "override_value", False):
            blockers.append(f"ya tenemos {ref} (una orden con --override-value puede comprar otra copia)")
        open_same = [o for o in s["offers"].get("offers", []) if o.get("maker") == team and o.get("to") == buyer_from
                     and o.get("status") == "open" and f"card:{ref}" in ((o.get("want") or {}).get("types") or [])]
        sent = [a for a in led["actions"] if a.get("type") == "bid" and a.get("ref") == ref and a.get("to") == buyer_from
                and a.get("status") in ("intent", "ambiguous", "submitted", "settled")]
        if open_same or sent:
            blockers.append(f"ya enviada a {buyer_from} (oferta {[o['id'] for o in open_same] or [a.get('offer_id') for a in sent]})")
        offer_in = seller_offer(s, ref, buyer_from, price)
        if offer_in is not None:  # el equipo ya publicó la carta para nosotros: ACEPTAR su oferta estructurada
            o, vid, fee = offer_in
            p = int(o["want"]["cash"])
            blockers = [b for b in blockers if not b.startswith("ya enviada")]
            if p + fee > view.free_dealer_cash:
                blockers.append(f"{p + fee} P > efectivo libre {view.free_dealer_cash} P (reserva dura, obligaciones "
                                "abiertas y presupuesto incluidos)")
            out.append({"type": "accept", "module": "manual", "kind": f"aceptar venta de {buyer_from}", "venue": vid,
                        "offer": o["id"], "maker": buyer_from, "receive": {ref: 1}, "deliver": {}, "assets": [],
                        "ref": ref, "price": p, "fee": fee, "cash": -(p + fee), "dv": gain,
                        "du": round((gain or 0) - p - fee, 2), "score": 4 * 10 ** 5, "blockers": blockers,
                        "manual_order": True, "reason": f"orden humana: aceptar oferta #{o['id']} de {buyer_from} "
                                                        f"({ref} por {p} P + {fee} P de comisión en {vid})",
                        "notes": ["estructura verificada: da exactamente una copia, pide solo efectivo ≤ precio ordenado"]})
            continue
        if price > view.free_dealer_cash:
            blockers.append(f"{price} P > efectivo libre {view.free_dealer_cash} P (reserva dura, pujas abiertas y "
                            "presupuesto incluidos)")
        out.append({"type": "bid", "module": "manual", "kind": f"compra dirigida a {buyer_from}", "ref": ref,
                    "to": buyer_from, "venue": args.duende_venue, "price": price, "cash": -price, "fee": 0,
                    "dv": gain, "du": du, "expected_du": du, "score": 4 * 10 ** 5, "blockers": blockers,
                    "expires_in": getattr(args, "directed_expiry", 20), "manual_order": True,
                    "reason": f"orden humana: {price} P a {buyer_from} por {ref} (valor privado {gain} P"
                              + (", ΔU negativo aceptado con --override-value)" if du is not None and du < args.margin else ")"),
                    "notes": ["oferta estructurada dirigida; si no la aceptan caduca sin coste"]})
    return out


def seller_offer(s, ref, seller, max_price):
    """Oferta estructurada ABIERTA de `seller` (pública o dirigida a nosotros) que da exactamente una copia de `ref`
    y pide solo efectivo ≤ `max_price` en un venue abierto que no es nuestro. Devuelve (oferta, venue, comisión)."""
    team, tick = s["me"]["id"], s["clock"]["tick"]
    venues = mi.venues_from(s)
    pool = list(s["offers"].get("offers", []))
    for b in (s.get("boards") or {}).values():
        pool += (b or {}).get("offers", [])
    best = None
    for o in pool:
        g, w = o.get("give") or {}, o.get("want") or {}
        assets = [a for a in g.get("assets") or [] if isinstance(a, dict)]
        if (o.get("maker") != seller or o.get("status") != "open" or o.get("to") not in (None, team)
                or (o.get("expires_tick") is not None and o["expires_tick"] <= tick) or o.get("venue") not in venues
                or len(assets) != 1 or assets[0].get("ref") != ref or g.get("cash") or g.get("types")
                or w.get("assets") or w.get("types") or w.get("cards") or not isinstance(w.get("cash"), int)
                or not 0 < w["cash"] <= max_price):
            continue
        fee = venues[o["venue"]].fee(w["cash"], 1)  # al aceptar pagamos nosotros la comisión del venue
        if w["cash"] + fee <= max_price and (best is None or w["cash"] + fee < best[0]["want"]["cash"] + best[2]):
            best = (o, o["venue"], fee)
    return best


def page_campaign_step(s, led, args, val, counts, pl, view, scored, ccfg, used, out):
    """Rutas activas hacia las cartas que faltan de la página objetivo; cancela búsquedas ya cumplidas; enfoca el
    capital (sin pujas públicas para cartas ajenas a la campaña mientras esté activa) y rebalancea si un cierre de
    campaña necesita efectivo. Nunca autoriza ΔU < margen: la prioridad solo ordena."""
    set_id = getattr(args, "page_campaign", "none")
    team, tick = s["me"]["id"], s["clock"]["tick"]
    cancels = pc.acquired_target_cancels(s["offers"].get("offers", []), team, counts)
    out += [dict(c, module="campaña página") for c in cancels]
    choice = None
    if set_id == "auto":  # página principal por viabilidad económica (inventario y ofertas ACTUALES)
        budget = max(view.free_tactical_cash, view.free_dealer_cash)  # el táctico está dentro del de vendedores
        choice = pc.choose_page(s, val, mi.venues_from(s), s.get("dealers") or {}, max(budget, 0), args.margin)
        set_id = choice["choice"] or next((p["set"] for p in choice["pages"] if not p["blocked"]), None)
    if not set_id or set_id == "none" or set_id not in val.pages:
        return {"selection": choice} if choice else None
    cfg = pc.CampaignConfig(set_id=set_id, margin=args.margin, per_card=args.per_card,
                            directed_expiry=getattr(args, "directed_expiry", 20),
                            anchor_near=getattr(args, "profile", None) == "fast-close")
    state = pc.campaign_state(s, val, set_id)
    rep = {"state": state, "plans": [], "next_action": "—", "selection": choice}
    if state["complete"]:
        rep["next_action"] = (f"{set_id} COMPLETA: campaña terminada; page_guard protege una copia de cada carta de "
                              "la página; solo los duplicados son negociables")
        return rep
    venues = mi.venues_from(s)
    committed = committed_ids(s, led)
    assets = pc.our_trade_assets(s, val, committed)
    intel = INTEL_STATE.get("intel")
    pursuing = {}
    for o in s["offers"].get("offers", []):
        if o.get("maker") == team and o.get("status") == "open" and not o.get("thread"):
            w = o.get("want") or {}
            for t in list(w.get("types") or []) + [f"card:{c}" for c in w.get("cards") or []]:
                if isinstance(t, str) and t.startswith("card:"):
                    pursuing.setdefault(t[5:], []).append(o)
    order = sorted(state["missing"], key=lambda r: -state["gains"][r]["gain"])
    plan_offers = set()
    for ref in order:
        g = state["gains"][ref]
        st = (pl.get("states") or {}).get(ref)
        market = {"value": st.market.value, "best_ask": st.best_ask.price if st.best_ask else None} if st else {}
        plan = pc.plan_target(ref, g["gain"], g["completes_page"], state["level"], s, val, cfg, venues, intel,
                              assets, market)
        rep["plans"].append(plan)
        best = plan.get("best")
        c = dict(best["candidate"]) if best and best.get("candidate") else None
        if c is None:
            continue
        c["module"] = "campaña página"
        if c.get("venue") is None:
            c["venue"] = args.duende_venue
        existing = pursuing.get(ref, [])
        if c["type"] == "accept":
            plan_offers.add(c["offer"])
            need = -int(c["cash"]) - view.free_tactical_cash
            if need > 0:
                rb, _, why = ca.rebalance(scored, need, c["du"], ccfg, f"campaña {ref}", used)
                out += [dict(x, module="capital") for x in rb]
                used |= {x["offer"] for x in rb}
                c["blockers"] = [f"capital: faltan {need} P · {why}" + ("; se ejecuta cuando el servidor confirme"
                                                                       if rb else "")]
            for o in existing:  # cierre inmediato: las búsquedas pasivas de esa carta sobran
                out.append({"type": "cancel", "module": "campaña página", "kind": "retirar búsqueda redundante",
                            "offer": o["id"], "venue": o.get("venue"), "ref": ref, "price": 0, "du": 0.0,
                            "score": 2.2 * 10 ** 5, "blockers": [], "notes": [], "uncertainty": "",
                            "reason": f"{ref}: hay una ruta inmediata superior ({best['via']})"})
        elif existing:
            c["blockers"] = [f"ya perseguimos {ref} con la oferta {[o['id'] for o in existing]} (una vía por carta)"]
        elif c["type"] == "bid" and -int(c["cash"]) > view.free_tactical_cash:
            c["blockers"] = [f"{-int(c['cash'])} P > efectivo táctico libre {view.free_tactical_cash} P"]
        out.append(c)
        if intel is not None and not c["blockers"]:
            try:
                intel.record_event(ref, c.get("maker") or c.get("to"), f"route:{best['route']}", c.get("price"),
                                   tick, c.get("venue"), {"du": c.get("du")})
            except Exception:
                pass
    targets = set(state["missing"])
    for x in out:  # foco: nada de dispersar efectivo en pujas públicas ajenas a la campaña; sin rutas duplicadas
        if x.get("page_campaign") or x.get("module") != "mercado":
            continue
        if x["type"] == "accept" and x.get("offer") in plan_offers:
            x["blockers"] = list(x.get("blockers") or []) + ["sustituida por la ruta de la campaña de página"]
        elif x["type"] == "bid" and not x.get("to"):
            why = (f"la campaña {set_id} gestiona {x.get('ref')}" if x.get("ref") in targets else
                   f"campaña {set_id} activa: el efectivo se enfoca en {sorted(targets)}")
            x["blockers"] = list(x.get("blockers") or []) + [why]
        elif x["type"] == "accept" and -(x.get("cash") or 0) > 0 and not set(x.get("receive") or {}) & targets:
            cost = -(x.get("cash") or 0)
            if (x.get("du") or 0) < max(10.0, 0.5 * cost):  # una compra ajena solo si es muy buena
                x["blockers"] = list(x.get("blockers") or []) + [
                    f"campaña {set_id} activa: compra ajena con ΔU {x.get('du')} P < max(10, 50 % de {cost} P)"]
    ranked = sorted((p for p in rep["plans"] if p.get("best")),
                    key=lambda p: (not p["completes_page"], -p["best"]["eu"]))
    if ranked:
        b = ranked[0]["best"]
        rep["next_action"] = f"{b['route'].upper()} · {b['via']} ({ranked[0]['ref']}, ΔU {b['du']} P)"
    else:
        dealers = sorted((x for x in out if x["type"] == "dealer_open" and x.get("ref", "")[5:] in targets),
                         key=lambda x: (bool(x["blockers"]), -x.get("du", 0)))
        if dealers:
            d = dealers[0]
            rep["next_action"] = (f"ABRIR con {d['dealer']} por {d['ref'][5:]} (lista {d['price']} P, máximo "
                                  f"{d['ceiling']} P)" + (f" · BLOQUEO: {'; '.join(d['blockers'])}" if d["blockers"] else ""))
        else:
            rep["next_action"] = ("sin ruta rentable: ningún ask por debajo del techo ni dueño identificable con "
                                  "evidencia; seguir observando tablones, feed y vendedores")
    rep["sequencing"] = sequencing_note(val, counts, state)
    return rep


def sequencing_note(val, counts, state):
    """La última carta se lleva el bono de página: conviene que sea la MÁS disponible/barata."""
    miss = state["missing"]
    if len(miss) < 2:
        return None
    total, _ = val.delta(counts, Counter({r: 1 for r in miss}), Counter())
    last = {r: round(val.next_copy(counts + Counter({x: 1 for x in miss if x != r}), r), 2) for r in miss}
    return {"total_gain_all": round(total, 2), "gain_if_last": last}


def dedupe_cancels(cands):
    """Una sola cancelación por oferta: la de mayor prioridad."""
    best = {}
    for c in cands:
        if c["type"] in ("cancel", "team_cancel") and c.get("offer") is not None:
            if c["offer"] not in best or c.get("score", 0) > best[c["offer"]].get("score", 0):
                best[c["offer"]] = c
    return [c for c in cands if c["type"] not in ("cancel", "team_cancel") or c.get("offer") is None
            or best.get(c["offer"]) is c]


def dealer_diag(did, deals, mode, t, st, ceiling, econ, avail, left, ttl, d, blockers, pol, best_open=None):
    """Por qué se cierra (o no) un trato con cada vendedor, en una línea legible."""
    info = {"dealer": did, "qualifying": len(deals), "slots": neg.LADDER_SLOTS, "mode": mode,
            "thread": t["id"] if t else None, "opening": st.opening if st else None,
            "our_last": st.last_ours if st else None, "current": st.live.price if st and st.live else None,
            "ceiling": ceiling, "econ_ceiling": econ, "capital_now": avail, "ticks_left": left, "expires_in": ttl,
            "concession": None, "action": None, "why_no_deal": []}
    if st:
        info["concession"] = bool(st.current and st.opening is not None and st.current.price < st.opening)
        info["action"] = f"{d.action.upper()} {d.price if d.price is not None else ''}".strip()
        why = []
        if blockers:
            why += blockers
        if d.action in ("abandon", "wait"):
            live = st.live
            if st.current is not None and live is None:
                why.append("oferta caducada")
            if live and econ is not None and live.price > econ:
                why.append(f"precio {live.price} P por encima del máximo económico {econ} P")
            elif live and ceiling is not None and live.price > ceiling:
                why.append(f"capital no disponible ({avail} P ahora)")
            if not info["concession"]:
                why.append("sin concesión todavía")
            if pol and st.turns >= pol.max_counteroffers:
                why.append("límite de contraofertas")
            if left is not None and left <= 0:
                why.append("límite de ticks de la conversación")
            why.append(d.reason)
        info["why_no_deal"] = why
    else:
        info["action"] = "ABRIR " + best_open["ref"] if best_open and not best_open["blockers"] else "—"
        info["why_no_deal"] = list(blockers or [])
    return info


def dealer_lines(diag):
    out = []
    for did, x in sorted(diag.items()):
        yn = {True: "SÍ", False: "NO", None: "—"}[x["concession"]]
        line = (f"{did.upper()} · escalera {x['qualifying']}/{x['slots']} · estrategia {x['mode']} · hilo "
                f"#{x['thread'] or '—'} · apertura {x['opening'] or '—'} · nuestra última {x['our_last'] or '—'} · "
                f"su precio {x['current'] or '—'} · máximo {x['ceiling'] if x['ceiling'] is not None else '—'} · "
                f"concesión {yn} · ticks restantes {x['ticks_left'] if x['ticks_left'] is not None else '—'}"
                f"{' (oferta caduca en ' + str(x['expires_in']) + ')' if x['expires_in'] is not None else ''} · "
                f"acción recomendada: {x['action']}")
        if x["why_no_deal"] and not str(x["action"]).startswith("ACCEPT"):
            line += " · sin trato porque: " + "; ".join(dict.fromkeys(str(w) for w in x["why_no_deal"]))
        out.append(line)
    return out


def campaign_cfg(args):
    kinds = ("collect", "sell", "swap") if args.campaign in ("all", "none") else (args.campaign,)
    extra = {}
    if getattr(args, "profile", None) == "fast-close":  # aperturas cercanas a un comparable y segunda propuesta fuerte
        extra = dict(buy_open_frac=0.92, sell_markup=1.08, concession=0.6)
    return cp.CampaignConfig(kinds=kinds, ticks=args.campaign_ticks, budget=args.campaign_budget,
                             max_proposals=args.max_proposals, negotiation_ticks=args.negotiation_ticks,
                             max_conversations=args.max_conversations, margin=args.margin, **extra)


PROFILES = {
    # Cierre rápido + colección. Margen económico 1 P (la verificación de la valoración contra collection_value
    # sigue bloqueando TODO si no cuadra: el margen no sustituye al colchón de incertidumbre); 2 propuestas y 4
    # ticks por conversación; colchones ajustados al trabajo activo y capital pasivo limitado.
    "fast-close": {"margin": 1.0, "max_proposals": 2, "negotiation_ticks": 4, "tactical_buffer": 15,
                   "max_passive_frac": 0.3, "page_campaign": "auto"},
}


def apply_profile(args, argv=None):
    """Aplica un perfil SOLO a los parámetros que el usuario no fijó explicitamente en la línea de órdenes."""
    prof = PROFILES.get(getattr(args, "profile", None) or "")
    if not prof:
        return {}
    argv = list(sys.argv[1:] if argv is None else argv)
    given = {a.split("=", 1)[0].lstrip("-").replace("-", "_") for a in argv if a.startswith("--")}
    applied = {}
    for k, v in prof.items():
        if k not in given:
            setattr(args, k, v)
            applied[k] = v
    return applied


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
            blockers += lplus.deny_blockers({"type": "team_open", "team": o["team"], "du": o.get("du_est")},
                                            *deny_cfg(args))  # --deny-teams: antes de ocupar un hueco
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


def committed_ids(s, led, exclude_key=None):
    """Copias nuestras ya comprometidas: ofertas propias abiertas + acciones de este tick o ambiguas sin liquidar."""
    tick = s["clock"]["tick"]
    pend = []
    for a in led.get("actions", []):
        if a.get("key") == exclude_key or a.get("type") in pg.NON_DELIVERING:
            continue
        if a.get("status") == "ambiguous" or (a.get("status") in ("intent", "submitted") and a.get("tick") == tick):
            pend += list(a.get("assets") or []) + ([a["asset"]] if a.get("asset") is not None else [])
    return pg.committed_assets(s["offers"].get("offers", []), s["me"]["id"], pend)


def double_commit(c, s, committed):
    """Texto de bloqueo si la candidata entregaría un activo ya comprometido en otra obligación abierta."""
    _, ids, _ = pg.delivery_of(c, s)
    committed = set(committed)
    if c.get("type") in ("dealer_sell_counter", "dealer_sell_accept"):
        # en una VENTA a un vendedor, nuestra petición anterior en el MISMO hilo es la misma obligación (un hilo liquida
        # un solo trato): no cuenta como segundo compromiso de la copia
        team, offers = s["me"]["id"], s["offers"].get("offers", [])
        mine = [o for o in offers if o.get("maker") == team and o.get("status") in pg.OPEN_STATES]
        same = pg.committed_assets([o for o in mine if o.get("thread") == c.get("thread")], team)
        other = pg.committed_assets([o for o in mine if o.get("thread") != c.get("thread")], team)
        committed -= same - other
    twice = sorted(set(ids) & committed)
    return f"activo(s) {twice} ya comprometido(s) en otra obligación abierta" if twice else None


def _cards_of(c):
    """Cartas que una candidata adquiere o entrega (para no perseguir la misma por dos vías)."""
    if c["type"] in ("cancel", "team_cancel", "team_close", "dealer_close", "info"):
        return set()
    out = set(c.get("receive") or {}) | set(c.get("deliver") or {})
    o = c.get("opp") or {}
    out |= {o.get("receive"), o.get("deliver")} - {None}
    if c["type"] in ("bid", "list", "dealer_open", "swap_list", "dealer_sell_open") and c.get("ref"):
        out.add(c["ref"].split(":")[-1])
    if c.get("give_ref"):
        out.add(c["give_ref"])
    return out


def rival_venue_blocker(c, s):
    """Bloqueo si la candidata publicaría o aceptaría en el venue de otro equipo: el market-making puntúa el valor
    creado entre otros equipos en tu venue, así que cada trato nuestro allí suma puntos a un rival (--no-rival-venues).
    Cancelar sigue permitido: retirar una puja de un venue rival nunca le suma."""
    if c["type"] not in ("list", "bid", "swap_list", "accept"):
        return None
    v = c.get("venue") or "rastro"
    for x in (s.get("venues") or {}).get("venues", []):
        if x.get("venue") == v and x.get("owner") not in (s["me"]["id"], "world", None):
            return f"venue {v} es de {x['owner']}: le sumaría market-making"
    return None


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


WRITE_GAP = 0.6  # s entre escrituras (cada una puede ir seguida de lecturas del propio SDK)


def pace(reader, gap=WRITE_GAP):
    last = getattr(reader, "last", None)
    if last is not None:
        time.sleep(max(0.0, gap - (time.monotonic() - last)))
        reader.last = time.monotonic()


def send(reader, led, s, c, args, journal):
    tick = s["clock"]["tick"]
    key = tr.idem_key({**c, "type": c["type"], "tick_scope": c.get("thread")})
    if any(a.get("key") == key and a["status"] in ("intent", "ambiguous", "submitted") for a in led["actions"]):
        print(f"   (ya enviada antes, no se repite: {describe(c)})")
        return None
    # Última barrera antes de la red (prioridad 1): ningún módulo puede saltarse la protección de páginas completas.
    committed = committed_ids(s, led)
    protect = pg.guard_candidate(c, s, committed)
    if protect:
        print(f"   PROTECTED_PAGE_BLOCK {describe(c)} · {'; '.join(protect)} · NO SE ENVÍA")
        return None
    twice = double_commit(c, s, committed)
    if twice:
        print(f"   ASSET_EXPOSURE_BLOCK {describe(c)} · {twice} · NO SE ENVÍA")
        return None
    rec = {"key": key, "type": c["type"], "module": c["module"], "kind": c["kind"], "tick": tick, "status": "intent",
           "offer": c.get("offer"), "asset": c.get("asset"), "assets": c.get("assets") or ([c["asset"]] if c.get("asset") else []),
           "ref": c.get("ref"), "price": c.get("price"), "cost": max(0, -(c.get("cash") or 0)) or (c.get("price") or 0
           if c["type"] == "dealer_accept" else 0), "du": c.get("du"), "thread": c.get("thread"),
           "dealer": c.get("dealer"), "item": c.get("item"), "opening": c.get("opening"), "ceiling": c.get("ceiling"),
           "reason": c.get("reason") or "; ".join(c.get("notes") or c.get("why") or []), "before": metrics(s),
           "version": VERSION, "cash": c.get("cash"), "dv": c.get("dv"), "expected_du": c.get("expected_du"),
           "ladder_mode": c.get("ladder_mode"), "p_fill": c.get("p_fill"), "confidence": c.get("confidence_label")}
    if c.get("neg") and c["neg"] in led["negotiations"]:
        led["negotiations"][c["neg"]]["prev_state"] = led["negotiations"][c["neg"]]["state"]
    if c["type"] in ("accept", "team_accept"):
        pool = s["board"].get("offers", []) + s["offers"].get("offers", []) + \
            [o for t in s["threads"]["open"] for o in t.get("standing_offers") or []] + \
            [o for b in (s.get("boards") or {}).values() for o in (b or {}).get("offers", [])]
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
    pace(reader)  # las escrituras también respetan el límite de 5 peticiones por segundo
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
            text = (neg.ladder_message if c.get("ladder") else neg.dealer_message)(c["dealer"], c.get("turns", 0),
                                                                                    c["price"], c["ref"])
            resp = reader.api.say(c["thread"], text, price=c["price"])
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
        elif c["type"] == "dealer_sell_open":
            resp = reader.api.open_thread(c["dealer"], topic={"sell": {"assets": [c["asset"]]}})
            rec["thread"] = resp.get("id")
            led["threads"].append(resp.get("id"))
            led.setdefault("pilar_sell", {})[str(c["asset"])] = tick  # no reabrir la misma copia enseguida
            if c.get("news_floor") is not None:  # --news-sell: el suelo (valor privado + margen) acompaña a la copia
                led.setdefault("news_floor", {})[str(c["asset"])] = c["news_floor"]
        elif c["type"] in ("dealer_sell_counter", "dealer_sell_accept"):
            if c["type"] == "dealer_sell_counter":
                resp = reader.api.say(c["thread"], neg.ladder_message(c["dealer"], c.get("turns", 0), c["price"],
                                                                      c["ref"], "sell"), price=c["price"])
            else:
                resp = reader.api.accept(c["offer"])
            journal.append("decision", {"dealer": c["dealer"], "item": c["item"], "thread": c["thread"], "tick": tick,
                                        "action": "sell_" + ("counter" if c["type"] == "dealer_sell_counter" else "accept"),
                                        "price": c["price"], "reason": c.get("reason"), "mode": f"coord-{args.mode}"})
        elif c["type"] == "team_open":
            nrec = cp.new_negotiation(c["opp"], tick, campaign_cfg(args))
            nrec["state"] = "ambigua"  # hasta saber si la conversación se abrió
            led["negotiations"][nrec["id"]] = nrec
            save(led)
            topic = {"campaign": nrec["kind"], "card": nrec.get("receive") or nrec.get("deliver")}
            resp = reader.api.open_thread(c["team"], topic=topic, venue="rastro")
            nrec.update(thread=resp.get("id"), state="contactada", last_tick=tick)
            rec["thread"] = resp.get("id")
            led["campaign"]["stats"]["contacts"] += 1
        elif c["type"] == "team_propose":
            nrec = led["negotiations"][c["neg"]]
            assert cp.text_matches(c["offer"], c["text"]), "el texto no coincide con la estructura"
            nrec["state"] = "ambigua"
            save(led)
            resp = reader.api.say(c["thread"], c["text"], offer=c["offer"])
            g, w = c["offer"]["give"], c["offer"]["want"]
            nrec["last_price"] = int(g.get("cash") or 0) if nrec["kind"] != "sell" else int(w.get("cash") or 0)
            nrec["proposals"].append({"tick": tick, "offer": c["offer"], "text": c["text"]})
            nrec.update(state="propuesta", last_tick=tick)
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
                                                       "team_propose", "dealer_sell_open") else "settled",
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

def open_intel(team, cache=None):
    """La capa de inteligencia se reutiliza entre ticks (en la caché del bucle) y falla de forma degradada."""
    intel = getattr(cache, "intel", None) if cache is not None else None
    if cache is None and INTEL_STATE.get("intel") is not None:  # análisis de un tick: no acumular conexiones
        try:
            INTEL_STATE["intel"].close()
        except Exception:
            pass
        INTEL_STATE["intel"] = None
    if intel is None:
        try:
            intel = intel_mod.Intelligence(DATA / "market.db", team)
        except Exception as e:  # la base de datos nunca detiene al coordinador
            print(f"   INTELIGENCIA no disponible ({type(e).__name__}); se sigue con la instantánea")
            return None
        if cache is not None:
            cache.intel = intel
    return intel


def feed_intel(intel, s):
    if intel is None:
        return None
    try:
        n = intel.ingest(s)
        m = intel.update_models(s["clock"]["tick"])
        return {**n, **m}
    except Exception as e:
        print(f"   INTELIGENCIA: fallo al ingerir ({type(e).__name__}: {e}); se sigue con la instantánea")
        return None


def cycle(reader, args, led, journal, execute, cache=None):
    s = snapshot(reader, cache)
    if getattr(args, "news_sell", False):  # --news-sell: una lectura más por tick (GET /api/news)
        try:
            s["news"] = nw.news_items(reader.call("call", "GET", "/api/news"))
        except BazaarError as e:
            s["news"] = []
            print(f"   NOTICIAS: /api/news no disponible ({e}); --news-sell sin efecto este tick")
    if getattr(args, "fever_priority", False):  # --fever-priority: GET /api/schedule (persona_patch de fiebres)
        try:
            s["schedule"] = reader.call("schedule")
        except BazaarError as e:
            s["schedule"] = None
            print(f"   FIEBRE: /api/schedule no disponible ({e}); --fever-priority sin efecto este tick")
    intel = open_intel(s["me"]["id"], cache)
    ingested = feed_intel(intel, s)  # reutiliza la instantánea: ninguna llamada extra a la API
    INTEL_STATE["intel"] = intel
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
    deny, deny_margin = deny_cfg(args)
    if deny:  # --deny-teams (opt-in): ni dirigidas a, ni aceptadas de, ni campañas con esos equipos
        n = lplus.apply_deny(cands, deny, deny_margin)
        if n:
            print(f"   DENY_TEAMS: {n} candidata(s) con {','.join(sorted(deny))} bloqueadas"
                  + (f" (excepción: excedente ≥ {deny_margin} P)" if deny_margin is not None else ""))
    committed = committed_ids(s, led)
    blocked = pg.apply_guard(cands, s, committed)  # antes de ordenar: inviables, fuera de la selección
    if blocked:
        print(f"   PROTECTED_PAGE_BLOCK: {blocked} candidata(s) romperían una página completa; excluidas")
    for c in cands:  # un activo físico solo puede estar en UNA obligación de entrega
        twice = double_commit(c, s, committed)
        if twice:
            c["blockers"] = list(c.get("blockers") or []) + [twice]
    m = metrics(s)
    print(f"\n[{VERSION} · tick {tick} · ronda {m['round']}] efectivo {m['cash']} P · libre {pl['free_cash']} P · "
          f"reservado en ofertas {pl['reserved_cash']} P · pendiente {pl['pending_cash'] - exposure} P · expuesto con "
          f"vendedores {exposure} P · gasto confirmado {led['spent_confirmed']}/{args.max_spend} P · score {m['score']} "
          f"(neg {m['neg_points']}, ladder {m['ladder_points']}, rank {m['rank']}) · valor colección "
          f"{pl['model_value']}/{pl['server_value']} {'OK' if pl['valuation_verified'] else 'NO VERIFICADO'}")
    if ingested:
        print(f"   INTELIGENCIA market.db: +{ingested['offers']} ofertas nuevas, +{ingested['settlements']} liquidaciones, "
              f"+{ingested['evidence']} evidencias · {ingested['teams']} equipos perfilados")
    if pl.get("capital"):
        print("   " + ca.CapitalView(**pl["capital"]).line())
        if getattr(args, "capital_report", False) or getattr(args, "show", 0):
            capc = [c for c in cands if c.get("capital_cancel") or c.get("duplicate_pursuit")]
            print("   " + ca.CapitalView(**pl["capital"]).block(capc).replace("\n", "\n   "))
    if getattr(args, "open_bid_audit", False):
        print("   === OPEN BID AUDIT === (de peor a mejor; las pujas cuentan ENTERAS como obligación)")
        for b in pl.get("open_bids") or []:
            print(f"   #{b['offer']} {b['ref']} {b['venue']} {b['price']} P · creada t{b.get('created_tick')} · edad "
                  f"{b['age']} · caduca t{b.get('expires_tick')} · valor {b.get('our_value')} · reserva "
                  f"{b.get('reservation_price')} · ΔU esperado {b['expected_du']} · P {b['p_fill']} (n={b['sample_count']}, "
                  f"{b['confidence']}) · eficiencia {b['efficiency']} · {b.get('market_position')} (rival "
                  f"{b.get('best_competing_bid')}, ask {b.get('best_ask')}) · página {b.get('page_completion')} · "
                  f"prioridad {b.get('strategic_priority')}")
    camp = pl.get("page_campaign")
    if camp and camp.get("selection"):
        for pg_ in camp["selection"]["pages"][:4]:
            print(f"   PÁGINA {pg_['set']}: faltan {pg_['missing']} · ganancia {pg_['gain_all']} P · coste estimado "
                  f"{pg_['est_cost']} P · {'VIABLE' if pg_['feasible'] else 'no viable'}"
                  + (f" · sin oferta: {pg_['blocked']}" if pg_["blocked"] else ""))
    if camp and camp.get("state"):
        print("   " + pc.report_block(camp["state"], camp["plans"], camp["next_action"]).replace("\n", "\n   "))
        if camp.get("sequencing"):
            print(f"   Secuencia: ganancia de completar todo {camp['sequencing']['total_gain_all']} P · valor de cada "
                  f"carta si es la ÚLTIMA {camp['sequencing']['gain_if_last']} (conviene dejar para el final la más "
                  "disponible)")
    for rep_ in pl.get("tactical_sales") or []:
        print("   " + ts.report_block(rep_).replace("\n", "\n   "))
    for line in dealer_lines(pl.get("dealer_diag") or {}):
        print(f"   {line}")
    if pl.get("pilar"):
        print("   " + pilar_line(pl["pilar"]))
    if pl.get("ladder_cal"):
        for line in ladder_cal_lines(pl["ladder_cal"]):
            print("   " + line)
    performance = perf.realized(led["actions"], {d: [None] * n for d, n in (pl.get("ladder") or {}).items()},
                                sum(1 for o in s["offers"].get("offers", []) if o.get("maker") == team
                                    and o.get("status") == "open"))
    print("   " + perf.line(performance))
    if flags:
        print("   AVISO actividad no registrada por este ordenador: " + "; ".join(flags))
    for line in camp_lines:
        print(f"   {line}")
    if getattr(args, "no_rival_venues", False):
        for c in cands:
            why = rival_venue_blocker(c, s)
            if why:
                c["blockers"] = list(c.get("blockers") or []) + [why]
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
                                               "flags": flags, "performance": performance})
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


TRANSIENT = {"rate_limited", "network", "wait_for_tick"}


def intel_reports(args, intel, team):
    """Informes de solo lectura de la capa de inteligencia (no envían nada)."""
    if intel is None:
        return
    j = lambda x: json.dumps(x, ensure_ascii=False, indent=1, default=str)
    if getattr(args, "intel_db_stats", False):
        print("=== MARKET.DB ===\n" + j(intel.stats()))
    if getattr(args, "intel_team", None):
        print(f"=== EQUIPO {args.intel_team} ===\n" + j(intel.context(args.intel_team).as_dict()))
    if getattr(args, "intel_card", None):
        ref = args.intel_card
        print(f"=== CARTA {ref} ===\nquién la quiere:\n" + j(intel.best_buyers(ref)) +
              "\nquién la tiene (evidencia publicada):\n" + j(intel.best_counterparties(ref)))
    if getattr(args, "intel_counterparties", None):
        print(f"=== MEJORES CONTRAPARTES para {args.intel_counterparties} ===\n" +
              j(intel.best_counterparties(args.intel_counterparties)))


def run_loop(api, reader, args, led, journal, cycle_fn=None):
    """El coordinador es el proceso vivo: observa durante TODOS los ticks pedidos. Un tick sin oportunidades no
    implica nada sobre el siguiente, y un límite de peticiones o un fallo de red transitorio no lo detiene (las
    escrituras ambiguas siguen bloqueando hasta reconciliar). Devuelve los ticks recorridos."""
    cycle_fn = cycle_fn or cycle
    cache, done = SlowCache(), 0
    total = args.ticks if args.execute else 1
    import inspect
    try:  # un envoltorio externo (memory_coordinator) puede no aceptar la caché
        takes_cache = "cache" in inspect.signature(cycle_fn).parameters or \
            any(p.kind == p.VAR_POSITIONAL for p in inspect.signature(cycle_fn).parameters.values())
    except (TypeError, ValueError):
        takes_cache = False
    for i in range(total):
        try:
            if takes_cache:
                cycle_fn(reader, args, led, journal, args.execute, cache)
            else:
                cycle_fn(reader, args, led, journal, args.execute)
        except BazaarError as e:
            if e.code not in TRANSIENT or not args.execute:
                raise
            print(f"   AVISO {e.code}: {e.message} · se reintenta en el siguiente tick (lo enviado está en el registro)")
            save(led)
        done += 1
        if i + 1 < total and args.execute:
            wait_next_tick(api)
    return done


def wait_next_tick(api):
    for attempt in range(5):
        try:
            return api.wait_tick()
        except BazaarError as e:
            if e.code not in TRANSIENT:
                raise
            time.sleep(1.0 + attempt)
    time.sleep(5.0)


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
    p.add_argument("--no-rival-venues", action="store_true",
                   help="no publicar ni aceptar en venues de otros equipos (les suma market-making); usar con "
                        "--duende-venue rastro o nuestro venue")
    p.add_argument("--duende-expiry", type=int, default=120, help="expires_in_ticks en El Duende (recomendación oficial)")
    p.add_argument("--max-posts", type=int, default=4, help="publicaciones/cancelaciones por tick (≤ límite del servidor)")
    p.add_argument("--intel", type=int, default=6, help="cartas a detallar en el informe de inteligencia (0 = ninguno)")
    p.add_argument("--history-window", type=int, default=240, help="ventana del historial de mercado en ticks")
    p.add_argument("--dealer-liquidity", type=int, default=40,
                   help="liquidez para vendedores sin negociación activa (P; con negociaciones = su mayor máximo)")
    p.add_argument("--min-cancel-gain", type=float, default=2.0,
                   help="ventaja mínima de una oportunidad sobre las pujas que cancela para liberar capital (P)")
    p.add_argument("--rebalance-threshold", type=int, default=20,
                   help="por debajo de este capital de mercado libre se retiran las pujas obsoletas (P)")
    p.add_argument("--stale-age", type=int, default=30, help="edad en ticks a partir de la cual una puja es obsoleta")
    p.add_argument("--tactical-buffer", type=int, default=30,
                   help="colchón táctico para oportunidades inmediatas/dirigidas; las pujas pasivas no lo usan (P)")
    p.add_argument("--max-passive-frac", type=float, default=0.5,
                   help="fracción máxima de (efectivo − reserva dura) que pueden inmovilizar las pujas pasivas")
    p.add_argument("--dealer-buffer-mode", choices=["active_max", "fixed", "off"], default="active_max",
                   help="liquidez de vendedores: mayor máximo activo (por defecto), fija (--dealer-liquidity) o nada")
    p.add_argument("--sale-target", action="append", default=None,
                   help="venta táctica de un duplicado: REF=PRECIO objetivo (repetible). Por defecto LAT-10=86")
    p.add_argument("--directed-buy", action="append", default=None,
                   help="compra dirigida REF@EQUIPO=PRECIO (orden humana; se envía una vez como puja con `to`)")
    p.add_argument("--override-value", action="store_true",
                   help="permite que --directed-buy tenga ΔU < margen (solo esa regla; reserva y presupuesto siguen)")
    p.add_argument("--directed-expiry", type=int, default=20, help="ticks de vida pedidos para la compra dirigida")
    p.add_argument("--profile", choices=sorted(PROFILES), default=None,
                   help="perfil de estrategia: fast-close = cierre rápido + colección (margen 1 P, 2 propuestas, 4 ticks)")
    p.add_argument("--page-campaign", default="MAL",
                   help="colección cuya página se completa con prioridad (por defecto MAL; 'none' la desactiva)")
    p.add_argument("--intel-team", help="informe de inteligencia de un equipo (solo lectura)")
    p.add_argument("--intel-card", help="quién tiene / quién quiere una carta (solo lectura)")
    p.add_argument("--intel-counterparties", help="mejores contrapartes para conseguir una carta (solo lectura)")
    p.add_argument("--intel-db-stats", action="store_true", help="estadísticas de data/market.db")
    p.add_argument("--capital-report", action="store_true", help="bloque === CAPITAL === con cancelaciones recomendadas")
    p.add_argument("--open-bid-audit", action="store_true", help="auditoría de cada puja abierta")
    p.add_argument("--campaign", choices=["none", "all", "collect", "sell", "swap"], default="none",
                   help="campaña de negociación directa con equipos: abre conversaciones, envía propuestas y acepta")
    p.add_argument("--campaign-ticks", type=int, default=30, help="duración de la campaña en ticks")
    p.add_argument("--campaign-budget", type=int, default=60, help="efectivo máximo de la campaña (P)")
    p.add_argument("--max-proposals", type=int, default=3, help="propuestas nuestras por conversación")
    p.add_argument("--negotiation-ticks", type=int, default=6, help="ticks máximos por negociación")
    p.add_argument("--max-conversations", type=int, default=2, help="negociaciones de campaña activas a la vez")
    g = p.add_argument_group("escalera de vendedores (opt-in; sin estos flags el comportamiento no cambia)")
    g.add_argument("--dealer-ladder", action="store_true",
                   help="política de escalera observada (Chato pasos +3/+4, apertura 0,70 y final-1 una vez; Abuela "
                        "pasos de 1 y apertura 0,60; poco comunes enrutadas a la Abuela; vendedores nuevos prudentes)")
    g.add_argument("--dealer-sell-dups", action="store_true",
                   help="vender duplicados comunes a la Abuela (pide 10, baja de 1 en 1; final ~6 P) sin romper páginas")
    g.add_argument("--dealer-sell-menu", action="store_true",
                   help="vender a cualquier vendedor que anuncie esa rareza en su menú (dealer['menu']['buys']): "
                        "suelo = valor privado + margen; se añade a --dealer-sell-dups/--pilar-sell/"
                        "--ladder-calibrated (no los sustituye, así que puede competir con sus propias aperturas)")
    g.add_argument("--dedupe-bids", action="store_true",
                   help="no pujar (y cancelar la puja abierta) por una carta que ya negociamos con un vendedor")
    g.add_argument("--ladder-new-open", dest="ladder_new_open", type=float, default=None,
                   help="vendedores nuevos (nivel 3): apertura en fracción de su precio (0.80)")
    g.add_argument("--ladder-new-step", dest="ladder_new_gap_frac", type=float, default=None,
                   help="vendedores nuevos: fracción de la brecha por contraoferta (0.35)")
    g.add_argument("--ladder-new-counters", dest="ladder_new_counters", type=int, default=None,
                   help="vendedores nuevos: contraofertas máximas (3)")
    g.add_argument("--ladder-new-ticks", dest="ladder_new_ticks", type=int, default=None,
                   help="vendedores nuevos: ticks máximos por conversación (10)")
    g.add_argument("--ladder-new-max-frac", dest="ladder_new_max_frac", type=float, default=None,
                   help="vendedores nuevos: nunca pagar más de esta fracción de su apertura (0.95)")
    g.add_argument("--ladder-sell-margin", dest="ladder_sell_margin", type=float, default=None,
                   help="venta a vendedores: excedente mínimo sobre el valor perdido (1.0 P)")
    g.add_argument("--ladder-sell-open", dest="ladder_sell_open", type=int, default=None,
                   help="venta a vendedores: primera petición (10 P)")
    g = p.add_argument_group("contrapartes, Pilar y escalera completa (opt-in, ladder_plus.py; sin ellos nada cambia)")
    g.add_argument("--deny-teams", default="",
                   help="no operar con estos equipos: ni ofertas dirigidas, ni aceptar las suyas, ni campañas "
                        "(p. ej. t12,t13,t14)")
    g.add_argument("--deny-margin", type=float, default=None,
                   help="con --deny-teams: permitirlo solo si nuestro excedente a valores privados es ≥ N P")
    g.add_argument("--pilar-sell", default=None, metavar="SET[:MULT]",
                   help="vender a Doña Pilar poco comunes/raras de esos barrios (p. ej. SAL:1.25; * = todos): pedir "
                        "33, bajar de 2 en 2, aceptar su final si ≥ valor privado, objetivo 3 tratos negociados")
    g.add_argument("--pilar-open", type=int, default=None, help="con --pilar-sell: primera petición (33 P; t04: 40)")
    g.add_argument("--pilar-step", type=int, default=None, help="con --pilar-sell: bajada por petición (2 P; t04: 1)")
    g.add_argument("--pilar-last-copy", action="store_true",
                   help="con --pilar-sell SET: vender también la ÚLTIMA copia de esos barrios (nunca la que mantiene "
                        "una página completa); equivale a --allow-last-copy SET")
    g.add_argument("--pilar-from-tick", type=int, default=None, help="con --pilar-sell: no abrir antes de este tick")
    g.add_argument("--pilar-until-tick", type=int, default=None, help="con --pilar-sell: no abrir después de este tick")
    g.add_argument("--pilar-id", default=lplus.PILAR, help="id de Pilar en /api/dealers")
    g.add_argument("--ladder-fill", action="store_true",
                   help="3 tratos negociados por vendedor desbloqueado (abuela, chato, pilar) antes que pujas pasivas: "
                        "--dealer-ladder con las aperturas de ESTRATEGIA_TOP3 y venta a Pilar")
    g = p.add_argument_group("escalera calibrada (opt-in, ladder_calibrated.py; sin ellos nada cambia)")
    g.add_argument("--ladder-calibrated", action="store_true",
                   help="perfiles medidos en los hilos reales: apertura extrema, paso corto, paciencia hasta final:true "
                        "(un «final» sin final:true es un farol), nunca por debajo del valor privado; plan de 3 tratos "
                        "por vendedor (Abuela, Chato, Pilar) por puntos/P, incluidas ventas de duplicados")
    g.add_argument("--ladder-profile", default=None, metavar="FICHERO.json",
                   help="con --ladder-calibrated: sobrescribe campos de los perfiles ({\"chato|buy|rare\": {\"step\": 4}})")
    g.add_argument("--ladder-budget", type=int, default=None,
                   help="con --ladder-calibrated: caja máxima de las COMPRAS del plan (por defecto, la libre para vendedores)")
    g.add_argument("--chato-mirror", choices=["off", "auto", "on"], default="off",
                   help="con --ladder-calibrated: Chato v3 refleja lo que cedemos; nunca quedarse quieto ni pasos de 1 "
                        "(auto = versión ≥ 3 en /api/dealers, persona.updated o «so did I» en el hilo)")
    g = p.add_argument_group("noticias (opt-in, news_watch.py; sin ellos nada cambia)")
    g.add_argument("--news-sell", action="store_true",
                   help="con una noticia de demanda viva (fuente fiable en su ventana o fila en el menú del vendedor), "
                        "abrir venta a ese vendedor de copias que no rompen página; suelo = valor privado + margen")
    g.add_argument("--news-margin", type=float, default=2.0, help="con --news-sell: margen sobre el valor privado (P)")
    g.add_argument("--fever-priority", action="store_true",
                   help="fiebres de /api/schedule (Pilar +25 %% por SAL): priorizar esas ventas mientras duran y "
                        "esperarlas si empiezan pronto")
    g.add_argument("--fever-wait", type=float, default=1.0,
                   help="con --fever-priority: horas de juego antes de la fiebre en que se esperan esas ventas")
    g.add_argument("--news-db", default=None, metavar="market.db",
                   help="con --news-sell: calibrar la fiabilidad por fuente con este market.db (solo lectura)")
    args = p.parse_args()
    if args.ladder_fill:
        args.dealer_ladder = True
    if not 1 <= args.ticks <= 120:
        p.error("--ticks entre 1 y 120")
    try:
        lplus.parse_pilar_sell(args.pilar_sell)
        if args.ladder_profile:
            lcal.load_profiles(args.ladder_profile)
    except (ValueError, OSError) as e:
        p.error(str(e))
    DATA.mkdir(exist_ok=True)
    if args.news_sell and args.news_db:
        NEWS_CAL["cal"] = nw.calibrate(args.news_db)
    # retries=3: el SDK solo reintenta lo seguro (rate_limited = rechazada sin ejecutar; fallos de red en LECTURAS).
    # Una escritura con fallo de red nunca se repite a ciegas: queda ambigua hasta reconciliar. wait_for_tick no se
    # reintenta aquí (wait_on_tick=False): lo gestiona el coordinador.
    api = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"],
                 wait_on_tick=False, retries=3)
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
    if args.sale_target is None:
        args.sale_target = ["LAT-10=86"]
    applied = apply_profile(args)
    if applied:
        print(f"PERFIL {args.profile}: {applied} (lo fijado explícitamente en la línea de órdenes manda)")
    try:
        me = api.me()
        led = load_ledger(me["id"])
        run_loop(api, reader, args, led, journal)
        intel_reports(args, INTEL_STATE.get("intel"), me["id"])
    finally:
        if args.execute:
            lock.release()


def _graceful_signals():
    """Ctrl+C y `kill`/`kill -INT` paran el coordinador de forma limpia (libera el bloqueo, deja el registro
    reconciliable) aunque se haya lanzado en segundo plano con nohup, donde el shell ignora SIGINT."""
    import signal

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    _graceful_signals()
    try:
        main()
    except (ValueError, BazaarError) as e:
        raise SystemExit(str(e))
    except KeyboardInterrupt:
        raise SystemExit("\nParado. Lo enviado está anotado en data/coordinator_ledger.json y se reconcilia al relanzar.")
