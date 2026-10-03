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
Opt-in PARAMS["PROFILES"] (`--profiles`) y PARAMS["LOGROLL"] (`--logroll`): ver la sección de perfiles más abajo.
Opt-in PARAMS["LADDER"]: frente a un rival que no habla, en vez de una sola oferta, una escalera de ofertas que se
acerca al límite (por defecto desactivada: una sola oferta). PROBE queda absorbido: la contraoferta a un rival
estancado es ahora parte de la política por defecto.

Repetición individual retrospectiva (sin límite compartido de aceptaciones ni validación fuera de muestra): 98 % del margen máximo disponible (562 de 574 P) con los parámetros por defecto, frente
a 77 % aceptando en cuanto hay un 30 % de margen. El viernes, sin módulo de duelos, se capturó 0.
"""
from __future__ import annotations

import math
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
    "PROBE": False,       # rival plantado con tiempo de sobra: UNA contraoferta a mitad de camino antes de aceptar
    "PROFILES": False,    # reglas por perfil del bot de la casa (Plata, Verde, Oro, Luna, Rojo, Noche) y mudos pronto
    "LOGROLL": False,     # días: conceder los que nos cuestan poco a cambio de precio (necesita PLAY_DAYS)
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
        prof = rival_profile(d) if PARAMS["PROFILES"] else None
        traj = rival_trajectory(d)
        safe = f["effective_safe_ticks"]
        if prof and f["rival_price"] is None and not traj:
            muted = _mute_offer(d, tick, left)
            if muted:
                muted["facts"] = f
                out.append(muted)
            continue
        if prof and prof != "desconocido" and f["surplus_now"] is not None and f["surplus_now"] >= 0:
            pm = margin(dict(d, your_days_weight=None), f["rival_price"])
            profile_accept, _ = _profile_rule(
                prof, d, pm, f["own_limit"], traj, f["recent_improvement_rate"] or 0,
                f["trend"] == "STALLED", left, safe)
            action, reason = (("accept", f"perfil {prof}: aceptar según política del perfil")
                              if profile_accept else ("wait", f"perfil {prof}: esperar según política del perfil"))
        if (PARAMS["PROBE"] and action == "wait" and f["trend"] == "STALLED" and
                f["rival_price"] is not None and f["surplus_now"] is not None and f["surplus_now"] >= 0 and
                d.get("your_offer") is None and left > safe + 2):
            action, reason = "counter", "sondeo opt-in a rival estancado"
        if (PARAMS["LOGROLL"] and days_duel and f["rival_price"] is not None and
                f["surplus_now"] is not None and f["surplus_now"] >= 0 and prof != "empeora" and
                left > safe + 1 and not _our_messages(d)):
            lr = logroll_offer(d, f["rival_price"], f.get("days"))
            if lr and lr["gain"] >= PROFILE_PARAMS["LOGROLL_MIN_GAIN"] + _decay(d) * f["surplus_now"]:
                out.append({"type": "duel_say", "duel": d["duel"], "price": lr["price"], "days": lr["days"],
                            "du": f["surplus_now"] + lr["gain"], "score": 300,
                            "text": _say_text(lr["price"], lr["days"]),
                            "why": f"logroll: día {lr['days']} por precio (+{lr['gain']:.1f} P)", "facts": f})
                continue
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
                _maybe_logroll(d, c)
            out.append(c)
    return sorted(out, key=lambda c: -c["score"])


# ---------------------------------------------------------------- perfiles de rival (opt-in: PARAMS["PROFILES"])
# Duelos I (rivales = bots de la casa con perfil fijo, los mismos para todos los equipos):
#   Plata cede 1-3 P/tick de forma constante; Verde mejora en ciclos (escalón + meseta); Oro y Luna dan un salto
#   grande (16-26 P) y se plantan; Rojo EMPEORA con el tiempo (vendedor 133 → 166 con nosotros compradores);
#   Noche a veces empeora; algunos rivales no hablan nunca. El nombre es un prior; un empeoramiento observado manda.
RIVAL_PROFILES = {"plata": "cede", "verde": "ciclos", "oro": "salto", "luna": "salto", "rojo": "empeora",
                  "noche": "mixto"}

PROFILE_PARAMS = {
    "CEDE_SHARE": 0.90,      # con rivales que ceden solo se adelanta la aceptación con un margen enorme
    "JUMP": 12,              # mejora de un tick ≥ JUMP primas = «salto»; después se plantan
    "MUTE_AFTER": 3,         # ticks de silencio total del rival antes de abrir nosotros
    "MUTE_GAP": 3,           # ticks entre nuestras ofertas a un rival mudo
    "MUTE_MAX": 3,           # ofertas como mucho a un mudo (cada una puede costar una ronda de decay)
    "MUTE_STEP": 0.10,       # cada oferta nueva al mudo rebaja el ancla 10 puntos del límite (0,30 → 0,20 → 0,10)
    "DUEL_TICKS": {0.06: 12, 0.08: 16, 0.10: 12},   # duración por decay de la sesión (práctica, Duelos II, III)
    "RIVAL_DAY_SCALE": 1.0,  # prior: al rival le importa un día de distancia lo mismo que a nosotros de media
    "LOGROLL_MIN_GAIN": 2.0, # ganancia mínima (P) de un logroll además de la ronda de decay que cuesta
    "LOGROLL_SHARE": 0.5,    # parte del crecimiento estimado de la tarta que se ofrece al rival
}


def rival_profile(duel: dict) -> str:
    """Perfil del rival por su alias («Rival Plata» → «cede»), «desconocido» si no figura."""
    words = str(duel.get("rival") or "").lower().split()
    return next((prof for key, prof in RIVAL_PROFILES.items() if key in words), "desconocido")


def _profile_rule(prof, d, pm, lim, traj, slope, stalled, left, safe):
    """(aceptar, prioridad) para un rival con perfil y oferta dentro de límite."""
    urgent, worsened = left <= safe, slope < 0
    if prof == "empeora":                                     # cada tick de espera cuesta: aceptar ya
        return True, 2000
    if prof == "mixto":                                       # aceptar en cuanto empeora un solo tick
        steps = _steps(d, traj)
        return urgent or worsened or stalled or (len(traj) >= 2 and steps[-1] < 0), 1500
    if prof == "salto":                                       # tras el salto se planta: aceptar ya
        jumped = any(x >= PROFILE_PARAMS["JUMP"] for x in _steps(d, traj))
        return urgent or worsened or jumped, 1500
    if prof in ("cede", "ciclos"):                            # mesetas de Verde no son plantones: esperar
        return urgent or worsened or pm >= PROFILE_PARAMS["CEDE_SHARE"] * lim, 0
    return urgent or pm >= PARAMS["GOOD_SHARE"] * lim or worsened or stalled, 0


def _steps(d: dict, traj: list) -> list:
    """Mejoras de precio por mensaje a nuestro favor (positivo = el rival cede)."""
    sign = 1 if d["role"] == "buyer" else -1
    return [sign * (a[1] - b[1]) for a, b in zip(traj, traj[1:])] or [0]


def _our_messages(d: dict) -> list:
    rival = d.get("rival")
    return [m for m in d.get("messages") or [] if m.get("from") != rival and m.get("price") is not None]


def _decay(d: dict) -> float:
    return float(d.get("decay_per_round") or 0.08)


def duel_length(d: dict) -> int:
    return int(d.get("duel_ticks") or PROFILE_PARAMS["DUEL_TICKS"].get(round(_decay(d), 2), 16))


def _mute_offer(d: dict, tick: int, left: int) -> Optional[dict]:
    """Rival mudo: abrir pronto y rebajar el ancla por escalones (máximo MUTE_MAX ofertas)."""
    start = d.get("start_tick", d["deadline_tick"] - duel_length(d))
    ours = _our_messages(d)
    if tick - start < PROFILE_PARAMS["MUTE_AFTER"] or len(ours) >= PROFILE_PARAMS["MUTE_MAX"] or left <= 1:
        return None
    if ours and tick - max(x["tick"] for x in ours) < PROFILE_PARAMS["MUTE_GAP"]:
        return None
    price = _own_price(d, max(0.0, PARAMS["ANCHOR"] - len(ours) * PROFILE_PARAMS["MUTE_STEP"]))
    c = {"type": "duel_say", "duel": d["duel"], "price": price, "du": None, "score": 100,
         "text": _say_text(price, None), "why": f" ⇒ rival mudo: oferta {len(ours) + 1}"}
    if _is_days(d):
        c["days"] = _best_days(d)
        _maybe_logroll(d, c)
    return c


# ---------------------------------------------------------------- logrolling de días (opt-in: PARAMS["LOGROLL"])
def rival_days(d: dict) -> Optional[int]:
    """Día que prefiere el rival: el más repetido en sus ofertas (empate ⇒ el más reciente)."""
    rival = d.get("rival")
    seen = [_num(m.get("days")) for m in d.get("messages") or [] if m.get("from") == rival and m.get("price") is not None]
    seen = [int(x) for x in seen if x is not None and x == int(x) and 0 <= x <= 10]
    if not seen:
        x = _num((d.get("rival_offer") or {}).get("days"))
        return int(x) if x is not None and x == int(x) and 0 <= x <= 10 else None
    return max(set(seen), key=lambda k: (seen.count(k), max(i for i, v in enumerate(seen) if v == k)))


def logroll_offer(d: dict, price: Optional[int], day_ref: Optional[int]) -> Optional[dict]:
    """Mejor (precio, día) para nosotros que deja al rival (estimado) igual o mejor que (price, day_ref).

    El rival revela su día preferido en sus ofertas; su utilidad se estima como −s·|día − preferido|, con s la
    pendiente media de la nuestra × RIVAL_DAY_SCALE. Conceder un día que a nosotros nos cuesta poco se cobra en precio
    (y al revés). El precio NUNCA sale de nuestro límite. None si falta información (`days_table` no entiende el
    formato de your_days_weight o el rival no ha revelado día) o si no hay mejora."""
    u = days_table(d)
    pref = rival_days(d)
    if u is None or pref is None or price is None:
        return None
    ref = pref if day_ref is None else int(day_ref)
    s = PROFILE_PARAMS["RIVAL_DAY_SCALE"] * sum(abs(u[k + 1] - u[k]) for k in range(10)) / 10
    est = lambda k: -s * abs(k - pref)
    lim = float(d["your_limit"])
    buyer = d["role"] == "buyer"
    base = (-price if buyer else price) + u[ref]
    best = None
    for k in range(11):
        if k == ref:
            continue
        comp = est(ref) - est(k)                  # lo que pierde el rival al pasar de ref a k (negativo = gana)
        joint = (u[k] - u[ref]) - comp            # lo que crece la tarta (estimado) al cambiar de día
        if joint > 0:                             # cedemos parte de lo que crece: colchón si subestimamos al rival
            comp += PROFILE_PARAMS["LOGROLL_SHARE"] * joint
        # redondeo a favor del rival y 1 P más para que prefiera estrictamente nuestra oferta
        p = math.ceil(price + comp) + 1 if buyer else math.floor(price - comp) - 1
        if (buyer and p > lim) or (not buyer and p < lim):
            continue
        ours = (-p if buyer else p) + u[k]
        if best is None or ours > best[0]:
            best = (ours, p, k)
    if best is None or best[0] - base <= 0:
        return None
    return {"price": best[1], "days": best[2], "gain": best[0] - base}


def _maybe_logroll(d: dict, c: dict) -> None:
    """Oferta propia en un duelo con días: con LOGROLL, mover día y precio si mejora; texto con el día."""
    if PARAMS["LOGROLL"]:
        lr = logroll_offer(d, c["price"], c["days"])
        if lr:
            c.update(price=lr["price"], days=lr["days"])
    c["text"] = _say_text(c["price"], c["days"])


def _say_text(price: int, days: Optional[int]) -> str:
    return f"{price} P y cerramos ahora." if days is None else f"{price} P con entrega el día {days} y cerramos ahora."


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
