"""Simulador offline de duelos: rivales sintéticos frente a la política de duels.py. Sin red, sin claves.

    python3 duel_sim.py                    # tabla: captura por tipo de rival y variante de la política
    python3 duel_sim.py --n 400 --seed 7   # más oleadas / otra semilla
    python3 duel_sim.py --days             # duelos de precio + días (Duelos II)
    python3 duel_sim.py --house --days --session II   # bots de la casa con nombre (Duelos I) y --profiles/--logroll

Modelo (aproximación, no el servidor):
  * Oleadas de WAVE duelos simultáneos con el mismo deadline (Duelos II: 6) y la regla real de UNA aceptación por
    tick y equipo para nosotros; el rival de cada duelo es independiente.
  * Tarta = valor del comprador − coste del vendedor (+ mejor suma de utilidades de días si el duelo los negocia).
  * Captura = nuestra utilidad / tarta × (1 − decay)^rondas. Una ronda = ambos lados han hablado desde la anterior
    (en la práctica `rounds` siguió en 0 con un rival que hablaba solo). Sin trato, 0; fuera de límite, negativo.
  * Tipos de rival (`RIVALS`): cedente, firme, mudo, espejo, impaciente. Los parámetros son supuestos razonables
    inspirados en los 24 duelos de práctica, no mediciones del torneo.
  * `--house`: bots de la casa con alias y el comportamiento medido en Duelos I (`HOUSE`): Plata cede 1-3 P/tick,
    Verde en ciclos, Oro/Luna saltan 16-26 P y se plantan, Rojo empeora 2-5 P/tick, Noche a veces empeora, Sol
    desconocido; un 20 % de los duelos con rival mudo (cualquier alias). Los bots proponen su día preferido y aceptan
    nuestra oferta si les vale al menos lo que su oferta vigente (SUPOSICIÓN).
"""
from __future__ import annotations

import argparse
import random
from typing import Optional

import duels as dl

TICKS = 12          # duración de un duelo (en la práctica, 10–12 ticks entre el primer mensaje y el deadline)
WAVE = 6            # duelos simultáneos con el mismo deadline (Duelos II)
DECAY = 0.08


class Rival:
    """Contraparte sintética. `lim` es su límite (coste si vende, valor si compra). `good(p)` = mejor para él."""
    name = "base"
    alias = "R"

    def __init__(self, role: str, lim: float, rng: random.Random, days_w: Optional[list] = None):
        self.role, self.lim, self.rng, self.days_w = role, lim, rng, days_w
        self.offer: Optional[int] = None
        self.withdrawn = False

    def sgn(self) -> int:              # +1 si al rival le conviene un precio más alto (vende)
        return 1 if self.role == "seller" else -1

    def at(self, share: float) -> int:  # precio que le deja un margen `share` sobre su límite
        return max(1, int(round(self.lim * (1 + self.sgn() * share))))

    def surplus(self, price: float, days: Optional[int] = None) -> float:
        dv = self.days_w[days] if self.days_w is not None and days is not None else 0.0
        return self.sgn() * (price - self.lim) + dv

    def days(self) -> Optional[int]:
        return None if self.days_w is None else max(range(11), key=lambda k: self.days_w[k])

    def act(self, t: int, ours: Optional[int], ours_moved: bool) -> Optional[int]:
        """Precio que publica en el tick t (None = no habla). `ours` es nuestra última oferta."""
        raise NotImplementedError

    def accepts(self, t: int, price: int, days: Optional[int]) -> bool:
        return self.surplus(price, days) >= self.lim * 0.05


class Cedente(Rival):
    """Abre exigiendo +50 % y cede cada tick hacia +5 % sin necesitar respuesta (lo más visto en la práctica)."""
    name = "cedente"

    def act(self, t, ours, ours_moved):
        share = 0.50 - (0.45 * t / (TICKS - 1))
        self.offer = self.at(share)
        return self.offer

    def accepts(self, t, price, days):
        return self.surplus(price, days) >= self.sgn() * (self.offer - self.lim) if self.offer else super().accepts(t, price, days)


