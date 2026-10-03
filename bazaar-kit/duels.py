"""Duelos: política de «silencio estratégico» con aceptación escalonada. Lógica pura, sin red.

Qué puntúa (RULES.md): en cada duelo, la parte de la tarta capturada (tarta = valor del comprador − coste del
vendedor). Cerrar fuera del propio límite resta, no cerrar da 0 y la tarta encoge un `decay_per_round` (6–10 %) con
cada ronda de conversación.

Evidencia (sesión de práctica del viernes, 24 duelos propios, `duels_fixture_practice.json`):
  * la mayoría de rivales CEDE CADA TICK SIN NECESITAR RESPUESTA (p. ej. 122 → 82 en 12 ticks, con nuestro límite 158);
  * `rounds` quedó en 0 aunque el rival enviara 12 mensajes: callar no encoge la tarta;
  * dos rivales hicieron una primera oferta «explosiva» que después empeoró;
  * ~25 % de los rivales no habló nunca.

Política por duelo vivo, en cada tick (hotfix: se optimiza el excedente ESPERADO de un trato cerrado, no la máxima
concesión posible). Todo se mide sobre el tick actual:
  * excedente = límite − precio (comprador) o precio − límite (vendedor); ratio = excedente / límite (diagnóstico);
  * tendencia del rival (ajustada al rol): STRONG/WEAK_IMPROVEMENT, STALLED (sin mejorar STALL_TICKS ticks),
    WORSENING o UNKNOWN; ganancia esperada de esperar = su mejora reciente por tick; riesgo de esperar = excedente ×
    (RISK_BASE + RISK_URGENCY / ticks útiles restantes): crece hacia el deadline.
  * EARLY (> 8 ticks): esperar si mejora fuerte o es su primera oferta; aceptar solo lo excepcional (≥ 30 %) si ya no
    mejora; estancado ⇒ una contraoferta.
  * MID (4-8): esperar solo si mejora fuerte y compensa; aceptar con ≥ 15 %; estancado ⇒ contraoferta y después
    cerrar un trato razonable (≥ 5 %).
  * LATE (≤ 3) y cierre seguro (SAFE_TICKS efectivos) ⇒ aceptar cualquier excedente positivo.
  * Fuera de límite: nunca se acepta; a falta de SPEAK_AT ticks, UNA oferta propia (ancla, sin revelar el límite).
  Solo hay UNA aceptación por tick y equipo: los duelos que comparten deadline se escalonan.
Duelos de precio + días: omitidos por defecto hasta verificar la fórmula del servidor. Con PARAMS["PLAY_DAYS"] = True
(opt-in, `duel_runner.py --days`) se juegan con la misma política, valorando cada oferta como precio + utilidad de
días, exigiendo SIEMPRE precio dentro de límite y enviando `days` en toda oferta propia.
Opt-in PARAMS["LADDER"]: frente a un rival que no habla, en vez de una sola oferta, una escalera de ofertas que se
acerca al límite (por defecto desactivada: una sola oferta). PROBE queda absorbido: la contraoferta a un rival
estancado es ahora parte de la política por defecto.

Repetición individual retrospectiva (sin límite compartido de aceptaciones ni validación fuera de muestra): 98 % del margen máximo disponible (562 de 574 P) con los parámetros por defecto, frente
a 77 % aceptando en cuanto hay un 30 % de margen. El viernes, sin módulo de duelos, se capturó 0.
"""
from __future__ import annotations

from typing import Optional

