"""Simulación local: vendedores sintéticos contra la política rápida de primera compra, la adaptativa y la de +1 P.

    python3 sim.py              # escenarios con nombre y comparación sobre 2000 vendedores (semilla fija)

Los vendedores son un MODELO de lo que dicen las reglas: conceden solo si concedemos (más cuanto más subimos), repetir
precio no consigue nada y gasta paciencia, tienen un límite secreto y nombran una oferta final al cansarse. Responden un
tick después de nuestra oferta, como en el juego. Los números dependen de ese modelo y no son garantías del juego real.
"""
from __future__ import annotations

import random
import statistics

import negotiation as neg

DEALER, TEAM, ITEM = "abuela", "t99", "card:LAV-03"


class SimSeller:
    def __init__(self, opening, floor, patience, reciprocity, stop_after=None, final_at=None, silent_after=None,
                 tick=1, thread_id=1, item=ITEM):
        self.floor, self.patience, self.k = floor, patience, reciprocity
        self.stop_after, self.final_at, self.silent_after = stop_after, final_at, silent_after
        self.last_bid, self.turn, self.tick, self._id, self.item = 0, 0, tick, 0, item
        self.status, self.price, self.final, self.queued, self.deal_tick = "open", None, False, None, None
        kind, ref = item.split(":", 1)
        self.thread = {"id": thread_id, "team": TEAM, "with": DEALER, "topic": {"buy": {kind: ref}}, "created_tick": tick - 1,
                       "status": "open", "closed_reason": None, "messages": []}
        self._offer(opening)

    def _next_id(self):
        self._id += 1
        return self._id

    def _offer(self, price, final=False):
        for m in self.thread["messages"]:
            o = m.get("offer")
            if o and o["maker"] == DEALER and o["status"] == "open":
                o["status"] = "expired"
        oid = self._next_id()
        self.thread["messages"].append({"id": oid, "tick": self.tick, "sender": DEALER, "text": "", "offer": {
            "id": oid, "maker": DEALER, "to": TEAM, "thread": self.thread["id"], "status": "open", "final": final,
            "give": {"cash": 0, "assets": [], "types": [self.item]}, "want": {"cash": price, "assets": [], "types": []},
            "expires_tick": self.tick + 2}})
        self.ask, self.final = price, final

    def _close(self, status, reason=None, price=None):
        self.status, self.price = status, price
        self.thread["status"], self.thread["closed_reason"] = status, reason
        if status == "deal":
            self.deal_tick = self.tick

    def bid(self, price):
        """Nuestra contraoferta: queda registrada ahora y la vendedora responde en el tick siguiente (respond)."""
        self.thread["messages"].append({"id": self._next_id(), "tick": self.tick, "sender": TEAM, "price": price})
        self.queued = price

    def respond(self):
        price, self.queued = self.queued, None
        if price is None:
            return
        if self.final:
            return self._close("walked", "final_rejected")
        self.turn += 1
        if self.silent_after is not None and self.turn > self.silent_after:
            return  # no contesta: su última oferta caduca sola
        if price <= self.last_bid:  # mismo precio (o menor): ninguna concesión y más impaciencia
            self.patience -= 2
            conc = 0
        else:
            self.patience -= 1
            stopped = self.stop_after is not None and self.turn > self.stop_after
            raise_ = price - self.last_bid if self.last_bid else 0
            conc = 0 if stopped else max(1, round(self.k * raise_)) if raise_ else max(1, round(0.1 * (self.ask - self.floor)))
            self.last_bid = price
        new = max(self.floor, self.ask - conc)
        if price >= new:  # nuestra oferta cubre su precio: la acepta
            return self._close("deal", None, price)
        self._offer(new, final=(self.final_at is not None and self.turn >= self.final_at) or self.patience <= 0)

    def expire(self):
        for m in self.thread["messages"]:
            o = m.get("offer")
            if o and o["status"] == "open" and o["expires_tick"] < self.tick:
                o["status"] = "expired"

    def accept(self, offer_id):
        o = neg.find_offer(self.thread, offer_id)
        if o and o["status"] == "open":
            o["status"] = "accepted"
            self._close("deal", None, self.ask)

    def walk(self):
        self._close("abandoned", "buyer_walked")