class Firme(Rival):
    """Una exigencia fija (+20…35 %) que repite cada tick; solo acepta ofertas que la igualen."""
    name = "firme"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.demand = self.at(self.rng.uniform(0.20, 0.35))

    def act(self, t, ours, ours_moved):
        self.offer = self.demand
        return self.offer

    def accepts(self, t, price, days):
        return self.sgn() * (price - self.demand) >= 0


class Mudo(Rival):
    """No habla nunca; acepta una oferta nuestra si le deja ≥ 10 % sobre su límite."""
    name = "mudo"

    def act(self, t, ours, ours_moved):
        return None

    def accepts(self, t, price, days):
        return self.surplus(price, days) >= self.lim * 0.10


class Espejo(Rival):
    """Abre en +40 % y solo se mueve cuando nos movemos: recorre la mitad de la distancia a nuestra oferta."""
    name = "espejo"

    def act(self, t, ours, ours_moved):
        if self.offer is None:
            self.offer = self.at(0.40)
        elif ours is not None and ours_moved:
            floor = self.at(0.03)
            mid = (self.offer + ours) / 2
            self.offer = int(round(max(mid, floor) if self.sgn() > 0 else min(mid, floor)))
        return self.offer

    def accepts(self, t, price, days):
        return self.offer is not None and self.sgn() * (price - self.offer) >= -0.05 * self.lim


class Impaciente(Rival):
    """Oferta buena pronto; si no le aceptamos en 3 ticks, empeora cada tick y a los 6 deja de hablar."""
    name = "impaciente"

    def act(self, t, ours, ours_moved):
        if t < 3:
            self.offer = self.at(0.30 - 0.06 * t)
        elif t < 6:
            self.offer = self.at(0.18 + 0.05 * (t - 2))
        else:
            return None                     # calla; su última oferta sigue en pie
        return self.offer


RIVALS = {c.name: c for c in (Cedente, Firme, Mudo, Espejo, Impaciente)}


class House(Rival):
    """Bot de la casa: sigue un camino de precios fijado al nacer (en primas absolutas desde su apertura) y acepta una
    oferta nuestra que le valga al menos lo que su oferta vigente."""
    name = "casa"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open = self.at(self.rng.uniform(0.35, 0.60))
        self.moves = self.path()

    def path(self) -> list:             # mejora acumulada A NUESTRO FAVOR (P) en cada tick; negativa = empeora
        raise NotImplementedError

    def act(self, t, ours, ours_moved):
        p = self.open - self.sgn() * self.moves[min(t, len(self.moves) - 1)]
        floor = self.at(0.02)
        self.offer = int(round(max(p, floor) if self.sgn() > 0 else min(p, floor)))
        return self.offer

    def accepts(self, t, price, days):
        if self.offer is None:
            return self.surplus(price, days) >= self.lim * 0.10
        return self.surplus(price, days) >= self.surplus(self.offer, self.days())


class Plata(House):
    name, alias = "plata", "Rival Plata"

    def path(self):
        c = self.rng.uniform(1, 3)
        return [c * t for t in range(TICKS)]