PARAMS = {
    "STALL_TICKS": 3,     # rival sin mejorar su precio tantos TICKS (no mensajes) ⇒ estancado
    "SPEAK_AT": 5,        # ticks antes del deadline para hablar si el rival calla o está fuera de límite
    "ANCHOR": 0.30,       # nuestra única oferta de apertura: comprador límite·(1−0,30), vendedor límite·(1+0,30)
    "SAFE_TICKS": 1,      # cierre seguro: aceptar a falta de SAFE_TICKS (+1 por cada otro duelo de la misma oleada)
    "EARLY_TICKS": 8,     # ticks_left > 8 ⇒ fase EARLY
    "LATE_TICKS": 3,      # ticks_left ≤ 3 ⇒ fase LATE (entre ambos: MID)
    "EARLY_ACCEPT_RATIO": 0.30,  # excedente/límite para aceptar en EARLY (si el rival ya no mejora fuerte)
    "MID_ACCEPT_RATIO": 0.15,    # ... en MID
    "LATE_ACCEPT_RATIO": 0.0,    # ... en LATE: cualquier excedente positivo
    "STALLED_ACCEPT_RATIO": 0.05,  # rival estancado en MID tras nuestra contraoferta: aceptar si ≥ esto
    "TREND_WINDOW": 4,    # ticks para medir la mejora reciente del rival
    "STRONG_RATE": 2.0,   # mejora ≥ max(2 P/tick, 1,5 % del límite) ⇒ STRONG_IMPROVEMENT
    "STRONG_FRAC": 0.015,
    "RISK_BASE": 0.02,    # riesgo por tick de esperar, como fracción del excedente actual...
    "RISK_URGENCY": 0.15,  # ... + RISK_URGENCY / ticks útiles restantes (crece hacia el deadline)
    "COUNTER_FRAC": 0.35,  # contraoferta = su precio movido un 35 % del excedente a nuestro favor (no revela el límite)
    # --- opt-in (desactivados por defecto; ver duel_sim.py para su medición) ---
    "PLAY_DAYS": False,   # jugar duelos de precio + días (si no, se omiten)
    "LADDER": (),         # rival mudo: márgenes sucesivos tras ANCHOR, p. ej. (0.20, 0.12, 0.06); () = una sola oferta
    "PROBE": False,       # obsoleto: la contraoferta a un rival estancado ya es política por defecto (sin efecto)
}
DEPRECATED = {"GOOD_SHARE": "sustituido por EARLY/MID/LATE_ACCEPT_RATIO: comparaba el margen con el 60 % del LÍMITE"}


_SCALAR_KEYS = ("per_day", "weight", "value", "slope", "w")


def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if x == x and abs(x) != float("inf") else None
    if isinstance(x, str):
        try:
            return _num(float(x.strip()))
        except ValueError:
            return None
    return None


def days_table(duel: dict) -> Optional[list]:
    """`your_days_weight` normalizado a una lista de 11 utilidades (día 0..10), o None si falta o no se entiende.
    El formato no está documentado: se aceptan lista por día, dict por día ({"3": 12} o {3: 12}), peso escalar por
    día (lineal: peso·días), un dict con un único escalar ({"per_day": 2}) y números en texto."""
    w = duel.get("your_days_weight")
    if w is None or isinstance(w, bool):
        return None
    if isinstance(w, (list, tuple)):
        vals = [_num(x) for x in w]
        if not vals or any(v is None for v in vals):
            return None
        return [vals[k] if k < len(vals) else 0.0 for k in range(11)]
    if isinstance(w, dict):
        for k in _SCALAR_KEYS:
            if k in w and _num(w[k]) is not None and len(w) == 1:
                return [_num(w[k]) * d for d in range(11)]
        table = {}
        for k, v in w.items():
            kk, vv = _num(k), _num(v)
            if kk is None or vv is None or kk != int(kk) or not 0 <= kk <= 10:
                return None
            table[int(kk)] = vv
        return [table.get(d, 0.0) for d in range(11)] if table else None
    n = _num(w)
    return None if n is None else [n * d for d in range(11)]


def _days_value(duel: dict, days: Optional[int]) -> float:
    """Utilidad (en primas) del día de entrega, si el duelo negocia días (0 si no hay tabla o día fuera de 0..10)."""
    if days is None:
        return 0.0
    t = days_table(duel)
    d = _num(days)
    if t is None or d is None or d != int(d) or not 0 <= d <= 10:
        return 0.0
    return t[int(d)]


def margin(duel: dict, price: Optional[int], days: Optional[int] = None) -> Optional[float]:
    """Nuestro excedente en primas si cerramos a `price` (negativo = fuera de límite). None si no hay precio."""
    if price is None:
        return None
    lim = float(duel["your_limit"])
    m = (lim - price) if duel["role"] == "buyer" else (price - lim)
    if m < 0:
        return m                      # los días nunca excusan un precio fuera de límite
    return m + _days_value(duel, days)


def rival_trajectory(duel: dict) -> list:
    """[(tick, precio)] de los mensajes con precio del rival, del más antiguo al más reciente."""
    rival = duel.get("rival")
    return [(m["tick"], m["price"]) for m in duel.get("messages", [])
            if m.get("price") is not None and m.get("from") == rival]


def _best_days(duel: dict) -> int:
    """Nuestro día preferido; en empate (o sin tabla), el último día que propuso el rival: si a nosotros nos da
    igual, se lo concedemos (la tarta crece cuando cada uno se queda con lo que más valora)."""
    ro = duel.get("rival_offer") or {}
    rd = _num(ro.get("days"))
    pref = int(rd) if rd is not None and rd == int(rd) and 0 <= rd <= 10 else 0
    return max(range(11), key=lambda k: (_days_value(duel, k), k == pref, -k))


