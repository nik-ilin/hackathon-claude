"""Simulador offline de los vendedores de la escalera (Abuela, Chato, Pilar), calibrado con los hilos reales.

    python3 dealer_sim.py                 # captura esperada ANTES (main) / DESPUÉS (--ladder-calibrated), semilla fija
    python3 dealer_sim.py --n 3000 --seed 3
    python3 dealer_sim.py --replay FICHERO.json   # valida el modelo reproduciendo aperturas/pasos reales (ladder_measure)

Modelo (patrones medidos en intel/market.db; ver ladder_calibrated.py):
  * Abre a `open`; tras cada oferta nuestra concede según un calendario de Boulware hacia su límite secreto (uno por
    conversación, al azar en `limit`):
    precio(k) = open ∓ |open − limit| · ((k − stall)/(T − stall))^beta, con `stall` rondas planas al principio.
  * `mirror` (Chato): nunca concede más que nuestro último paso; repetir precio = 0 («small steps earn small steps»).
  * Acepta nuestra oferta si está a ≤ 1 P de su nuevo precio (t09 84 frente a 85, t05 30 frente a 31, t17 8 frente a 9).
  * Paciencia T (rondas) aleatoria por conversación; en la ronda T nombra su oferta con `final: true`. Una contraoferta
    final∓1 que respete su límite se acepta con probabilidad `p_counter_final` (Abuela/Chato 3/3 observadas); si no, se va.
  * Pilar farolea: con probabilidad `p_bluff` dice «final» en el texto SIN `final: true` y sigue moviéndose.
El modelo es una hipótesis de trabajo: sirve para comparar políticas, no para prometer precios.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass
from typing import Callable, Optional

import ladder_calibrated as lcal
import ladder_plus as lplus
import negotiation as neg

TEAM = "t15"


@dataclass(frozen=True)
class Spec:
    mode: str                 # "buy" (nosotros compramos: su precio baja) | "sell" (su puja sube)
    open: int
    limit: tuple              # (min, max) límite secreto por conversación («every conversation has its own»)
    patience: tuple           # (min, max) rondas hasta final:true
    beta: float = 1.0
    stall: int = 0
    mirror: bool = False
    p_counter_final: float = 0.0
    p_bluff: float = 0.0


# Ajustados a mano contra los hilos reales (ver --replay): apertura, límite y paciencia observados.
SPECS = {
    "abuela|buy|common": Spec("buy", 12, (8, 8), (4, 6), beta=0.5, p_counter_final=1.0),
    "abuela|buy|uncommon": Spec("buy", 29, (19, 21), (5, 8), beta=0.5, p_counter_final=1.0),
    "chato|buy|rare": Spec("buy", 97, (78, 82), (5, 7), beta=2.0, mirror=True, p_counter_final=1.0),
    "chato|buy|uncommon": Spec("buy", 33, (25, 26), (6, 10), beta=1.0, stall=2, mirror=True, p_counter_final=1.0),
    "abuela|sell|common": Spec("sell", 5, (6, 6), (4, 7), beta=1.0, stall=2),
    "chato|sell|uncommon": Spec("sell", 13, (15, 15), (5, 7), beta=1.0, stall=3),
    "pilar|sell|uncommon": Spec("sell", 16, (19, 22), (4, 8), beta=1.5, stall=2, p_bluff=0.3),
    "pilar|sell|uncommon|fav": Spec("sell", 22, (25, 26), (3, 7), beta=1.0, stall=1, p_bluff=0.3),
}


class SimDealer:
    """Un hilo con un vendedor; produce mensajes con el formato del servidor (lo que lee neg.state_from_thread)."""

    def __init__(self, dealer: str, spec: Spec, rng: random.Random, tick: int = 1, tid: int = 1):
        self.dealer, self.spec, self.rng = dealer, spec, rng
        self.T = rng.randint(*spec.patience)
        self.L = rng.randint(*spec.limit)
        self.k, self.tick, self._id = 0, tick, 0
        self.price = spec.open
        self.status, self.deal = "open", None
        self.queued: Optional[int] = None
        self.final_sent = False
        self.prev_ours: Optional[int] = None
        topic = {"buy": {"card": "XXX-01"}} if spec.mode == "buy" else {"sell": {"assets": [1]}}
        self.thread = {"id": tid, "kind": "persona", "with": dealer, "team": TEAM, "topic": topic, "status": "open",
                       "created_tick": tick, "messages": []}
        self._offer(spec.open)

    @property
    def sign(self) -> int:
        return -1 if self.spec.mode == "buy" else 1

    def _offer(self, price: int, final: bool = False, text: str = ""):
        for m in self.thread["messages"]:
            o = m.get("offer")
            if o and o["maker"] == self.dealer and o["status"] == "open":
                o["status"] = "expired"
        self._id += 1
        cash = {"want": {"cash": price, "assets": [], "types": []}, "give": {"cash": 0, "assets": [], "types": []}} \
            if self.spec.mode == "buy" else \
            {"give": {"cash": price, "assets": [], "types": []}, "want": {"cash": 0, "assets": [1], "types": []}}
        self.thread["messages"].append({"id": self._id, "tick": self.tick, "sender": self.dealer, "text": text,
                                        "offer": dict(id=self._id, maker=self.dealer, to=TEAM, status="open",
                                                      final=final, expires_tick=self.tick + 3, **cash)})
        self.price = price

    def _sched(self, k: int) -> float:
        s = self.spec
        span = abs(self.L - s.open)
        if k <= s.stall:
            frac = 0.0
        else:
            frac = min(1.0, ((k - s.stall) / max(1, self.T - s.stall)) ** s.beta)
        return s.open + self.sign * span * frac

    def say(self, price: int):
        self._id += 1
        self.thread["messages"].append({"id": self._id, "tick": self.tick, "sender": TEAM, "price": price})
        self.queued = price

    def accept(self, offer_id: int) -> bool:
        live = next((m["offer"] for m in self.thread["messages"] if (m.get("offer") or {}).get("id") == offer_id
                     and m["offer"]["status"] == "open"), None)
        if live is None:
            return False
        self._close("deal", live["give" if self.spec.mode == "sell" else "want"]["cash"])
        return True

    def close(self):
        self._close("closed", None)

    def _close(self, status, price):
        self.status, self.deal = status, price
        self.thread["status"] = status

    def respond(self):
        """Respuesta un tick después de nuestra oferta."""
        p, self.queued = self.queued, None
        if p is None or self.status != "open":
            return
        s, sg = self.spec, self.sign
        if self.final_sent:  # contraoferta tras su final: la acepta si respeta su límite, si no se va
            ok = (p >= self.L if s.mode == "buy" else p <= self.L) and abs(p - self.price) <= 1
            if ok and self.rng.random() < s.p_counter_final:
                self._close("deal", p)
            else:
                self._close("walked", None)
            return
        moved = p != self.prev_ours
        step = abs(p - self.prev_ours) if self.prev_ours is not None else 10 ** 6
        self.prev_ours = p
        if moved:
            self.k += 1
        target = self._sched(self.k)
        inc = max(0.0, (target - self.price) * sg)
        if s.mirror:
            inc = min(inc, step if moved else 0)
        new = self.price + sg * int(round(inc))
        new = max(new, self.L) if s.mode == "buy" else min(new, self.L)
        within = p >= self.L if s.mode == "buy" else p <= self.L
        # nuestra oferta a ≤ 1 P de su nuevo precio y dentro de su límite: la acepta
        if within and ((s.mode == "buy" and p >= new - 1) or (s.mode == "sell" and p <= new + 1)):
            self._close("deal", p)
            return
        if self.k >= self.T:
            # su final queda 1 P por encima de su límite (por eso final∓1 funcionó 3/3 con Abuela y Chato)
            if abs(self.L - s.open) > 2:
                new = max(new, self.L + 1) if s.mode == "buy" else min(new, self.L - 1)
            self.final_sent = True
            self._offer(new, final=True, text="That is my final word.")
            return
        bluff = self.k == self.T - 1 and self.rng.random() < s.p_bluff
        self._offer(new, text="My final courtesy, not a step more." if bluff else "")


Policy = Callable[[dict, int], neg.Decision]


def run(dealer: SimDealer, policy: Policy, max_ticks: int = 60) -> dict:
    side = dealer.spec.mode
    for _ in range(max_ticks):
        if dealer.status != "open":
            break
        st_thread = dealer.thread
        d = policy(st_thread, dealer.tick)
        if d.action == "accept":
            dealer.accept(d.offer_id)
            break
        if d.action == "abandon":
            dealer.close()
            break
        if d.action == "counter":
            dealer.say(d.price)
        dealer.tick += 1
        dealer.respond()
    st = neg.state_from_thread(dealer.thread, dealer.dealer, dealer.tick, neg.Config(), side=side)
    return {"status": dealer.status, "price": dealer.deal, "opening": st.opening, "rounds": st.turns}


# ------------------------------------------------------------------ políticas: antes (main) y después (calibrada)

def _rarity(key: str) -> str:
    return key.split("|")[2]


def default_policy(key: str, ceiling: int, floor: int) -> Optional[Policy]:
    """Coordinador SIN flags de escalera (decide_dealer en modo score, SECURE): solo compra; no vende a vendedores."""
    dealer, mode = key.split("|")[:2]
    if mode != "buy":
        return None
    pol = neg.policy_for(dealer, "score", 0)

    def run_pol(t, tick):
        st = neg.state_from_thread(t, dealer, tick, neg.Config())
        return neg.decide_dealer(st, pol, ceiling, pol.max_ticks - (tick - t["created_tick"]))
    return run_pol


def before_policy(key: str, ceiling: int, floor: int) -> Policy:
    """Lo que hace main con --ladder-fill --pilar-sell SAL:1.25 (escalera de #8 + ESTRATEGIA_TOP3 + ladder_plus)."""
    dealer, mode = key.split("|")[:2]
    lc = lplus.top3_ladder_config(neg.LadderConfig())
    if mode == "buy":
        prof = neg.ladder_profile(dealer, lc, "score")

        def pol(t, tick):
            st = neg.state_from_thread(t, dealer, tick, neg.Config())
            return neg.decide_ladder(st, prof, ceiling, prof.max_ticks - (tick - t["created_tick"]), _rarity(key), tick)
        return pol
    if dealer == "pilar":
        cfg = lplus.parse_pilar_sell("SAL:1.25")

        def pol(t, tick):
            st = neg.state_from_thread(t, dealer, tick, neg.Config(), side="sell")
            return lplus.decide_sell(st, floor, lplus.first_ask(floor, cfg), cfg, cfg.max_ticks - (tick - t["created_tick"]))
        return pol

    def pol(t, tick):
        st = neg.state_from_thread(t, dealer, tick, neg.Config(), side="sell")
        return neg.decide_ladder_sell(st, lc, floor, lc.sell_ticks - (tick - t["created_tick"]))
    return pol


def after_policy(key: str, ceiling: int, floor: int, profiles: Optional[dict] = None) -> Policy:
    dealer, mode, rarity = key.split("|")[:3]
    _, prof = lcal.profile_for(profiles or lcal.PROFILES, dealer, mode, rarity, key.endswith("|fav"))

    def pol(t, tick):
        st = neg.state_from_thread(t, dealer, tick, neg.Config(), side=mode)
        left = prof.max_ticks - (tick - t["created_tick"])
        return (lcal.decide_buy(st, prof, ceiling, left, tick) if mode == "buy" else
                lcal.decide_sell(st, prof, floor, left, tick))
    return pol


def scripted_policy(key: str, prices: list) -> Policy:
    """Reproduce la secuencia real de precios de un equipo; al acabarse, acepta la oferta vigente."""
    dealer, mode = key.split("|")[:2]
    seq = list(prices)

    def pol(t, tick):
        st = neg.state_from_thread(t, dealer, tick, neg.Config(), side=mode)
        if st.awaiting_reply:
            return neg.Decision("wait", "")
        live = st.live
        if live and live.final:
            return neg.Decision("accept", "", live.price, live.offer_id)
        if st.turns < len(seq):
            return neg.Decision("counter", "", seq[st.turns])
        return neg.Decision("accept", "", live.price, live.offer_id) if live else neg.Decision("abandon", "")
    return pol


def capture(key: str, r: dict, profiles: Optional[dict] = None) -> float:
    dealer, mode, rarity = key.split("|")[:3]
    _, prof = lcal.profile_for(profiles or lcal.PROFILES, dealer, mode, rarity, key.endswith("|fav"))
    return prof.capture(r["opening"], r["price"], mode) if r["status"] == "deal" else 0.0


def compare(n: int = 2000, seed: int = 7, keys=None) -> dict:
    """Captura media, tasa de trato y precio medio, antes y después, por vendedor × modo × rareza."""
    out = {}
    for key in keys or SPECS:
        spec = SPECS[key]
        ceiling = spec.open - 1          # compra: estaríamos dispuestos a pagar casi su apertura (peor caso)
        floor = 1                        # venta: copia de poco valor privado (duplicado)
        row = {}
        for name, mk in (("sin flags", default_policy), ("antes", before_policy), ("después", after_policy)):
            if mk(key, ceiling, floor) is None:
                row[name] = None
                continue
            rng = random.Random(seed)
            res = [run(SimDealer(key.split("|")[0], spec, rng), mk(key, ceiling, floor)) for _ in range(n)]
            deals = [r for r in res if r["status"] == "deal"]
            row[name] = {"captura": round(statistics.mean(capture(key, r) for r in res), 3),
                         "tratos": round(len(deals) / n, 3),
                         "precio": round(statistics.mean(r["price"] for r in deals), 1) if deals else None,
                         "rondas": round(statistics.mean(r["rounds"] for r in res), 1)}
        out[key] = row
    return out


def replay(rows: list, seed: int = 7, reps: int = 30) -> dict:
    """Valida el modelo: para cada hilo real con trato o final:true conocido, reproduce las aperturas y pasos del equipo
    y compara el precio simulado (mediana de `reps` paciencias) con el real. rows = salida de ladder_measure --rows."""
    errs = {}
    for r in rows:
        key = f"{r['dealer']}|{r['mode']}|{r['kind']}" + ("|fav" if r.get("fav") else "")
        spec = SPECS.get(key)
        real = r.get("deal") if r.get("deal") is not None else r.get("final")
        if spec is None or real is None or not r.get("team_prices") or not r.get("dealer_open"):
            continue
        spec = Spec(**{**spec.__dict__, "open": r["dealer_open"]})
        rng = random.Random(seed)
        sims = [run(SimDealer(r["dealer"], spec, rng), scripted_policy(key, r["team_prices"])) for _ in range(reps)]
        got = [x["price"] for x in sims if x["price"] is not None]
        if got:
            errs.setdefault(key, []).append(abs(statistics.median(got) - real))
    return {k: {"hilos": len(v), "error_medio_P": round(statistics.mean(v), 2)} for k, v in errs.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--replay", help="JSON de ladder_measure.py --rows (hilos reales) para validar el modelo")
    a = ap.parse_args()
    if a.replay:
        with open(a.replay, encoding="utf-8") as fh:
            for k, v in sorted(replay(json.load(fh), a.seed).items()):
                print(f"{k:28s} {v['hilos']:4d} hilos · error medio {v['error_medio_P']} P")
        return
    res = compare(a.n, a.seed)
    print("captura media del rango (0 sin trato) · precio medio del trato · sin flags = coordinador por defecto · antes ="
          " main con --ladder-fill --pilar-sell SAL:1.25 · después = --ladder-calibrated")
    print(f"{'vendedor|modo|rareza':26s} {'sin flags':>10s} {'antes':>7s} {'después':>8s}   precio sin flags/antes/después")
    for k, v in res.items():
        z, b, d = v["sin flags"], v["antes"], v["después"]
        print(f"{k:26s} {z['captura'] if z else '—':>10} {b['captura']:7.3f} {d['captura']:8.3f}   "
              f"{z['precio'] if z else '—'} / {b['precio']} / {d['precio']}")


if __name__ == "__main__":
    main()