def run(seller: SimSeller, policy, max_ticks: int = 80) -> dict:
    """Bucle por ticks: la política decide con lo que ve; la vendedora responde al tick siguiente."""
    for _ in range(max_ticks):
        if seller.status != "open":
            break
        d = policy(seller.thread, seller.tick)
        if d.action == "counter":
            seller.bid(d.price)
        elif d.action == "accept":
            seller.accept(d.offer_id)
        elif d.action == "abandon":
            seller.walk()
        seller.tick += 1
        seller.respond()
        seller.expire()
    bids = [m["price"] for m in seller.thread["messages"] if m["sender"] == TEAM]
    return {"status": seller.status, "price": seller.price, "turns": len(bids), "bids": bids,
            "ticks": (seller.deal_tick + 1 if seller.deal_tick is not None else seller.tick),  # +1: liquidación
            "repeats": sum(1 for a, b in zip(bids, bids[1:]) if b <= a), "reason": seller.thread["closed_reason"]}


def fast(ceiling: int, f: neg.FastConfig = None, total_ticks_left: int = 10 ** 6):
    f = f or neg.FastConfig()
    cfg = neg.Config()

    def policy(thread, tick):
        st = neg.state_from_thread(thread, DEALER, tick, cfg)
        conv_left = f.max_negotiation_ticks - neg.conversation_ticks_used(thread, tick)
        return neg.decide_fast(st, f, ceiling, conv_left, total_ticks_left - tick)
    return policy


def adaptive(ceiling: int, cfg: neg.Config = None, history=()):
    cfg = cfg or neg.Config()

    def policy(thread, tick):
        st = neg.state_from_thread(thread, DEALER, tick, cfg)
        m = neg.metrics(st, cfg)
        return neg.decide(st, cfg, ceiling, neg.estimate(list(history), st, m, cfg), m)
    return policy


def lowball(ceiling: int):
    """La trayectoria observada en el hilo #102: 35 % de la apertura y pasos pequeños (Config de la versión 2)."""
    return adaptive(ceiling, neg.Config(open_frac=0.35, step_frac=0.25, accept_gap=1, min_saving=0.5,
                                        start_room_frac=0.25))


def baseline(ceiling: int):
    """La estrategia del primer starter modificado: 40 % de la apertura, +1 P por turno hasta min(máximo, apertura - 1),
    acepta si su precio <= oferta + 1 o si es final y entra en el tope; al llegar al tope repite precio."""
    s = {"offer": None}

    def policy(thread, tick):
        st = neg.state_from_thread(thread, DEALER, tick, neg.Config())
        if st.awaiting_reply:
            return neg.Decision("wait", "")
        live, cap = st.live, min(ceiling, st.opening - 1)
        if s["offer"] is None:
            s["offer"] = max(1, int(st.opening * 0.4))
        if live and (live.price <= min(cap, s["offer"] + 1) or (live.final and live.price <= cap)):
            return neg.Decision("accept", "", live.price, live.offer_id)
        price, s["offer"] = s["offer"], min(cap, s["offer"] + 1)
        return neg.Decision("counter", "", price)
    return policy


POLICIES = (("rápida", fast), ("adaptativa", adaptive), ("apertura baja", lowball), ("+1 P/turno", baseline))


def population(n: int, seed: int = 7):
    rng = random.Random(seed)
    for _ in range(n):
        opening = rng.randint(12, 30)
        r = rng.random()
        seller = dict(opening=opening, floor=max(1, round(opening * rng.uniform(0.5, 1.0))),
                      patience=rng.randint(3, 14), reciprocity=rng.uniform(0.0, 1.0),
                      stop_after=rng.randint(0, 5) if r < 0.35 else None,
                      final_at=rng.randint(1, 3) if 0.35 <= r < 0.45 else None)
        yield seller, round(opening * rng.uniform(0.8, 1.3))