def _is_days(duel: dict) -> bool:
    return "days" in (duel.get("issues") or [])


def _own_price(duel: dict, share: float) -> int:
    lim = float(duel["your_limit"])
    return max(1, int(round(lim * (1 - share)))) if duel["role"] == "buyer" else max(1, int(round(lim * (1 + share))))


def price_at(traj: list, t: int, current: Optional[int] = None) -> Optional[int]:
    """Precio vigente del rival en el tick `t` (función escalón de sus mensajes con precio)."""
    p = None
    for tk, pr in traj:
        if tk <= t:
            p = pr
    return current if p is None and current is not None and not traj else p


def analyze(d: dict, tick: int, same_deadline: int = 1) -> dict:
    """Hechos de la decisión, medidos sobre el TICK ACTUAL (no sobre el último mensaje): excedente, fase, tendencia,
    estancamiento, ganancia esperada de esperar frente a su riesgo, y parámetros configurados frente a efectivos."""
    ro = d.get("rival_offer") or {}
    price, days = ro.get("price"), ro.get("days")
    lim = float(d["your_limit"])
    left = d["deadline_tick"] - tick
    m = margin(d, price, days)
    traj = rival_trajectory(d)
    if price is not None and (not traj or traj[-1][1] != price):
        traj = traj + [(tick, price)]  # la oferta en pie manda aunque no venga en un mensaje
    sign = 1 if d["role"] == "buyer" else -1          # comprador: que baje es mejora; vendedor: que suba
    w = PARAMS["TREND_WINDOW"]
    now_p = price_at(traj, tick, price)
    then_p = price_at(traj, tick - w, None)
    if then_p is None and traj:
        then_p, span = traj[0][1], max(1, tick - traj[0][0])
    else:
        span = w
    rate = round(sign * (then_p - now_p) / span, 3) if then_p is not None and now_p is not None else None
    last_change = traj[-1][0] if traj else None
    for k in range(len(traj) - 1, 0, -1):         # inicio del último tramo con el precio actual
        if traj[k - 1][1] != traj[k][1]:
            last_change = traj[k][0]
            break
    else:
        last_change = traj[0][0] if traj else None
    since = tick - last_change if last_change is not None else 0
    stalled = len(traj) >= 1 and since >= PARAMS["STALL_TICKS"]
    strong = max(PARAMS["STRONG_RATE"], PARAMS["STRONG_FRAC"] * abs(lim))
    if rate is None or len(traj) < 2 and not stalled:
        trend = "UNKNOWN"
    elif rate < 0:
        trend = "WORSENING"
    elif stalled or rate == 0:
        trend = "STALLED"
    elif rate >= strong:
        trend = "STRONG_IMPROVEMENT"
    else:
        trend = "WEAK_IMPROVEMENT"
    safe_eff = PARAMS["SAFE_TICKS"] + max(0, same_deadline - 1)
    phase = "LATE" if left <= PARAMS["LATE_TICKS"] else "EARLY" if left > PARAMS["EARLY_TICKS"] else "MID"
    surplus = m if m is not None else None
    ratio = round(surplus / max(abs(lim), 1.0), 4) if surplus is not None else None
    useful = max(1, left - safe_eff)
    risk = round((surplus or 0) * (PARAMS["RISK_BASE"] + PARAMS["RISK_URGENCY"] / useful), 2) if surplus and surplus > 0 else 0.0
    gain = round(max(0.0, rate or 0.0) * (0 if trend in ("STALLED", "WORSENING") else 1), 2)
    return {"duel": d.get("duel"), "role": d["role"], "own_limit": lim, "rival_price": price, "days": days,
            "surplus_now": surplus, "surplus_ratio": ratio, "ticks_left": left, "phase": phase, "trend": trend,
            "recent_improvement_rate": rate, "ticks_since_change": since, "stalled": stalled,
            "expected_extra_gain": gain, "risk_cost": risk, "strong_rate_threshold": round(strong, 2),
            "configured_safe_ticks": PARAMS["SAFE_TICKS"], "effective_safe_ticks": safe_eff,
            "safe_ticks_note": f"SAFE_TICKS {PARAMS['SAFE_TICKS']} + {max(0, same_deadline - 1)} por duelos con el "
                               f"mismo deadline (una aceptación por tick)", "same_deadline": same_deadline,
            "our_offer_exists": d.get("your_offer") is not None}


