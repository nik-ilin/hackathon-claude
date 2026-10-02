"""Agente de compra con Abuela Carmen.

    ./run.sh --first-purchase               # PRIMERA COMPRA: una carta, negociación corta, termina al confirmarla
    ./run.sh                                # AUTÓNOMO: elige qué comprar, negocia, confirma, abre sobres y sigue
    ./run.sh --max-spend 100 --max-purchases 5
    ./run.sh --card LAT-03                  # una sola carta concreta
    ./run.sh --pack sobre_barrio            # un solo sobre
    ./run.sh --dry-run                      # solo lecturas: muestra qué elegiría y la decisión, sin enviar nada

La política de negociación está en negotiation.py (probada con sim.py y test_negotiation.py); aquí van las llamadas a la
API y la elección de qué comprar. Registro local en data/ (sin credenciales).
"""
import argparse
import math
import os
import statistics
import subprocess
import sys
import time

import negotiation as neg
from bazaar_sdk import Bazaar, BazaarError

VERSION = "3.0 (primera compra rápida; sobres valorados con tus valores privados, nunca con expected_book)"
DEALER = "abuela"
HERE = os.path.dirname(os.path.abspath(__file__))
ATTEMPT = "first_purchase.json"  # estado persistido del intento de primera compra
SETTLE_TICKS = 4      # ticks que esperamos a ver el artículo en nuestras manos antes de dejarlo pendiente
RETRY_TICKS = 15      # tras abandonar o perder un artículo, no lo volvemos a pedir durante estos ticks
EXPLAIN = {
    "persona_quota": "cupo de la abuela agotado esta hora",
    "sold_out": "sin existencias de ese artículo esta hora",
    "cooloff": "la abuela no quiere tratar contigo durante un rato",
    "locked": "dealer no desbloqueado todavía",
    "insufficient_cash": "no tienes efectivo suficiente",
    "thread_exists": "ya hay una conversación abierta con ella",
}


class Stop(Exception):
    """Fin de la sesión (juego cerrado en modo de un artículo, error grave...)."""


def env(name, default, cast=int):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


def parse_args():
    d = neg.Config()
    p = argparse.ArgumentParser(description="Compra a Abuela Carmen negociando; sin --card/--pack es autónomo.")
    what = p.add_mutually_exclusive_group()
    what.add_argument("--card", help="solo esta carta, p. ej. LAT-03")
    what.add_argument("--pack", help="solo este sobre, p. ej. sobre_barrio")
    p.add_argument("--budget", type=int, default=env("NEG_BUDGET", d.budget), help="máximo por artículo (P)")
    p.add_argument("--reserve", type=int, default=env("NEG_RESERVE", d.reserve), help="efectivo que no se toca (P)")
    p.add_argument("--margin", type=int, default=env("NEG_MIN_MARGIN", d.min_margin), help="beneficio mínimo (P)")
    p.add_argument("--max-spend", type=int, default=env("NEG_MAX_SPEND", 150), help="gasto máximo de la sesión (P)")
    p.add_argument("--max-purchases", type=int, default=env("NEG_MAX_PURCHASES", 8), help="compras máximas de la sesión")
    p.add_argument("--open-frac", type=float, default=env("NEG_OPEN_FRAC", d.open_frac, float))
    p.add_argument("--turn-budget", type=int, default=env("NEG_TURN_BUDGET", d.turn_budget))
    p.add_argument("--pack-value-factor", type=float, default=env("NEG_PACK_VALUE_FACTOR", d.pack_value_factor, float))
    fd = neg.FastConfig()
    p.add_argument("--first-purchase", action="store_true", help="una carta, negociación corta, termina tras confirmarla")
    p.add_argument("--max-negotiation-ticks", type=int, default=env("NEG_MAX_NEGOTIATION_TICKS", fd.max_negotiation_ticks))
    p.add_argument("--max-total-ticks", type=int, default=env("NEG_MAX_TOTAL_TICKS", fd.max_total_ticks))
    p.add_argument("--max-counteroffers", type=int, default=env("NEG_MAX_COUNTEROFFERS", fd.max_counteroffers))
    p.add_argument("--fast-open-frac", type=float, default=env("NEG_FAST_OPEN_FRAC", fd.open_frac, float))
    p.add_argument("--reset-attempt", action="store_true", help="empieza un intento de primera compra nuevo")
    p.add_argument("--dry-run", action="store_true", help="solo lecturas: muestra la elección y la decisión y sale")
    p.add_argument("--again", action="store_true", help="modo de un artículo: comprar otra copia aunque ya haya una")
    p.add_argument("--keep-sealed", action="store_true", help="no abrir los sobres comprados")
    a = p.parse_args()
    if not a.card and not a.pack:
        a.card, a.pack = os.environ.get("NEG_CARD") or None, os.environ.get("NEG_PACK") or None
    a.auto = not a.card and not a.pack and not a.first_purchase
    return a