def summary(results, sellers):
    deals = [(r, s) for r, s in zip(results, sellers) if r["status"] == "deal"]
    fails = {}
    for r in results:
        if r["status"] != "deal":
            fails[r["reason"]] = fails.get(r["reason"], 0) + 1
    return {"n": len(results), "deals": len(deals), "rate": len(deals) / len(results),
            "mean_price_ratio": statistics.mean(r["price"] / s["opening"] for r, s in deals) if deals else None,
            "mean_ticks": statistics.mean(r["ticks"] for r, _ in deals) if deals else None,
            "at_opening": sum(1 for r, s in deals if r["price"] >= s["opening"]),
            "repeats": sum(r["repeats"] for r in results), "fails": fails}


def compare(n: int = 2000, seed: int = 7):
    pop = list(population(n, seed))
    sellers = [s for s, _ in pop]
    res = {name: [run(SimSeller(**s), make(c)) for s, c in pop] for name, make in POLICIES}
    print(f"\nComparación sobre {n} vendedores sintéticos (semilla {seed}); máximo del comprador 80-130 % de la apertura;"
          " un 35 % deja de conceder pronto (algunos desde el principio)")
    print(f"{'política':<14} {'cierres':>8} {'tasa':>6} {'precio/apertura':>16} {'ticks hasta compra':>19} "
          f"{'a precio inicial':>17} {'repeticiones':>13}")
    for name, rs in res.items():
        s = summary(rs, sellers)
        print(f"{name:<14} {s['deals']:>8} {s['rate']:>6.1%} {s['mean_price_ratio']:>16.1%} {s['mean_ticks']:>19.2f} "
              f"{s['at_opening']:>17} {s['repeats']:>13}")
        print(f"{'':<14} sin compra: {s['fails']}")
    names = [name for name, _ in POLICIES]
    both = [rows for rows in zip(*res.values()) if all(r["status"] == "deal" for r in rows)]
    print(f"Casos en que todas cierran ({len(both)}): precio medio " +
          " · ".join(f"{name} {statistics.mean(rows[i]['price'] for rows in both):.2f} P" for i, name in enumerate(names)))
    print("Las negociaciones sin compra cuentan en la tasa y en 'sin compra'; el precio solo se mide sobre los cierres.")


SCENARIOS = {
    "mantiene apertura, rentable": (dict(opening=17, floor=17, patience=8, reciprocity=0.0, stop_after=0), 18),
    "mantiene apertura > máximo": (dict(opening=17, floor=17, patience=8, reciprocity=0.0, stop_after=0), 15),
    "concede progresivamente": (dict(opening=17, floor=10, patience=12, reciprocity=0.8), 16),
    "deja de conceder": (dict(opening=17, floor=8, patience=12, reciprocity=0.8, stop_after=1), 16),
    "oferta final temprana": (dict(opening=17, floor=10, patience=12, reciprocity=0.8, final_at=1), 16),
    "respuesta que no llega": (dict(opening=17, floor=10, patience=12, reciprocity=0.8, silent_after=1), 16),
    "hilo #102 real (17 fijo)": (dict(opening=17, floor=17, patience=5, reciprocity=0.0, stop_after=0), 25),
}


def scenarios():
    print(f"{'escenario':<28} {'política':<14} {'resultado':<10} {'precio':>6} {'ticks':>5}  ofertas")
    for name, (seller, ceiling) in SCENARIOS.items():
        for pname, make in POLICIES:
            r = run(SimSeller(**seller), make(ceiling))
            print(f"{name:<28} {pname:<14} {r['status']:<10} {str(r['price']):>6} {r['ticks']:>5}  {r['bids']}")


if __name__ == "__main__":
    scenarios()
    compare()