class Verde(House):
    name, alias = "verde", "Rival Verde"

    def path(self):
        step, length = self.rng.uniform(5, 10), self.rng.choice([3, 4])
        return [step * (t // length) for t in range(TICKS)]


class Oro(House):
    name, alias = "oro", "Rival Oro"

    def path(self):
        j, jump = self.rng.choice([1, 2, 3]), self.rng.uniform(16, 26)
        return [jump if t >= j else 0.0 for t in range(TICKS)]


class Luna(Oro):
    name, alias = "luna", "Rival Luna"


class Rojo(House):
    """Abre bien (+8…20 %) y empeora 2-5 P por tick (Duelos I: vendedor 133 → 166)."""
    name, alias = "rojo", "Rival Rojo"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open = self.at(self.rng.uniform(0.08, 0.20))

    def path(self):
        w = self.rng.uniform(2, 5)
        return [-w * t for t in range(TICKS)]


class Noche(House):
    """A veces empeora (como un Rojo suave) y a veces cede con pausas."""
    name, alias = "noche", "Rival Noche"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        if self.worse:
            self.open = self.at(self.rng.uniform(0.10, 0.25))

    def path(self):
        self.worse = self.rng.random() < 0.5
        if self.worse:
            w = self.rng.uniform(1, 3)
            return [-w * t for t in range(TICKS)]
        c = self.rng.uniform(1.5, 4)
        return [c * (t // 2) for t in range(TICKS)]


class Sol(House):
    """Perfil desconocido: cede constante o en ciclos."""
    name, alias = "sol", "Rival Sol"

    def path(self):
        return (Plata.path if self.rng.random() < 0.5 else Verde.path)(self)


class HouseMute(Mudo):
    """Bot mudo con alias de la casa (en Duelos I había rivales que no hablaron nunca)."""
    name = "mudo"

    def __init__(self, *a, alias="Rival Sol", **k):
        super().__init__(*a, **k)
        self.alias = alias


HOUSE = {c.name: c for c in (Plata, Verde, Oro, Luna, Rojo, Noche, Sol)}
SESSIONS = {"II": {"ticks": 16, "decay": 0.08, "wave": 6}, "III": {"ticks": 12, "decay": 0.10, "wave": 4}}


def _day_weight(rng: random.Random, scale: float) -> float:
    """Peso POSITIVO por día de entrega, como lo da el servidor (`your_days_weight`); el signo lo pone el rol."""
    return round(rng.uniform(0.0, 0.05) * scale, 2)


def _signed(role: str, w: float) -> list:
    """Utilidad por día desde el lado de `role`: el comprador PAGA cada día (−w·d), el vendedor COBRA (+w·d)."""
    return [round((-w if role == "buyer" else w) * d, 2) for d in range(11)]


def play_wave(kinds: list, rng: random.Random, params: Optional[dict] = None, days: bool = False,
              house: bool = False, collect: Optional[list] = None, base_duel: int = 0) -> list:
    """Juega una oleada de duelos simultáneos (uno por tipo de `kinds`). Devuelve [{kind, capture, deal, outside}]."""
    saved = dict(dl.PARAMS)
    dl.PARAMS.update(params or {})
    try:
        duels, rivals, results = [], [], []
        for i, kind in enumerate(kinds):
            role = "buyer" if rng.random() < 0.5 else "seller"
            cost = rng.uniform(30, 150)
            value = cost * rng.uniform(1.25, 2.0)
            ours, theirs = (value, cost) if role == "buyer" else (cost, value)
            rrole = "seller" if role == "buyer" else "buyer"
            wo = _day_weight(rng, value - cost) if days else None            # nuestro peso, formato del servidor
            wr = _day_weight(rng, value - cost) if days else None
            wd = _signed(role, wo) if days else None                       # utilidades firmadas (solo para el modelo)
            rw = _signed(rrole, wr) if days else None
            if kind == "mudo" and house:
                rivals.append(HouseMute(rrole, theirs, rng, rw, alias=rng.choice([c.alias for c in HOUSE.values()])))
            else:
                rivals.append((HOUSE if house else RIVALS)[kind](rrole, theirs, rng, rw))
            joint = max(wd[k] + rw[k] for k in range(11)) if days else 0.0
            duels.append({"duel": i, "status": "live", "role": role, "your_limit": int(round(ours)), "rival": rivals[-1].alias,
                          "deadline_tick": TICKS, "decay_per_round": DECAY, "issues": ["price", "days"] if days else ["price"],
                          "your_days_weight": wo, "days_meaning": (None if not days else
                          "each delivery day costs you this much cash" if role == "buyer" else
                          "each delivery day adds this much cash to your side"), "rival_offer": None, "your_offer": None, "messages": [],
                          "rounds": 0, "_pie": value - cost + joint, "_spoke": [False, False], "_last_ours": None})
        done = {}
        for t in range(TICKS):
            # 1) rivales hablan (al principio del tick)
            for d, r in zip(duels, rivals):
                if d["duel"] in done:
                    continue
                ours = (d["your_offer"] or {}).get("price")
                moved = ours is not None and ours != d["_last_ours"]
                d["_last_ours"] = ours
                p = r.act(t, ours, moved)
                if p is not None:
                    rd = r.days()
                    d["rival_offer"] = {"id": len(d["messages"]), "price": p, "days": rd, "tick": t}
                    d["messages"].append({"tick": t, "from": r.alias, "price": p, "days": rd})
                    _spoke(d, 1)
            # 2) nuestra política: una aceptación por tick como mucho
            live = [d for d in duels if d["duel"] not in done]
            accepted = False
            for c in dl.duel_candidates(live, t):
                d = duels[c["duel"]]
                if c["type"] == "duel_accept":
                    if accepted:
                        continue
                    accepted = True
                    ro = d["rival_offer"]
                    done[d["duel"]] = (ro["price"], ro.get("days"))
                else:
                    if days:
                        assert c.get("days") is not None and 0 <= c["days"] <= 10, c   # nunca missing_days
                    d["your_offer"] = {"price": c["price"], "days": c.get("days")}
                    d["messages"].append({"tick": t, "from": "us", "price": c["price"], "days": c.get("days")})
                    _spoke(d, 0)
            # 3) rivales consideran nuestra oferta
            for d, r in zip(duels, rivals):
                yo = d["your_offer"]
                if d["duel"] not in done and yo and r.accepts(t, yo["price"], yo.get("days")):
                    done[d["duel"]] = (yo["price"], yo.get("days"))
        for d, r in zip(duels, rivals):
            deal = d["duel"] in done
            u = dl.margin(d, *done[d["duel"]]) if deal else 0.0
            cap = (u / d["_pie"]) * (1 - DECAY) ** d["rounds"] if deal and d["_pie"] > 0 else 0.0
            results.append({"kind": r.name, "capture": cap, "deal": deal, "outside": deal and u < 0})
            if collect is not None:      # historial con la forma del servidor (done=true) para el laboratorio de aprendizaje
                px, dx = done[d["duel"]] if deal else (None, None)
                collect.append({"duel": base_duel + d["duel"], "session": 3, "status": "deal" if deal else "no_deal",
                                "role": d["role"], "item": "sim", "issues": d["issues"], "your_days_weight": d["your_days_weight"],
                                "days_meaning": d.get("days_meaning"), "your_limit": d["your_limit"], "rival": "Rival " + r.name.capitalize(),
                                "deadline_tick": d["deadline_tick"] + base_duel * 0, "decay_per_round": DECAY, "rounds": d["rounds"],
                                "result": round(u * (1 - DECAY) ** d["rounds"], 2) if deal else 0.0, "price": px, "days": dx,
                                "messages": [dict(m, **{"from": ("you" if m["from"] == "us" else m["from"])}) for m in d["messages"]]})
        return results
    finally:
        dl.PARAMS.clear()
        dl.PARAMS.update(saved)


def _spoke(d: dict, side: int) -> None:
    d["_spoke"][side] = True
    if all(d["_spoke"]):
        d["rounds"] += 1
        d["_spoke"] = [False, False]


VARIANTS = {
    "por defecto": {},
    "escalera (--ladder)": {"LADDER": (0.20, 0.12, 0.06)},
    "sondeo (--probe)": {"PROBE": True},
    "escalera + sondeo": {"LADDER": (0.20, 0.12, 0.06), "PROBE": True},
    "aceptar ≥30 % margen": {"GOOD_SHARE": 0.30},
}


HOUSE_VARIANTS = {
    "por defecto": {},
    "--ladder": {"LADDER": (0.20, 0.12, 0.06)},
    "--profiles": {"PROFILES": True},
    "--ladder --profiles": {"LADDER": (0.20, 0.12, 0.06), "PROFILES": True},
    "--profiles --logroll": {"PROFILES": True, "LOGROLL": True},
    "--ladder --profiles --logroll": {"LADDER": (0.20, 0.12, 0.06), "PROFILES": True, "LOGROLL": True},
}


def simulate(n: int = 200, seed: int = 1, days: bool = False, variants: Optional[dict] = None,
             mix: Optional[list] = None, house: bool = False, mute_share: float = 0.2) -> dict:
    """{variante: {tipo: {capture, deals, outside, n}}} con oleadas de WAVE duelos de tipos aleatorios.
    Misma semilla por variante: todas juegan exactamente los mismos duelos. `house`: bots de la casa con alias
    (`HOUSE`) más un `mute_share` de mudos."""
    base = HOUSE_VARIANTS if house else VARIANTS
    variants = variants or (dict(base, **{"sin --days (actual)": {"PLAY_DAYS": False}}) if days else base)
    mix = mix or (list(HOUSE) + ["mudo"] if house else list(RIVALS))
    out = {}
    for vname, params in variants.items():
        rng = random.Random(seed)
        agg = {k: {"capture": 0.0, "deals": 0, "outside": 0, "n": 0} for k in mix + ["TOTAL"]}
        for _ in range(n):
            if house:
                kinds = ["mudo" if rng.random() < mute_share else rng.choice([k for k in mix if k != "mudo"])
                         for _ in range(WAVE)]
            else:
                kinds = [rng.choice(mix) for _ in range(WAVE)]
            p = {"PLAY_DAYS": True, **params} if days else params
            for r in play_wave(kinds, random.Random(rng.random()), p, days, house):
                for k in (r["kind"], "TOTAL"):
                    a = agg[k]
                    a["n"] += 1
                    a["capture"] += r["capture"]
                    a["deals"] += r["deal"]
                    a["outside"] += r["outside"]
        out[vname] = {k: {"capture": v["capture"] / v["n"] if v["n"] else 0.0, "deals": v["deals"] / v["n"] if v["n"] else 0.0,
                          "outside": v["outside"], "n": v["n"]} for k, v in agg.items()}
    return out


def table(res: dict) -> str:
    kinds = list(next(iter(res.values())))
    lines = ["| Variante | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
    for v, row in res.items():
        cells = [f"{row[k]['capture'] * 100:.0f} % ({row[k]['deals'] * 100:.0f} % trato{', ' + str(row[k]['outside']) + ' fuera' if row[k]['outside'] else ''})"
                 for k in kinds]
        lines.append(f"| {v} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=200, help="oleadas de %d duelos" % WAVE)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--days", action="store_true", help="duelos de precio + días")
    ap.add_argument("--house", action="store_true", help="bots de la casa con alias (perfiles de Duelos I)")
    ap.add_argument("--session", choices=sorted(SESSIONS), help="ticks, decay y simultáneos de Duelos II o III")
    a = ap.parse_args()
    if a.session:
        configure(a.session)
    print(f"captura media de la tarta (y % de duelos con trato) · {a.n} oleadas × {WAVE} duelos · "
          f"{TICKS} ticks · decay {DECAY} · {'precio + días' if a.days else 'solo precio'} · "
          f"{'bots de la casa' if a.house else 'rivales genéricos'} · semilla {a.seed}")
    print(table(simulate(a.n, a.seed, a.days, house=a.house)))


def configure(session: str) -> None:
    """Fija TICKS, DECAY y WAVE de la sesión (II: 16 ticks, 0,08, 6 a la vez; III: 12 ticks, 0,10, 4 a la vez)."""
    global TICKS, DECAY, WAVE
    cfg = SESSIONS[session]
    TICKS, DECAY, WAVE = cfg["ticks"], cfg["decay"], cfg["wave"]


if __name__ == "__main__":
    main()