def explain(code, until=None) -> str:
    return f"{code}: {EXPLAIN.get(code, code)}" + (f" (hasta el tick {until})" if until else "")


class Agent:
    def __init__(self, a):
        self.a = a
        self.cfg = neg.Config(budget=a.budget, reserve=a.reserve, min_margin=a.margin, open_frac=a.open_frac,
                              turn_budget=a.turn_budget, pack_value_factor=a.pack_value_factor)
        self.b = Bazaar(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), os.environ["BAZAAR_KEY"])
        self.journal = neg.Journal(os.path.join(HERE, "data"))
        self.fast = neg.FastConfig(open_frac=a.fast_open_frac, max_counteroffers=a.max_counteroffers,
                                   max_negotiation_ticks=a.max_negotiation_ticks, max_total_ticks=a.max_total_ticks)
        self.values = {}  # caché de valores privados por carta; se vacía tras cada compra
        self.est = None
        self.attempt = None  # intento de primera compra (persistido en data/first_purchase.json)

    # ------------------------------------------------------------------ catálogo y valores

    def setup(self):
        self.wait_open()
        self.catalog = self.b.catalog()
        self.menu = self.b.dealer(DEALER).get("menu") or {}
        self.cards = {c["id"]: {**c, "set": s["id"], "released": s.get("released")}
                      for s in self.catalog.get("sets", []) for c in s.get("cards", [])}
        me = self.b.me()
        self.team = me.get("id")
        print(f"Agente versión {VERSION}")
        print(f"{me.get('name')}: {me['cash']} P · nivel {me.get('level')} · multiplicadores {me.get('affinity')}")

    def wait_open(self):
        while True:
            c = self.b.clock()
            if not c.get("paused") and c.get("doors", "open") == "open":
                return c
            if not self.a.auto or self.a.dry_run:
                raise Stop(f"El juego está cerrado (sin ticks). Próxima apertura: {c.get('next_opens')}.")
            print(f"Juego cerrado. Próxima apertura {c.get('next_opens')}; vuelvo a mirar en 60 s.")
            time.sleep(60)

    def value_of(self, ref):
        if ref not in self.values:
            self.values[ref] = float(self.b.value(ref)["your_value"])
        return self.values[ref]

    def name(self, item):
        kind, ref = item.split(":", 1)
        if kind == "card":
            return self.cards.get(ref, {}).get("name", ref)
        return next((p.get("name", ref) for p in self.catalog.get("packs", []) if p["id"] == ref), ref)

    def list_price(self, item):
        kind, ref = item.split(":", 1)
        for s in self.menu.get("sells", []):
            if kind == "pack" and s.get("pack") == ref:
                return s.get("list_price")
            if kind == "card" and s.get("rarity") == self.cards.get(ref, {}).get("rarity"):
                return s.get("list_price")
        return None

    def item_value(self, item):
        """(valor, explicación). Carta: tu valor privado. Sobre: valor esperado con tus valores privados (estimación)."""
        kind, ref = item.split(":", 1)
        if kind == "card":
            return self.value_of(ref), "tu valor privado de una copia más"
        pack = next((p for p in self.catalog.get("packs", []) if p["id"] == ref), None)
        if pack is None:
            raise Stop(f"El catálogo no tiene el sobre {ref}.")
        mean = {}
        for slot in pack["slots"]:
            for rarity in slot:
                if rarity not in mean:
                    pool = [c for c in self.cards.values() if c["released"] and c["rarity"] == rarity and not c.get("hidden")]
                    mean[rarity] = statistics.mean(self.value_of(c["id"]) for c in pool) if pool else 0.0
        ev = sum(p * mean[r] for slot in pack["slots"] for r, p in slot.items())
        return ev * self.cfg.pack_value_factor, (f"ESTIMACIÓN: valor esperado {ev:.1f} P con tus valores privados "
                                                 f"(cartas equiprobables) x {self.cfg.pack_value_factor}")

    def expected_price(self, item, list_price):
        closes = [o["close_price"] for o in self.journal.outcomes(DEALER, item) if o.get("status") == "deal" and o.get("close_price")]
        if closes:
            return statistics.median(closes), f"mediana de {len(closes)} compras previas"
        return round(list_price * self.cfg.prior_close_frac, 1), f"lista {list_price} P x {self.cfg.prior_close_frac} (sin historial)"

    def candidates(self):
        """[(beneficio esperado, artículo, valor, precio esperado, base)] de lo que la abuela vende, de mejor a peor."""
        sells = self.menu.get("sells", [])
        rarities = {s["rarity"] for s in sells if "rarity" in s}
        items = [f"card:{c['id']}" for c in self.cards.values()
                 if c["released"] and c["rarity"] in rarities and not c.get("hidden")]
        items += [f"pack:{s['pack']}" for s in sells if "pack" in s]
        out = []
        for item in items:
            lp = self.list_price(item)
            if lp is None:
                continue
            value, _ = self.item_value(item)
            est, basis = self.expected_price(item, lp)
            out.append((round(value - est, 1), item, round(value, 1), est, basis))
        return sorted(out, reverse=True)

    # ------------------------------------------------------------------ modo autónomo

    def auto(self):
        self.setup()
        if self.resolve_pending() == "waiting":
            return
        bought, spent, blocked = 0, 0, {}  # blocked: artículo o "*" -> tick hasta el que no se pide
        while bought < self.a.max_purchases and spent < self.a.max_spend:
            clock = self.wait_open()
            tick = clock["tick"]
            open_ = [t for t in self.b.my_threads(status="open").get("threads", []) if t.get("with") == DEALER]
            thread = open_[0] if open_ else None
            if thread:
                item = neg.item_of(thread.get("topic"))
                print(f"\nRetomo la conversación abierta #{thread['id']} sobre {item}.")
            else:
                if blocked.get("*", 0) > tick:
                    self.sleep_ticks(blocked["*"] - tick, "la abuela no atiende ahora")
                    continue
                item = self.pick(blocked, tick, spent)
                if item is None:
                    waiting = [u for u in blocked.values() if u > tick]
                    if not waiting:
                        print("Nada de lo que vende la abuela deja beneficio ahora mismo. Fin de la sesión.")
                        return
                    self.sleep_ticks(min(waiting) - tick, "artículos rentables bloqueados temporalmente")
                    continue
            if self.a.dry_run:
                self.negotiate(item, thread, spent)
                return
            res = self.negotiate(item, thread, spent)
            tick = self.b.clock()["tick"]
            st, reason = res["status"], res.get("closed_reason")
            if st == "deal":
                bought, spent = bought + 1, spent + (res.get("price") or 0)
                self.values.clear()  # tus valores cambian con cada carta nueva
                print(f"Sesión: {bought} compras, {spent} P gastados (límites {self.a.max_purchases} y {self.a.max_spend} P).")
            elif st == "pending":
                print("Hay una liquidación sin confirmar: paro para no comprar dos veces. Vuelve a lanzar el agente.")
                return
            elif reason == "persona_quota":
                blocked[item] = self.next_hour_tick(clock)
                blocked["*"] = tick + 3
            elif reason == "cooloff":
                blocked["*"] = res.get("until_tick") or tick + 20
            elif reason == "sold_out":
                blocked[item] = self.next_hour_tick(clock)
            elif reason == "insufficient_cash":
                print("Sin efectivo suficiente. Fin de la sesión.")
                return
            else:
                blocked[item] = tick + RETRY_TICKS
        print(f"Límite de sesión alcanzado: {bought} compras, {spent} P gastados.")

    def pick(self, blocked, tick, spent):
        cash = self.b.me()["cash"]
        cands = self.candidates()
        print(f"\nEligiendo qué comprar entre {len(cands)} candidatos (beneficio esperado = valor - precio esperado):")
        for i, (surplus, item, value, est, basis) in enumerate(cands):  # todos los candidatos, no solo los primeros
            ceiling = min(neg.price_ceiling(self.cfg, cash, value)[0], self.a.max_spend - spent)
            ok = surplus >= self.cfg.min_margin and ceiling >= est and blocked.get(item, 0) <= tick
            if ok or i < 6:
                print(f"  {'->' if ok else '  '} {self.name(item):<32} {item:<20} valor {value:>5} · precio esperado "
                      f"{est:>5} ({basis}) · beneficio {surplus:>5}" + ("" if ok else " · descartado"))
            if ok:
                return item
        return None

    def next_hour_tick(self, clock):
        t_hours, ts = clock.get("t_hours", 0), clock.get("tick_seconds", 60) or 60
        return clock["tick"] + max(1, math.ceil((math.floor(t_hours) + 1 - t_hours) * 3600 / ts))

    def sleep_ticks(self, n, why):
        n = max(1, min(n, 30))
        print(f"Espero {n} ticks: {why}.")
        for _ in range(n):
            self.b.wait_tick()

    # ------------------------------------------------------------------ modo primera compra

    def first_purchase(self):
        """Una carta: elegir, negociar corto, aceptar, confirmar la entrega y terminar. El plazo global se persiste."""
        self.setup()
        clock = self.b.clock()
        tick, ts = clock["tick"], clock.get("tick_seconds") or 60
        att = self.journal.load(ATTEMPT)
        if att and att.get("status") == "done" and not self.a.reset_attempt:
            raise Stop(f"La primera compra ya está confirmada ({att['purchases'][-1]}). Usa --reset-attempt para otra.")
        if not att or self.a.reset_attempt:  # un intento agotado no se reinicia solo al relanzar
            att = {"version": VERSION, "start_tick": tick, "max_total_ticks": self.fast.max_total_ticks, "spent": 0,
                   "purchases": [], "tried": [], "active_thread": None, "status": "running"}
        self.attempt = att
        pending = self.resolve_pending()  # una aceptación sin confirmar se resuelve antes que nada
        if pending == "deal":
            return self.finish_attempt(self.last_purchase)
        if pending == "waiting":
            self.save_attempt()
            raise Stop("Hay una aceptación pendiente de liquidación: no abro otra compra. Vuelve a lanzar el agente.")
        open_ = [t for t in self.b.my_threads(status="open").get("threads", []) if t.get("with") == DEALER]
        for t in open_:  # el tiempo de los hilos retomados cuenta
            att["start_tick"] = min(att["start_tick"], int(t.get("created_tick", tick)))
        self.save_attempt()
        f = self.fast
        print(f"Modo primera compra · máximo por carta {self.cfg.budget} P · gasto total {self.a.max_spend} P · reserva "
              f"{self.cfg.reserve} P · margen {self.cfg.min_margin} P")
        print(f"Plazos: {f.max_negotiation_ticks} ticks por conversación, {f.max_total_ticks} en total (desde el tick "
              f"{att['start_tick']}), {f.max_counteroffers} contraofertas. Al ritmo actual ({ts:.0f} s/tick): "
              f"~{f.max_negotiation_ticks * ts / 60:.0f} min y ~{f.max_total_ticks * ts / 60:.0f} min; "
              "el ritmo cambia según el día.")
        while True:
            tick = self.wait_open()["tick"]
            left = self.total_ticks_left(tick)
            open_ = [t for t in self.b.my_threads(status="open").get("threads", []) if t.get("with") == DEALER]
            thread = open_[0] if open_ else None
            item = neg.item_of(thread.get("topic")) if thread else None
            if thread and not (item or "").startswith("card:"):
                print(f"\nLa conversación abierta #{thread['id']} es sobre {item} y este modo compra una carta: la cierro.")
                if self.a.dry_run:
                    print("   (dry-run) no se cierra nada.")
                    thread = None
                else:
                    self.close_unwanted(thread)
                    continue
            if thread:
                print(f"\nRetomo la conversación #{thread['id']} sobre {item} "
                      f"({neg.conversation_ticks_used(thread, tick)} ticks usados).")
            else:
                if left <= 0:
                    att["status"] = "expired"
                    self.save_attempt()
                    print(f"Plazo global agotado ({f.max_total_ticks} ticks desde el tick {att['start_tick']}) sin compra: "
                          "no inicio otra negociación. Usa --reset-attempt para un intento nuevo.")
                    return
                item = self.pick_card(att["tried"])
                if item is None:
                    print("Ninguna carta disponible es económicamente válida con estos límites. Fin, sin compra.")
                    return
            res = self.negotiate(item, thread, att["spent"], fast=True)
            if self.a.dry_run:
                return
            if res["status"] == "deal":
                return self.finish_attempt({"item": item, "price": res.get("price"), "tick": self.b.clock()["tick"]})
            if res["status"] == "pending":
                self.save_attempt()
                raise Stop("Liquidación sin confirmar: no compro otra cosa. Vuelve a lanzar el agente para comprobarla.")
            if res.get("closed_reason") in ("insufficient_cash", "cooloff", "persona_quota", "locked"):
                self.save_attempt()
                raise Stop(f"La abuela no puede venderte ahora: {explain(res['closed_reason'], res.get('until_tick'))}.")
            att["tried"].append(item)
            att["active_thread"] = None
            self.save_attempt()

    def total_ticks_left(self, tick):
        att = self.attempt
        return att["start_tick"] + att["max_total_ticks"] - tick if att else 10 ** 9

    def save_attempt(self):
        if self.attempt is not None and not self.a.dry_run:
            self.journal.save(ATTEMPT, self.attempt)

    def finish_attempt(self, purchase):
        att = self.attempt
        att["purchases"].append(purchase)
        att["spent"] += purchase.get("price") or 0
        att["status"], att["active_thread"] = "done", None
        self.save_attempt()
        print(f"Primera compra completada: {purchase}. Fin.")

    def close_unwanted(self, thread):
        st = neg.state_from_thread(thread, DEALER, 0, self.cfg)
        self.b.close_thread(thread["id"])
        self.journal.append("outcome", neg.outcome_record(
            st, dealer=DEALER, thread_id=thread["id"], status="abandoned", closed_reason="buyer_switched",
            close_price=None, ceiling=0, est=None, settled=False, note="modo primera compra: cambio a una carta"))

    def pick_card(self, tried):
        """Todas las cartas sueltas que vende la abuela, con su máximo económico; ver neg.rank_cards."""
        me = self.b.me()
        rarities = {s["rarity"]: s.get("list_price") for s in self.menu.get("sells", []) if "rarity" in s}
        rows = []
        for c in self.cards.values():
            item = f"card:{c['id']}"
            if not c["released"] or c.get("hidden") or c["rarity"] not in rarities or item in tried:
                continue
            value = self.value_of(c["id"])
            ceiling = min(neg.price_ceiling(self.cfg, me["cash"], value)[0], self.a.max_spend - self.attempt["spent"])
            rows.append({"item": item, "name": c.get("name", c["id"]), "value": value, "ceiling": ceiling,
                         "list_price": rarities[c["rarity"]], "owned": neg.holdings(me.get("assets", []), item)})
        ranked = neg.rank_cards(rows, self.fast)
        viable = [c for c in ranked if c.tier < 2]
        print(f"\nCartas candidatas: {len(rows)} evaluadas, {len(viable)} viables "
              f"({sum(c.tier == 0 for c in ranked)} al precio publicado). Mejores:")
        for c in ranked[:8]:
            print(f"   {'->' if c is (viable[0] if viable else None) else '  '} {c.name:<28} {c.item:<13} valor {c.value:>5.1f}"
                  f" · publicado {c.list_price} P · máximo {c.ceiling} P · {'la tienes' if c.owned else 'no la tienes'}"
                  f" · {c.why}")
        return viable[0].item if viable else None

    # ------------------------------------------------------------------ modo de un artículo

    def single(self):
        self.setup()
        if self.resolve_pending():
            return
        item = f"card:{self.a.card}" if self.a.card else f"pack:{self.a.pack}"
        self.reconcile(item)
        if self.journal.purchased(DEALER, item) and not self.a.again:
            raise Stop(f"El registro ya tiene una compra de {item}. Usa --again para comprar otra copia.")
        open_ = [t for t in self.b.my_threads(status="open").get("threads", []) if t.get("with") == DEALER]
        same = next((t for t in open_ if neg.item_of(t.get("topic")) == item), None)
        for t in open_:
            if t is not same:
                if self.a.dry_run:
                    raise Stop(f"Hay una conversación abierta sobre {neg.item_of(t.get('topic'))} (#{t['id']}).")
                self.b.close_thread(t["id"])
                print(f"Cerrada la conversación #{t['id']} sobre {neg.item_of(t.get('topic'))} (solo se permite una).")
        self.negotiate(item, same, 0)

    def reconcile(self, item):
        """Tratos cerrados que el registro no conoce (el agente se cortó): cuentan como compras ya hechas."""
        known = self.journal.known_threads()
        for t in self.b.my_threads(status="deal").get("threads", []):
            if t.get("with") == DEALER and neg.item_of(t.get("topic")) == item and t["id"] not in known:
                st = neg.state_from_thread(t, DEALER, 0, self.cfg)
                self.journal.append("outcome", neg.outcome_record(
                    st, dealer=DEALER, thread_id=t["id"], status="deal", closed_reason=t.get("closed_reason"),
                    close_price=neg.settled_price(t, DEALER), ceiling=0, est=None, settled=True,
                    note="reconciliado desde el servidor"))

    # ------------------------------------------------------------------ una negociación

    def negotiate(self, item, thread, spent, fast=False) -> dict:
        """Negocia un artículo hasta comprarlo, abandonarlo o perderlo. Devuelve {status, price, closed_reason, ...}.
        fast=True usa la política corta de primera compra (neg.decide_fast) con plazos por conversación y globales."""
        name = self.name(item)
        me = self.b.me()
        value, note = self.item_value(item)
        self.ceiling, parts = neg.price_ceiling(self.cfg, me["cash"], value)
        if self.a.auto or fast:
            parts["gasto total restante"] = self.a.max_spend - spent
            self.ceiling = min(self.ceiling, parts["gasto total restante"])
        lp = self.list_price(item)
        print(f"\n== {name} ({item}) · valor {value:.1f} P, {note} · precio publicado {lp} P (no es una oferta ejecutable)")
        print(f"   precio máximo {self.ceiling} P = min(" + ", ".join(f"{k} {v}" for k, v in parts.items()) + ")")
        if self.ceiling < 1 and not thread:
            print("   sin margen económico: no abro conversación")
            return {"status": "skipped", "closed_reason": "no_margin"}
        if thread is None:
            if self.a.dry_run:
                print(f"   (dry-run) abriría una conversación con la abuela sobre {item}")
                return {"status": "skipped"}
            kind, ref = item.split(":", 1)
            try:
                thread = self.b.open_thread(DEALER, topic={"buy": {kind: ref}})
            except BazaarError as e:
                print(f"   la abuela no abre conversación: {explain(e.code, e.extra.get('until_tick'))}")
                return {"status": "refused", "closed_reason": e.code, "until_tick": e.extra.get("until_tick")}
            if self.attempt is not None:
                self.attempt["active_thread"] = thread["id"]
                self.save_attempt()
        self.start_cash, self.start_holdings = me["cash"], neg.holdings(me.get("assets", []), item)
        errors = 0
        while True:
            clock = self.wait_open()
            t = self.observe(thread["id"], clock["tick"])
            st = neg.state_from_thread(t, DEALER, clock["tick"], self.cfg)
            m = neg.metrics(st, self.cfg)
            self.est = neg.estimate(self.journal.outcomes(DEALER, item), st, m, self.cfg)
            if t["status"] != "open":
                return self.finished(t, st, item, name)
            if fast:
                conv_left = self.fast.max_negotiation_ticks - neg.conversation_ticks_used(t, clock["tick"])
                total_left = self.total_ticks_left(clock["tick"])
                d = neg.decide_fast(st, self.fast, self.ceiling, conv_left, total_left)
            else:
                conv_left = total_left = None
                d = neg.decide(st, self.cfg, self.ceiling, self.est, m)
            self.report(clock["tick"], st, m, d, fast, conv_left, total_left)
            if self.a.dry_run:
                print("   (dry-run) no se envía nada.")
                return {"status": "skipped"}
            if d.action != "wait":
                self.journal.append("decision", {"dealer": DEALER, "item": item, "thread": t["id"], "tick": clock["tick"],
                                                 "turn": st.turns, "action": d.action, "price": d.price,
                                                 "mode": "first_purchase" if fast else "normal", "version": VERSION,
                                                 "reason": d.reason, "ceiling": self.ceiling, "estimate": vars(self.est)})
            try:
                if d.action == "counter":
                    self.b.say(t["id"], neg.message(st.turns, d.price, name), price=d.price)
                elif d.action == "accept":
                    res = self.accept(t, st, d, clock["tick"], item, name)
                    if res:
                        return res
                elif d.action == "abandon":
                    self.b.close_thread(t["id"])
                    self.journal.append("outcome", neg.outcome_record(
                        st, dealer=DEALER, thread_id=t["id"], status="abandoned", closed_reason="buyer_walked",
                        close_price=None, ceiling=self.ceiling, est=self.est, settled=False, note=d.reason))
                    print(f"   abandono: {d.reason}")
                    return {"status": "abandoned", "closed_reason": "buyer_walked"}
                errors = 0
            except BazaarError as e:
                errors += 1
                print(f"   rechazado: {explain(e.code, e.extra.get('until_tick'))} {e.message}")
                if e.code in ("insufficient_cash", "persona_quota", "cooloff", "sold_out", "locked") or errors >= 3:
                    return {"status": "error", "closed_reason": e.code, "until_tick": e.extra.get("until_tick")}
            self.b.wait_tick()

    def observe(self, thread_id, tick):
        """Lee el hilo. Si esperamos una respuesta que ya debería existir (enviamos en un tick anterior, o el hilo se
        abrió antes de este tick) pero aún no se ve, relee brevemente: al cambiar de tick el servidor puede tardar
        en publicarla. Como mucho 3 relecturas en ~2 s, muy por debajo del límite de 5 peticiones por segundo."""
        t = self.b.thread(thread_id)
        for _ in range(3):
            st = neg.state_from_thread(t, DEALER, tick, self.cfg)
            due = (st.awaiting_reply and st.rounds[-1].tick < tick) or \
                  (st.current is None and int(t.get("created_tick", tick)) < tick)
            if t.get("status") != "open" or not due:
                break
            time.sleep(0.7)
            t = self.b.thread(thread_id)
        return t

    def report(self, tick, st, m, d, fast=False, conv_left=None, total_left=None):
        live = st.live
        rejected = f"{st.last_ours} P" if st.rounds and not st.awaiting_reply else "ninguna"
        ask = f"{live.price} P" + (" (final)" if live.final else "") if live else "ninguna vigente"
        rec = f"{m.reciprocity:.2f}" if m.reciprocity is not None else "-"
        if fast:
            print(f"[tick {tick}] contraofertas {st.turns}/{self.fast.max_counteroffers} · plazo conversación "
                  f"{conv_left} ticks · plazo global {total_left} ticks · apertura {st.opening} P · máximo {self.ceiling} P")
        else:
            print(f"[tick {tick}] turno {st.turns}/{self.cfg.turn_budget} · apertura {st.opening} P · máximo {self.ceiling} P")
        print(f"   nuestras ofertas {[r.ours for r in st.rounds]} · sus respuestas {[r.reply for r in st.rounds]}")
        print(f"   observable: nuestra última oferta rechazada {rejected} · su oferta ejecutable {ask}")
        print(f"   brecha {m.gap} · su descenso {m.her_drop} · nuestra subida {m.our_raise} · reciprocidad {rec} · "
              f"sin concesión {m.stalled}")
        if self.est.low is not None:
            print(f"   zona {self.est.low}-{self.est.high} P · base: {self.est.strength} (n={self.est.n}): "
                  + "; ".join(self.est.evidence))
        print(f"   decisión: {d.action}{f' {d.price} P' if d.price else ''} · {d.reason}")

    # ------------------------------------------------------------------ aceptación y liquidación

    def resolve_asset(self, asset_id):
        c = self.b.card(asset_id)
        return f"{c.get('kind', 'card')}:{c.get('ref')}"

    def accept(self, t, st, d, tick, item, name):
        """Valida la estructura, anota la aceptación ANTES de enviarla y confirma la liquidación. None = seguir."""
        me = self.b.me()
        offer = neg.find_offer(t, d.offer_id) or {}
        ceiling = min(self.ceiling, me["cash"] - self.cfg.reserve)  # se revalida con el efectivo de este momento
        problems = neg.validate_offer(offer, dealer=DEALER, team=self.team or t.get("team"), thread_id=t["id"],
                                      item=item, ceiling=ceiling, cash=me["cash"], now_tick=tick,
                                      resolve_asset=self.resolve_asset)
        if problems:
            print("   no acepto, la estructura no cuadra: " + "; ".join(problems))
            return None
        pending = {"thread": t["id"], "offer_id": d.offer_id, "item": item, "price": d.price, "tick": tick,
                   "cash_before": me["cash"], "holdings_before": neg.holdings(me.get("assets", []), item)}
        self.journal.set_pending(pending)
        try:
            self.b.accept(d.offer_id)
        except BazaarError as e:
            if e.status:  # rechazada por el servidor: no se movió nada
                self.journal.clear_pending()
                print(f"   aceptación rechazada: {explain(e.code)}")
                return {"status": "error", "closed_reason": e.code} if e.code == "insufficient_cash" else None
            print("   la respuesta se perdió: la aceptación pudo llegar, compruebo la liquidación")
        print(f"   aceptada la oferta de {d.price} P: se liquida en el siguiente tick")
        self.b.wait_tick()
        return self.settle(pending, name)

    def settle(self, pending, name=None):
        name = name or self.name(pending["item"])
        for _ in range(SETTLE_TICKS):
            t, me = self.b.thread(pending["thread"]), self.b.me()
            status, note = neg.settlement_status(pending, t, neg.holdings(me.get("assets", []), pending["item"]), me["cash"])
            if status == "settled":
                st = neg.state_from_thread(t, DEALER, 0, self.cfg)
                paid = neg.settled_price(t, DEALER)
                if paid is not None and paid != pending.get("price"):
                    note = f"{note} el servidor liquidó {paid} P, no {pending.get('price')} P".strip()
                    pending = {**pending, "price": paid}
                self.journal.append("outcome", neg.outcome_record(
                    st, dealer=DEALER, thread_id=t["id"], status="deal", closed_reason=t.get("closed_reason"),
                    close_price=pending.get("price"), ceiling=getattr(self, "ceiling", 0), est=self.est, settled=True,
                    note=note))
                self.journal.clear_pending()
                saving = f", {st.opening - pending['price']} P menos que su apertura" if st.opening and pending.get("price") else ""
                print(f"COMPRA CONFIRMADA: {name} por {pending.get('price')} P{saving} en {st.turns} turnos. {note}")
                self.last_purchase = {"item": pending["item"], "price": pending.get("price"), "thread": t["id"]}
                self.open_pack(me, pending["item"])
                return {"status": "deal", "price": pending.get("price")}
            if status == "failed":
                self.journal.clear_pending()
                print(f"   la aceptación no se liquidó ({note})")
                return {"status": "failed", "closed_reason": t.get("closed_reason")}
            print(f"   {note}")
            self.b.wait_tick()
        print("   liquidación sin confirmar: queda anotada como pendiente (no se aceptará de nuevo).")
        return {"status": "pending"}

    def resolve_pending(self):
        """None si no hay nada pendiente; si no, 'deal', 'failed' o 'waiting'. Nunca vuelve a aceptar."""
        pending = self.journal.pending()
        if not pending:
            return None
        print(f"Hay una aceptación pendiente (oferta {pending.get('offer_id')} a {pending.get('price')} P): la compruebo.")
        if self.a.dry_run:
            return "waiting"
        res = self.settle(pending)
        return "waiting" if res["status"] == "pending" else res["status"]

    def finished(self, t, st, item, name):
        if t["status"] == "deal":  # la abuela aceptó nuestra oferta vigente
            return self.settle(self.journal.pending() or {
                "thread": t["id"], "offer_id": None, "item": item, "tick": None,
                "price": neg.settled_price(t, DEALER) or st.last_ours,
                "cash_before": self.start_cash, "holdings_before": self.start_holdings}, name)
        reason = t.get("closed_reason")
        self.journal.append("outcome", neg.outcome_record(
            st, dealer=DEALER, thread_id=t["id"], status=t["status"], closed_reason=reason, close_price=None,
            ceiling=self.ceiling, est=self.est, settled=False))
        print(f"   la conversación terminó sin compra ({t['status']}): {explain(reason, t.get('until_tick'))}")
        return {"status": t["status"], "closed_reason": reason, "until_tick": t.get("until_tick")}

    def open_pack(self, me, item):
        if not item.startswith("pack:") or self.a.keep_sealed:
            return
        sealed = next((x for x in me.get("assets", []) if f"{x.get('kind')}:{x.get('ref')}" == item), None)
        if sealed:
            for c in self.b.open_pack(sealed["id"])["cards"]:
                print(f"   sale {c['name']} · {c['rarity']} · #{c['serial']}/{c['print_run']}")