def decide(d: dict, f: dict) -> tuple[str, str]:
    """(acción, motivo). acción ∈ accept | counter | wait | open. Esperar NUNCA es el defecto: solo si la mejora
    esperada del rival supera el riesgo sobre el excedente actual (que crece hacia el deadline)."""
    m, ratio, left, phase, trend = f["surplus_now"], f["surplus_ratio"], f["ticks_left"], f["phase"], f["trend"]
    if m is None:
        return ("open", "sin oferta rival: abrir en la ventana de habla") if left <= PARAMS["SPEAK_AT"] \
            and not f["our_offer_exists"] else ("wait", "sin oferta rival todavía")
    if m < 0:
        if left <= PARAMS["SPEAK_AT"] and not f["our_offer_exists"]:
            return "open", "oferta rival fuera de límite: una oferta propia (ancla, sin revelar el límite)"
        return "wait", "oferta rival fuera de nuestro límite: nunca se acepta"
    worth_waiting = f["expected_extra_gain"] > f["risk_cost"]
    if left <= f["effective_safe_ticks"]:
        return "accept", "cierre seguro: ventana final con oferta rentable dentro de límite"
    if phase == "LATE":
        return "accept", "fase final: un trato positivo vale más que arriesgar el no-trato"
    if trend == "WORSENING":
        return "accept", "el rival empeora: asegurar el excedente actual"
    if trend == "UNKNOWN":  # primera oferta: una excepcional se toma ya (en la práctica, dos así empeoraron después)
        if ratio >= PARAMS["EARLY_ACCEPT_RATIO"]:
            return "accept", "primera oferta excepcional: asegurarla antes de que empeore"
        return "wait", "primera oferta: dejar que el rival revele su trayectoria"
    if phase == "MID":
        if trend == "STRONG_IMPROVEMENT" and worth_waiting:
            return "wait", "mejora fuerte y la ganancia esperada supera el riesgo: esperar un poco"
        if ratio >= PARAMS["MID_ACCEPT_RATIO"]:
            return "accept", "excedente actual sólido: domina al beneficio esperado de esperar"
        if trend == "STALLED":
            if not f["our_offer_exists"]:
                return "counter", "rival estancado: una contraoferta antes de cerrar"
            if ratio >= PARAMS["STALLED_ACCEPT_RATIO"]:
                return "accept", "rival estancado tras nuestra contraoferta: cerrar un trato razonable"
        return ("wait", "mejora reciente mayor que el riesgo") if worth_waiting else \
            ("accept", "esperar no compensa el riesgo sobre el excedente actual")
    # EARLY: dejar que el rival revele información; aceptar solo lo muy fuerte si ya no mejora con fuerza
    if trend == "STRONG_IMPROVEMENT":
        return "wait", "el rival mejora con fuerza: esperar (callar no encoge la tarta)"
    if ratio >= PARAMS["EARLY_ACCEPT_RATIO"] and not worth_waiting:
        return "accept", "excedente excepcional y el rival ya no mejora con fuerza"
    if trend == "STALLED" and not f["our_offer_exists"]:
        return "counter", "rival estancado al principio: una contraoferta"
    return "wait", "fase temprana: sin concesiones innecesarias (en MID se cierra lo razonable)"


def counter_price(f: dict) -> int:
    """Contraoferta: su precio movido una fracción del excedente a nuestro favor. Nunca nuestro límite."""
    step = max(1, int(round(PARAMS["COUNTER_FRAC"] * f["surplus_now"])))
    return int(f["rival_price"] - step) if f["role"] == "buyer" else int(f["rival_price"] + step)


