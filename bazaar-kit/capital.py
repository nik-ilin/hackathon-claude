"""Asignación de capital: reserva dura, liquidez para vendedores y capital de mercado; rebalanceo mediante CANCELACIONES.

    efectivo = reserva dura + liquidez de vendedores (objetivo) + capital de mercado (+ lo ya comprometido)

- RESERVA DURA: intocable (`--reserve`).
- LIQUIDEZ DE VENDEDORES: lo que debe quedar libre para cerrar una negociación activa con un vendedor. Objetivo
  dinámico = el mayor máximo económico entre las negociaciones activas (se acepta como mucho una oferta por tick, así
  que basta con poder pagar la mayor). Sin negociaciones activas: `--dealer-liquidity` (configurable, 0 = nada).
- CAPITAL DE MERCADO: lo único que pueden inmovilizar las pujas pasivas.

Las pujas abiertas son obligaciones REALES (varias pueden llenarse a la vez): nunca se descuentan por su baja
probabilidad de ejecución. Si una oportunidad superior necesita efectivo, se CANCELAN las pujas más débiles, se espera
a que el servidor lo confirme y después se ejecuta. Lógica pura, sin red.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import market_intel as mi


@dataclass
class CapitalConfig:
    hard_reserve: int = 100
    dealer_idle_liquidity: int = 40     # objetivo sin negociaciones activas (una oportunidad de vendedor probable)
    min_offer_age_ticks: int = 3        # no se toca una puja más joven (sin churn)
    min_cancel_gain: float = 2.0        # la oportunidad debe superar el valor esperado de lo cancelado en al menos esto
    capital_rebalance_threshold: int = 20  # con menos capital de mercado libre, se retiran las pujas obsoletas
    stale_age_ticks: int = 30           # edad a partir de la cual una puja sin oferta enfrente es obsoleta
    stale_p_fill: float = 0.05          # ... y su probabilidad de ejecución sigue por debajo de esto
    tactical_cash_buffer: int = 30      # colchón para oportunidades inmediatas/dirigidas de alto valor
    max_passive_cash_fraction: float = 0.5  # pujas pasivas: como mucho esta fracción de (efectivo − reserva dura)
    dealer_cash_buffer_mode: str = "active_max"  # active_max | fixed | off
    min_excess_to_act: int = 5          # histéresis: no se cancela por exceso de capital pasivo menor que esto


@dataclass
class CapitalView:
    cash: int
    hard_reserve: int
    dealer_liquidity_target: int
    market_reserved_cash: int
    dealer_exposure: int
    pending_cash: int
    free_market_cash: int
    free_dealer_cash: int
    budget_left: int
    tactical_buffer: int = 0
    passive_limit: int = 0
    excess_locked: int = 0
    free_tactical_cash: int = 0

    def line(self) -> str:
        return (f"CAPITAL efectivo {self.cash} P · reserva dura {self.hard_reserve} P · liquidez vendedores (objetivo) "
                f"{self.dealer_liquidity_target} P · colchón táctico {self.tactical_buffer} P · pujas de mercado "
                f"{self.market_reserved_cash} P (límite pasivo {self.passive_limit} P, exceso {self.excess_locked} P) · "
                f"expuesto con vendedores {self.dealer_exposure} P · pendiente {self.pending_cash} P · libre mercado "
                f"{self.free_market_cash} P · libre táctico {self.free_tactical_cash} P · libre vendedores "
                f"{self.free_dealer_cash} P · presupuesto {self.budget_left} P")

    def block(self, cancels: list = ()) -> str:
        rows = ["=== CAPITAL ===", f"Cash: {self.cash}", f"Hard reserve: {self.hard_reserve}",
                f"Dealer liquidity: {self.dealer_liquidity_target}", f"Tactical buffer: {self.tactical_buffer}",
                f"Dealer exposure: {self.dealer_exposure}", f"Passive bids reserved: {self.market_reserved_cash}",
                f"Passive limit: {self.passive_limit}", f"Excessively locked: {self.excess_locked}",
                f"Free market / tactical / dealer: {self.free_market_cash} / {self.free_tactical_cash} / "
                f"{self.free_dealer_cash}"]
        if cancels:
            rows.append("Recommended cancellations:")
            for c in cancels:
                rows.append(f"  #{c['offer']} {c.get('ref')} {c.get('kind')} {c.get('price')} P · {c.get('reason', '')[:120]}")
            freed = sum(int(c.get("price") or 0) for c in cancels)
            rows.append(f"Capital after cancellations (cuando el servidor las confirme): "
                        f"{max(0, self.free_dealer_cash + freed)} P libres para vendedores/tácticas")
        else:
            rows.append("Recommended cancellations: none")
        return "\n".join(rows)

    def as_dict(self) -> dict:
        return asdict(self)


def dealer_liquidity_target(active_ceilings: list, idle: int) -> int:
    """Mayor máximo económico entre las negociaciones activas (una aceptación por tick); sin ninguna, `idle`."""
    vals = [int(c) for c in active_ceilings if c and c > 0]
    return max(vals) if vals else max(0, int(idle))


def capital_view(cash: int, hard_reserve: int, market_reserved: int, dealer_exposure: int, pending: int,
                 target: int, budget_left: int, tactical: int = 0, passive_frac: float = 1.0) -> CapitalView:
    """Cuatro compartimentos sobre el efectivo real (las pujas abiertas cuentan ENTERAS, nunca × P(ejecución)):
    reserva dura | liquidez de vendedores | colchón táctico | capital pasivo de mercado.
    - free_dealer: lo que un vendedor puede cobrar ya (sin reserva dura ni lo inmovilizado en pujas).
    - free_tactical: lo que una oportunidad inmediata puede usar (además deja intacta la liquidez de vendedores).
    - passive_limit: máximo que pueden inmovilizar las pujas = min(invertible − vendedores − táctico,
      fracción × invertible). free_market = passive_limit − ya inmovilizado; excess_locked = lo que sobra."""
    investable = cash - hard_reserve - pending
    need = max(0, target - dealer_exposure)
    passive_limit = max(0, min(investable - dealer_exposure - need - tactical, int(passive_frac * max(0, investable))))
    free_dealer = max(0, min(investable - market_reserved - dealer_exposure, budget_left))
    free_tactical = max(0, min(investable - market_reserved - dealer_exposure - need, budget_left))
    free_market = max(0, min(passive_limit - market_reserved, budget_left))
    return CapitalView(cash, hard_reserve, target, market_reserved, dealer_exposure, pending, free_market, free_dealer,
                       budget_left, tactical, passive_limit, max(0, market_reserved - passive_limit), free_tactical)


def excess_cancels(scored: list, view: CapitalView, cfg: CapitalConfig, exclude: set = frozenset()) -> list:
    """Las pujas pasivas no pueden inmovilizar más que `passive_limit`: se retiran las PEORES (no las más nuevas)
    hasta volver al límite. Histéresis `min_excess_to_act` y edad mínima para no hacer churn."""
    if view.excess_locked < cfg.min_excess_to_act:
        return []
    out, freed = [], 0
    for b in scored:
        if freed >= view.excess_locked:
            break
        if b["offer"] in exclude or b["age"] < cfg.min_offer_age_ticks:
            continue
        out.append(cancel_candidate(b, f"EXCESO DE CAPITAL PASIVO: pujas {view.market_reserved_cash} P > límite "
                                       f"{view.passive_limit} P; se retira la puja menos valiosa", score=3 * 10 ** 5,
                                    kind="liberar capital pasivo"))
        freed += b["price"]
    return out


# ------------------------------------------------------------------ calidad de las pujas abiertas

def score_open_bids(snap: dict, states: dict, icfg: mi.IntelConfig, actions: list) -> list:
    """Cada puja pasiva propia (no las de conversaciones) evaluada con el estado ACTUAL: efectivo inmovilizado,
    ΔU esperado = P(ejecución) × (ganancia − precio), confianza, edad, vida restante, si seguimos siendo la mejor
    puja, si completa página. Se ordenan de MENOS a MÁS valiosa (las primeras son las que se cancelan antes)."""
    team, tick = snap["me"]["id"], snap["clock"]["tick"]
    venues = mi.venues_from(snap)
    out = []
    for o in (snap.get("offers") or {}).get("offers", []):
        if o.get("maker") != team or o.get("status") != "open" or o.get("thread"):
            continue
        c = mi.classify(o)
        if not c or c[0] != "bid":
            continue
        _, ref, price, _, _ = c
        st, v = states.get(ref), venues.get(o.get("venue"))
        age = tick - int(o.get("created_tick") or tick)
        rec = {"offer": o["id"], "ref": ref, "venue": o.get("venue"), "price": price, "age": age,
               "ticks_left": (o["expires_tick"] - tick) if o.get("expires_tick") is not None else None,
               "p_fill": 0.0, "sample_count": 0, "confidence": "HEURISTIC", "expected_du": 0.0, "efficiency": 0.0,
               "gain": None, "still_best": None, "completes_page": False, "acquired": False, "supply": 0.0}
        if st is None or v is None:
            rec["why_unknown"] = "carta o venue sin estado"
            out.append(rec)
            continue
        p, basis = mi.fill_probability("bid", price, st, v, icfg, actions)
        n, label = mi.fill_stats(actions, v.id, "bid")
        rivals = [q.price for q in st.bids if q.offer != o["id"]]
        surplus = st.gain_if_bought - price
        best_ask = st.best_ask.price if st.best_ask else None
        rec.update(p_fill=p, sample_count=n, confidence=label, gain=st.gain_if_bought, supply=st.supply,
                   expected_du=round(p * surplus, 2), efficiency=round(p * surplus / max(1, price), 4),
                   still_best=not rivals or price > max(rivals), completes_page=st.completes_page,
                   acquired=st.copies > 0, surplus=round(surplus, 2), created_tick=o.get("created_tick"),
                   expires_tick=o.get("expires_tick"), locked_cash=price, our_value=st.gain_if_bought,
                   reservation_price=math.floor(min(st.gain_if_bought - icfg.margin, icfg.per_card)),
                   best_competing_bid=max(rivals) if rivals else None, best_ask=best_ask,
                   market_position="MEJOR PUJA" if not rivals or price > max(rivals) else "SUPERADA",
                   strategic_priority=mi.strategic_value(st), page_completion=st.completes_page)
        out.append(rec)
    return sorted(out, key=lambda r: (r["completes_page"], r["efficiency"], r["expected_du"], -r["age"]))


def stale_bid_cancels(scored: list, view: CapitalView, cfg: CapitalConfig, margin: float) -> list:
    """Pujas que ya no tienen sentido. Las razones DURAS (ya tenemos la carta, ya no compensa) se aplican siempre;
    las BLANDAS (vieja, sin oferta enfrente, P(ejecución) mínima) solo si el capital de mercado escasea."""
    out = []
    scarce = view.free_market_cash < cfg.capital_rebalance_threshold
    top = {}  # ref -> la puja propia más alta (si varias se llenaran, compraríamos duplicados con efectivo bloqueado)
    for b in scored:
        if b["ref"] not in top or (b["price"], b["offer"]) > (top[b["ref"]]["price"], top[b["ref"]]["offer"]):
            top[b["ref"]] = b
    for b in scored:
        why = None
        if top[b["ref"]] is not b:
            why = (f"puja duplicada: ya pujamos {top[b['ref']]['price']} P por {b['ref']} (oferta "
                   f"{top[b['ref']]['offer']}); dos pujas por la misma carta son dos obligaciones para una necesidad")
            out.append(cancel_candidate(b, why, score=10 ** 5, kind="retirar puja duplicada"))
            continue
        if b["age"] < cfg.min_offer_age_ticks:
            continue
        if b["acquired"]:
            why = f"ya tenemos {b['ref']}: la puja compraría un duplicado"
        elif b.get("surplus") is not None and b["surplus"] < margin:
            why = f"ya no compensa: ganancia {b['gain']} P − precio {b['price']} P < margen {margin} P"
        elif scarce and b["age"] >= cfg.stale_age_ticks and b["p_fill"] < cfg.stale_p_fill and b["supply"] <= 0:
            why = (f"obsoleta: {b['age']} ticks sin oferta enfrente, P(ejecución) {b['p_fill']:.1%} "
                   f"({b['confidence']}, n={b['sample_count']}) y capital de mercado escaso")
        if why:
            out.append(cancel_candidate(b, why, score=50.0, kind="retirar puja obsoleta"))
    return out


def rebalance(scored: list, need: int, opportunity_value: float, cfg: CapitalConfig, label: str,
              exclude: set = frozenset()) -> tuple[list, int, str]:
    """Cancela las pujas más débiles hasta liberar `need` P para una oportunidad SUPERIOR. Solo si la oportunidad
    vale más que el ΔU esperado de lo cancelado + `min_cancel_gain`. Devuelve (cancelaciones, liberado, motivo)."""
    if need <= 0:
        return [], 0, "no hace falta liberar capital"
    chosen, released, lost = [], 0, 0.0
    for b in scored:
        if b["offer"] in exclude or b["age"] < cfg.min_offer_age_ticks:
            continue
        chosen.append(b)
        released += b["price"]
        lost += max(0.0, b["expected_du"])
        if released >= need:
            break
    if released < need:
        return [], 0, f"no hay pujas cancelables suficientes ({released} P de {need} P)"
    if opportunity_value < lost + cfg.min_cancel_gain:
        return [], 0, (f"no compensa: {label} vale {opportunity_value:.1f} P y las pujas a cancelar esperan "
                       f"{lost:.1f} P (+{cfg.min_cancel_gain} P mínimo)")
    why = (f"REBALANCEO: libera {released} P de {need} P necesarios para {label} (vale {opportunity_value:.1f} P; "
           f"las pujas canceladas esperaban {lost:.1f} P)")
    return [cancel_candidate(b, why, score=5 * 10 ** 5, kind="liberar capital") for b in chosen], released, why


def cancel_candidate(b: dict, why: str, score: float, kind: str) -> dict:
    return {"type": "cancel", "kind": kind, "venue": b["venue"], "offer": b["offer"], "ref": b["ref"],
            "price": b["price"], "du": 0.0, "score": score, "blockers": [], "reason": why,
            "notes": [f"ΔU esperado de la puja {b['expected_du']} P · eficiencia {b['efficiency']} · P {b['p_fill']} "
                      f"({b['confidence']}, n={b['sample_count']}) · edad {b['age']} ticks"],
            "capital_cancel": True, "uncertainty": ""}


def releasable(scored: list, cfg: CapitalConfig) -> int:
    return sum(b["price"] for b in scored if b["age"] >= cfg.min_offer_age_ticks)


def cost_of(c: dict) -> int:
    return max(0, -int(c.get("cash") or 0))


def capacity_only(c: dict) -> bool:
    """La candidata solo está bloqueada por falta de capital (no por valor, seguridad ni límites)."""
    bl = c.get("blockers") or []
    return bool(bl) and all("capacidad" in b for b in bl)