def other_agents() -> list:
    """Procesos de starter_agent.py distintos de este (también los de versiones antiguas, que no usan el bloqueo)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines()
            if "starter_agent.py" in line and "python" in line.lower() and int(line.split()[0]) != os.getpid()]


def how_to_stop(pid) -> str:
    return (f"Para pararla sin duplicar acciones: pulsa Ctrl+C en su terminal (o ejecuta: kill -INT {pid}) y espera a que "
            "salga. Al relanzar, este agente reconcilia con el servidor: retoma el hilo abierto, comprueba cualquier "
            "aceptación pendiente y no vuelve a aceptar.")


def main():
    a = parse_args()
    code_mtime = round(os.path.getmtime(os.path.abspath(__file__)))
    lock = neg.InstanceLock(os.path.join(HERE, "data", "agent.lock"), {"version": VERSION, "code_mtime": code_mtime})
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    others = other_agents()
    holder = None if a.dry_run or others else lock.acquire()  # con otra instancia viva no se toma el bloqueo
    if holder or (others and not a.dry_run):
        pid = holder.get("pid") if holder else others[0].split()[0]
        old = (not holder) or holder.get("code_mtime", 0) < code_mtime
        print(f"Hay otra instancia del agente en marcha (pid {pid}). Una instancia de Python ya iniciada no carga los "
              "cambios del código" + (": esa ejecuta una versión anterior." if old else "."))
        for line in others:
            print(f"   {line}")
        sys.exit(how_to_stop(pid))
    if others:
        print(f"Aviso: hay otra instancia en marcha ({others[0]}); el dry-run solo lee, así que continúo.")
    agent = Agent(a)
    try:
        if a.first_purchase:
            agent.first_purchase()
        elif a.auto:
            agent.auto()
        else:
            agent.single()
    except Stop as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("\nParado. Las conversaciones abiertas y las aceptaciones pendientes se retoman al relanzar.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