def duel_candidates(duels: list, tick: int) -> list:
    """Candidatas para el tick, con el mismo espíritu que las del coordinador: dicts con `type`, `duel`, `score`, `du`
    (excedente), `why` y `facts`. type ∈ {"duel_accept", "duel_say"}. Ordenar por `score`; UNA aceptación por tick.
    Los duelos ya aceptados (pendientes de liquidar) NO deben pasarse: consumirían plazas del escalonado."""
    playable = [d for d in duels if d.get("status") == "live" and d.get("deadline_tick") is not None
                and (PARAMS["PLAY_DAYS"] or not _is_days(d))]
    by_deadline: dict = {}
    for d in duels:
        if d.get("status") == "live":
            by_deadline[d.get("deadline_tick")] = by_deadline.get(d.get("deadline_tick"), 0) + 1
    in_limit = []                                     # deadlines de duelos con una oferta rival aceptable ya
    for d in playable:
        ro = d.get("rival_offer") or {}
        m = margin(d, ro.get("price"), ro.get("days") if _is_days(d) else None)
        if m is not None and m >= 0:
            in_limit.append(d["deadline_tick"])
    out = []
    for d in playable:
        deadline = d["deadline_tick"]
        left = deadline - tick
        if left <= 0:
            continue  # no enviar acciones sobre un snapshot caducado
        days_duel = _is_days(d)
        # una aceptación por tick: compiten los duelos con el mismo deadline y los aceptables con deadline ≤ el nuestro
        queue = sum(1 for x in in_limit if x <= deadline)
        f = analyze(d if days_duel else dict(d, rival_offer={**(d.get("rival_offer") or {}), "days": None}),
                    tick, max(by_deadline.get(deadline, 1), queue))
        action, reason = decide(d, f)
        f.update(action=action, reason=reason)
        why = (f"duelo {d['duel']} fase={f['phase']} {f['role']} límite {f['own_limit']:.0f} rival {f['rival_price']} "
               f"excedente {f['surplus_now']} ({(f['surplus_ratio'] or 0):.1%}) tendencia {f['trend']} mejora "
               f"{f['recent_improvement_rate']} esperado {f['expected_extra_gain']} riesgo {f['risk_cost']} quedan "
               f"{left} ⇒ {action.upper()}: {reason}")
        if action == "accept":
            urgent = left <= f["effective_safe_ticks"] + 1 or f["phase"] == "LATE"
            # urgencia primero (antes el deadline más próximo); entre iguales, más excedente
            score = (1000 + 10 * max(0, 50 - left) if urgent else 500) + int(f["surplus_now"])
            out.append({"type": "duel_accept", "duel": d["duel"], "du": f["surplus_now"], "score": score,
                        "why": why, "facts": f})
            continue
        ours = None
        if action == "counter":
            ours = counter_price(f)
        elif action == "open":
            ours = _own_price(d, PARAMS["ANCHOR"])
        elif f["rival_price"] is None and left <= PARAMS["SPEAK_AT"] and PARAMS["LADDER"]:
            # escalera (opt-in) solo frente a rivales mudos: un peldaño por tick, nunca hacia atrás
            mine = (d.get("your_offer") or {}).get("price")
            ladder = [s_ for s_ in PARAMS["LADDER"] if s_ < PARAMS["ANCHOR"]]
            below = [s_ for s_ in ladder if mine is not None and
                     (_own_price(d, s_) > mine if d["role"] == "buyer" else _own_price(d, s_) < mine)]
            if below:
                ours, reason = _own_price(d, below[0]), "escalera opt-in frente a rival mudo"
        if ours is not None:
            c = {"type": "duel_say", "duel": d["duel"], "price": ours, "du": None, "score": 100,
                 "text": f"{ours} P y cerramos ahora.", "why": why, "facts": f}
            if days_duel:
                c["days"] = _best_days(d)
                c["text"] = f"{ours} P con entrega el día {c['days']} y cerramos ahora."
            out.append(c)
    return sorted(out, key=lambda c: -c["score"])


def replay(duels: list, params: Optional[dict] = None) -> dict:
    """Repite duelos terminados tick a tick con la política: {'captured', 'best', 'outside_limit'}.
    Sirve para medir y reajustar PARAMS con datos reales al terminar cada oleada."""
    saved = dict(PARAMS)
    PARAMS.update(params or {})
    captured = best = 0.0
    outside = 0
    try:
        for d in duels:
            msgs = d.get("messages") or []
            dl = d.get("deadline_tick")
            if not msgs or dl is None:
                continue
            best += max([margin(d, m["price"], m.get("days")) or 0 for m in msgs if m.get("price") is not None] + [0])
            for t in range(min(m["tick"] for m in msgs), dl):
                seen = [m for m in msgs if m["tick"] <= t]
                priced = [m for m in seen if m.get("price") is not None and m.get("from") == d.get("rival")]
                if not priced:
                    continue
                st = dict(d, status="live", messages=seen, your_offer=None,
                          rival_offer={"price": priced[-1]["price"], "days": priced[-1].get("days")})
                acc = [c for c in duel_candidates([st], t) if c["type"] == "duel_accept"]
                if acc:
                    captured += max(0.0, acc[0]["du"])
                    outside += acc[0]["du"] < 0
                    break
    finally:
        PARAMS.clear()
        PARAMS.update(saved)
    return {"captured": captured, "best": best, "outside_limit": outside}
