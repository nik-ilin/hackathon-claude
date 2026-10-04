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
Duelos de precio + días: omitidos por defecto. Con PARAMS["PLAY_DAYS"] = True (opt-in, `duel_runner.py --days`) se juegan
con la misma política. Fórmula VERIFICADA con los 37 duelos liquidados de la sesión 3: result = (margen_de_precio ∓
peso·días) · (1 − decay)^rondas, con «−» para el comprador (cada día le cuesta) y «+» para el vendedor (cada día le suma).
Se exige SIEMPRE precio dentro de límite Y excedente total > 0; toda oferta propia lleva `days` (el que maximiza NUESTRA
utilidad neta). Ver «CONVENCIÓN DE SIGNOS» más abajo: antes se sumaban los días al comprador y se aceptaron tratos que el
servidor liquidó en negativo (5842, 5694, 5762, 5919, 5702, 5728, 5844).
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
    "HISTORY_MIN": 8,     # tratos cerrados comparables (mismo rol y temas) necesarios para usar el historial al anclar
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
    # --- perfil AGRESIVO (duel_runner --day3 lo activa; --no-aggressive lo apaga). Decide por VALOR ESPERADO sobre el excedente
    #     TOTAL (precio + utilidad firmada de los días): aceptar ahora vale m; esperar H ticks vale (1−p)^H·(m + mejora·H), con p
    #     la probabilidad por tick de perder la oferta (sube al acercarse el deadline y con la cola de aceptaciones).
    "AGGRESSIVE": False,
    "AGG_EARLY_RATIO": 0.25,     # EARLY, rival sin mejora fuerte: aceptar si total/límite ≥ esto (antes 0,30)
    "AGG_MID_RATIO": 0.10,       # MID, rival sin mejora fuerte: ídem (antes 0,15)
    "AGG_STALL_RATIO": 0.03,     # rival estancado o empeorando: esperar no añade nada, cerrar desde aquí (antes 0,05 tras contraoferta)
    "AGG_HORIZON": 2,            # ticks de espera que se valoran (no se apuesta a mejoras lejanas)
    "AGG_RISK_BASE": 0.05,       # p de perder la oferta por tick… (antes 0,02 como coste)
    "AGG_RISK_URGENCY": 0.25,    # … + esto / ticks útiles (ticks restantes − cola de aceptaciones por delante)
    "AGG_MIN_GAIN_FRAC": 0.02,   # no esperar por mejoras esperadas < max(2 P, 2 % del límite) en el horizonte
    "AGG_ZONE_FRAC": 0.04,       # total negativo con tiempo: contraoferta en nuestra zona, a un 4 % del límite de nuestra reserva
    "AGG_GOOD_RATIO": 0.32,      # MID con rival que MEJORA FUERTE: aceptar ya si el total ya es «bueno» (antes 0,15 y solo sin mejora fuerte)
    "AGG_EARLY_GOOD_RATIO": 0.45,  # EARLY con rival que mejora fuerte: solo lo excepcional (antes: esperar SIEMPRE en EARLY)
    "AGG_PRIOR_RATE_FRAC": 0.02, # primera oferta (tendencia desconocida): mejora a priori por tick = 2 % del límite, no 0
    "AGG_SPEAK_AT": 8,           # rival mudo / fuera de zona: hablar desde 8 ticks antes (antes 5)
    "AGG_MAX_OWN": 2,            # como mucho 2 ofertas propias por duelo (cada ronda encoge la tarta)
    "AGG_OWN_GAP": 2,            # ticks entre ofertas propias
    "LEARN": False,       # aprendizaje en línea (duel_runner --learn): refinar accept/wait y el ancla con la historia confirmada
}
# Ganchos del aprendizaje en línea (duel_learning.py). Con PARAMS["LEARN"] y un hook instalado, la decisión base puede
# refinarse (accept↔wait, ancla de apertura) SOLO con evidencia suficiente; sin hook o sin evidencia, manda la base.
HOOKS = {"refine": None, "open_share": None}
NOTES: dict = {}     # duelo → {action, reason, learned} de la última evaluación (lo lee duel_tree para registrar también las esperas)
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


# ---------------------------------------------------------------- CONVENCIÓN DE SIGNOS (léase antes de tocar nada)
#
# El servidor da `your_days_weight` como un PESO POSITIVO por día (p. ej. 4.48) y una frase `days_meaning` que fija su signo:
#     comprador: "each delivery day costs you this much cash"          → cada día NOS RESTA `peso` primas
#     vendedor : "each delivery day adds this much cash to your side"  → cada día NOS SUMA `peso` primas
# La utilidad de días es lineal: ±peso·día. Verificado contra `result` de los 37 duelos liquidados (data en
# test_duels_days.py): result = (margen_de_precio ∓ peso·días) · (1 − decay)^rondas, con ∓ = − comprador, + vendedor.
# El peso es la MAGNITUD del efecto en el lado de quien lo recibe; el signo es del rol, no del número.
#
#   margen_de_precio = límite − precio   (comprador)        precio − límite   (vendedor)
#   excedente_total  = margen_de_precio + utilidad_de_días
#       comprador: margen_de_precio − peso·días        vendedor: margen_de_precio + peso·días
# Dos restricciones INDEPENDIENTES: (1) el límite de precio del servidor (margen_de_precio ≥ 0, los días nunca lo excusan) y
# (2) excedente_total > 0 (un trato con total ≤ 0 puntúa 0 o resta: peor que no cerrar). Ninguna sustituye a la otra.
#
# Otros formatos (lista/dict por día, escalar sin `days_meaning`) NO están verificados contra el servidor. Regla: si hay
# `days_meaning`, manda su frase; si no, los valores no negativos son magnitudes con el signo del ROL (nunca premian al
# comprador), y los valores con algún negativo se leen como utilidad ya firmada («firmado», no verificado, se avisa).
# Si no se entiende el formato, la utilidad es DESCONOCIDA (None): no se acepta nada hasta entenderla.
def days_sign(duel: dict) -> tuple:
    """(signo, fuente): −1 si cada día nos cuesta, +1 si nos suma. La frase del servidor manda sobre el rol."""
    text = str(duel.get("days_meaning") or "").lower()
    if "cost" in text:
        return -1, "days_meaning"
    if "add" in text:
        return 1, "days_meaning"
    return (-1 if duel.get("role") == "buyer" else 1), "rol"


def days_utility_table(duel: dict) -> Optional[list]:
    """Utilidad de días FIRMADA desde nuestro lado, en primas, para los días 0..10 (comprador ≤ 0, vendedor ≥ 0 en el
    formato verificado). None si el duelo no tiene tabla entendible."""
    raw = days_table(duel)
    if raw is None:
        return None
    sign, source = days_sign(duel)
    signed_input = any(v < 0 for v in raw) and source != "days_meaning"
    return list(raw) if signed_input else [sign * abs(v) for v in raw]


def days_format(duel: dict) -> str:
    """verificado (peso + days_meaning del servidor) | por_rol (magnitudes sin frase) | firmado (no verificado) | desconocido."""
    raw = days_table(duel)
    if raw is None:
        return "desconocido"
    if days_sign(duel)[1] == "days_meaning":
        return "verificado"
    return "firmado" if any(v < 0 for v in raw) else "por_rol"


def days_utility(duel: dict, days: Optional[int]) -> float:
    """Utilidad FIRMADA (primas) del día de entrega para nosotros: comprador −peso·día, vendedor +peso·día. 0 si el duelo no
    negocia días, `days` es None o el día está fuera de 0..10. Formato ilegible ⇒ 0.0 (el llamador decide con `days_known`)."""
    if days is None:
        return 0.0
    t = days_utility_table(duel)
    d = _num(days)
    if t is None or d is None or d != int(d) or not 0 <= d <= 10:
        return 0.0
    return t[int(d)]


_days_value = days_utility      # nombre histórico: ahora devuelve la utilidad FIRMADA (antes sumaba al comprador)


def days_known(duel: dict) -> bool:
    return (not _is_days(duel)) or days_utility_table(duel) is not None


def price_margin(duel: dict, price: Optional[int]) -> Optional[float]:
    """Excedente de PRECIO (sin días): límite − precio (comprador) o precio − límite (vendedor). Negativo = fuera de límite."""
    if price is None:
        return None
    lim = float(duel["your_limit"])
    return (lim - price) if duel["role"] == "buyer" else (price - lim)


def margin(duel: dict, price: Optional[int], days: Optional[int] = None) -> Optional[float]:
    """Excedente TOTAL en primas si cerramos a (`price`, `days`): margen de precio + utilidad firmada de los días.
    Comprador: (límite − precio) − peso·días. Vendedor: (precio − límite) + peso·días. Fuera del límite de precio devuelve el
    margen de precio negativo (los días nunca lo excusan). Puede ser negativo con el precio DENTRO de límite."""
    pm = price_margin(duel, price)
    if pm is None or pm < 0:
        return pm
    return pm + days_utility(duel, days)


def expected_result(duel: dict, price: Optional[int], days: Optional[int]) -> Optional[float]:
    """Lo que el servidor registra en `result` si se cierra ahora: excedente total · (1 − decay)^rondas (calibrado con los
    duelos liquidados). Un precio fuera de límite resta igual que dentro (el servidor puntúa la diferencia)."""
    if price is None:
        return None
    lim = float(duel["your_limit"])
    pm = (lim - price) if duel["role"] == "buyer" else (price - lim)
    return round((pm + days_utility(duel, days)) * (1 - _decay(duel)) ** int(duel.get("rounds") or 0), 2)


def rival_trajectory(duel: dict) -> list:
    """[(tick, precio)] de los mensajes con precio del rival, del más antiguo al más reciente."""
    rival = duel.get("rival")
    return [(m["tick"], m["price"]) for m in duel.get("messages", [])
            if m.get("price") is not None and m.get("from") == rival]


def _best_days(duel: dict) -> int:
    """Nuestro día de máxima utilidad NETA (firmada): comprador → el más pronto (0), vendedor → el más tardío (10), salvo
    que la tabla diga otra cosa. En empate (o sin tabla) el día que ya propuso el rival: si a nosotros nos da igual, se lo
    concedemos (la tarta crece cuando cada uno se queda con lo que más valora)."""
    ro = duel.get("rival_offer") or {}
    rd = _num(ro.get("days"))
    pref = int(rd) if rd is not None and rd == int(rd) and 0 <= rd <= 10 else 0
    return max(range(11), key=lambda k: (days_utility(duel, k), k == pref, -k))


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
    if _is_days(d) and days is None and price is not None and days_known(d):
        days = min(range(11), key=lambda k: (days_utility(d, k), k))      # sin día en la oferta: se valora el PEOR (prudente)
    m = margin(d, price, days)
    pm_only = price_margin(d, price)
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
            "price_margin": pm_only, "days_utility": days_utility(d, days) if days is not None else 0.0,
            "days_sign": days_sign(d)[0] if _is_days(d) else None, "days_format": days_format(d) if _is_days(d) else None,
            "days_known": days_known(d), "expected_result": expected_result(d, price, days),
            "surplus_now": surplus, "surplus_ratio": ratio, "ticks_left": left, "phase": phase, "trend": trend,
            "recent_improvement_rate": rate, "ticks_since_change": since, "stalled": stalled,
            "expected_extra_gain": gain, "risk_cost": risk, "strong_rate_threshold": round(strong, 2),
            "configured_safe_ticks": PARAMS["SAFE_TICKS"], "effective_safe_ticks": safe_eff,
            "safe_ticks_note": f"SAFE_TICKS {PARAMS['SAFE_TICKS']} + {max(0, same_deadline - 1)} por duelos con el "
                               f"mismo deadline (una aceptación por tick)", "same_deadline": same_deadline,
            "our_offer_exists": d.get("your_offer") is not None, "tick": tick, "queue_ahead": max(0, same_deadline - 1)}


def decide(d: dict, f: dict) -> tuple[str, str]:
    """(acción, motivo). acción ∈ accept | counter | wait | open. Esperar NUNCA es el defecto: solo si la mejora
    esperada del rival supera el riesgo sobre el excedente actual (que crece hacia el deadline)."""
    m, ratio, left, phase, trend = f["surplus_now"], f["surplus_ratio"], f["ticks_left"], f["phase"], f["trend"]
    if m is None:
        return ("open", "sin oferta rival: abrir en la ventana de habla") if left <= PARAMS["SPEAK_AT"] \
            and not f["our_offer_exists"] else ("wait", "sin oferta rival todavía")
    if not f.get("days_known", True):
        return "wait", "formato de your_days_weight no entendido: no se acepta sin conocer el coste de los días"
    if m <= 0:      # dos restricciones independientes: límite de precio del servidor Y excedente total positivo
        outside = (f.get("price_margin") or 0) < 0
        what = ("oferta rival fuera de límite de precio" if outside else
                f"el precio está dentro de límite pero los días ({f.get('days_utility'):+.1f} P) dejan el excedente total en {m:.1f} P")
        if left <= PARAMS["SPEAK_AT"] and not f["our_offer_exists"]:
            return "open", f"{what}: una oferta propia realista (sin revelar el límite)"
        return "wait", f"{what}: nunca se acepta"
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


def agg_p_lose(f: dict) -> float:
    """Probabilidad por tick de perder una oferta rival si no se acepta (heurística configurable, no medida): base + urgencia
    sobre los ticks ÚTILES, descontando la cola de aceptaciones por delante (una aceptación por tick)."""
    useful = max(1, f["ticks_left"] - max(0, f.get("queue_ahead", 0)))
    p = PARAMS["AGG_RISK_BASE"] + PARAMS["AGG_RISK_URGENCY"] / useful
    if f["trend"] == "WORSENING":
        p += 0.25
    return round(min(0.95, p), 4)


def agg_values(f: dict) -> dict:
    """Valor de aceptar ya frente a esperar AGG_HORIZON ticks, ambos sobre el excedente TOTAL."""
    m = f["surplus_now"] or 0.0
    if f["trend"] == "UNKNOWN" or (f["trend"] == "STALLED" and f["phase"] == "EARLY"):
        # sin trayectoria, o una pausa temprana: no se supone que el rival ya no se moverá (eso aceptaba la 1.ª oferta)
        rate = PARAMS["AGG_PRIOR_RATE_FRAC"] * abs(f["own_limit"])
    else:
        rate = max(0.0, f["recent_improvement_rate"] or 0.0) if f["trend"] in ("STRONG_IMPROVEMENT", "WEAK_IMPROVEMENT") else 0.0
    h = max(0, min(PARAMS["AGG_HORIZON"], f["ticks_left"] - 1 - max(0, f.get("queue_ahead", 0))))
    p = agg_p_lose(f)
    gain = rate * h
    ev_wait = round(((1 - p) ** h) * (m + gain), 2) if h > 0 else 0.0
    return {"ev_now": round(m, 2), "ev_wait": ev_wait, "p_lose": p, "horizon": h, "gain": round(gain, 2),
            "min_gain": round(max(2.0, PARAMS["AGG_MIN_GAIN_FRAC"] * abs(f["own_limit"])), 2)}


def reserve_price(d: dict, days: Optional[int]) -> float:
    """Precio de RESERVA con el día dado: aquel en que el excedente total es 0 (comprador: límite + utilidad de días ≤ 0 → paga
    menos; vendedor: límite − utilidad de días ≥ 0 → puede cobrar menos)."""
    lim, du = float(d["your_limit"]), days_utility(d, days)
    return lim + du if d["role"] == "buyer" else lim - du


def zone_price(d: dict, days: Optional[int]) -> int:
    """Oferta propia DENTRO de nuestra zona: a AGG_ZONE_FRAC del límite de la reserva, siempre con total > 0 y precio en límite."""
    lim = float(d["your_limit"])
    r = reserve_price(d, days)
    pad = max(1.0, PARAMS["AGG_ZONE_FRAC"] * abs(lim))
    if d["role"] == "buyer":
        p = int(math.floor(min(lim, r - pad)))
    else:
        p = int(math.ceil(max(lim, r + pad)))
    return max(1, p)


def decide_aggressive(d: dict, f: dict) -> tuple:
    """Perfil agresivo. Nunca acepta con total ≤ 0 ni con el precio fuera de límite (barrera final en duel_candidates)."""
    m, ratio, left, phase, trend = f["surplus_now"], f["surplus_ratio"], f["ticks_left"], f["phase"], f["trend"]
    ours = _our_messages(d)
    can_speak = len(ours) < PARAMS["AGG_MAX_OWN"] and (not ours or f["tick"] - max(x["tick"] for x in ours) >= PARAMS["AGG_OWN_GAP"])
    if m is None:
        if left <= PARAMS["AGG_SPEAK_AT"] and can_speak and left > 1:
            return "open", "sin oferta rival: abrir pronto en nuestra zona (perfil agresivo)"
        return "wait", "sin oferta rival todavía"
    if not f.get("days_known", True):
        return "wait", "formato de your_days_weight no entendido: no se acepta sin conocer el coste de los días"
    if m <= 0:
        outside = (f.get("price_margin") or 0) < 0
        what = (f"precio fuera de límite (margen de precio {f.get('price_margin')})" if outside else
                f"total {m:.1f} P ≤ 0 (precio {f.get('price_margin'):+.1f}, días {f.get('days_utility'):+.1f})")
        if left > 1 and can_speak:
            return "zone", f"{what}: contraoferta hacia nuestra zona de acuerdo"
        return "expire", f"{what}: se deja vencer POR ECONOMÍA (aceptar daría resultado ≤ 0), no por inacción"
    v = agg_values(f)
    f["agg"] = v
    if phase == "LATE" or left <= 1 + max(0, f.get("queue_ahead", 0)):
        return "accept", f"cierre: quedan {left} ticks (cola {f.get('queue_ahead', 0)}) y el total es positivo ({m:.1f} P)"
    if (trend == "WORSENING" or (trend == "STALLED" and phase != "EARLY")) and ratio >= PARAMS["AGG_STALL_RATIO"]:
        # un plantón temprano suele ser una pausa (Verde cicla): en EARLY no se cierra solo por eso
        return "accept", f"rival {trend.lower()}: esperar no añade valor; total {m:.1f} P ({ratio:.0%})"
    strong = trend == "STRONG_IMPROVEMENT"
    good = PARAMS["AGG_EARLY_GOOD_RATIO"] if phase == "EARLY" else PARAMS["AGG_GOOD_RATIO"]
    if strong and ratio >= good:
        return "accept", f"total {m:.1f} P ({ratio:.0%}) ya es bueno (≥ {good:.0%} en {phase}): aceptar aunque el rival mejore"
    limit_ratio = PARAMS["AGG_EARLY_RATIO"] if phase == "EARLY" else PARAMS["AGG_MID_RATIO"]
    if not strong and ratio >= limit_ratio:
        return "accept", f"total {m:.1f} P ({ratio:.0%}) ≥ umbral {phase} {limit_ratio:.0%} y el rival no mejora con fuerza"
    if v["gain"] < v["min_gain"]:
        return "accept", f"mejora esperada {v['gain']} P < {v['min_gain']} P: no se espera por poco"
    if v["ev_now"] >= v["ev_wait"]:
        return "accept", f"valor de aceptar {v['ev_now']} ≥ valor de esperar {v['ev_wait']} (p pérdida {v['p_lose']}/tick, {v['horizon']} ticks)"
    if trend == "STALLED" and can_speak and not f["our_offer_exists"]:
        return "counter", "rival estancado con excedente pequeño: una contraoferta"
    return "wait", f"esperar: valor {v['ev_wait']} > {v['ev_now']} (mejora {v['gain']} P en {v['horizon']} ticks, p pérdida {v['p_lose']}/tick)"


def counter_price(f: dict) -> int:
    """Contraoferta: su precio movido una fracción del excedente de PRECIO a nuestro favor (los días se negocian aparte, en
    el día que elegimos). Nunca nuestro límite."""
    base = f["price_margin"] if f.get("price_margin") is not None else f["surplus_now"]
    step = max(1, int(round(PARAMS["COUNTER_FRAC"] * max(base, 0))))
    return int(f["rival_price"] - step) if f["role"] == "buyer" else int(f["rival_price"] + step)


def vanish_risk(f: dict) -> float:
    """Probabilidad HEURÍSTICA (no medida) de perder una buena oferta si NO se acepta en este tick: 1.0 si es el último tick útil;
    si no, por tendencia del rival (empeora > estancado > mejora) y por cercanía del deadline. Solo ordena aceptaciones."""
    if f["ticks_left"] <= f["effective_safe_ticks"]:
        return 1.0
    base = {"WORSENING": 0.5, "STALLED": 0.2, "UNKNOWN": 0.1}.get(f["trend"], 0.05)
    return min(0.95, base + 0.5 / max(1, f["ticks_left"]))


def agg_priority(f: dict, competitors: int) -> float:
    """Prioridad EXPLÍCITA entre aceptaciones del mismo tick (perfil agresivo): pérdida esperada si esta espera un tick =
    total × P(perderla), con P = 1 si ya no quedan ticks para todas las aceptaciones con deadline ≤ el suyo. Desempates:
    deadline más próximo y más excedente. El orden de iteración no decide nada."""
    s = max(0.0, f["surplus_now"] or 0.0)
    p = 1.0 if f["ticks_left"] <= competitors else agg_p_lose(f)
    return round(500 + s * p + s / 100.0 - f["ticks_left"] / 1000.0, 4)


def accept_priority(f: dict) -> float:
    """Orden de las aceptaciones candidatas (solo una por tick): lo que PERDEMOS si esa oferta espera un tick = excedente neto ·
    riesgo de que desaparezca. Una oferta mediocre que vence ya (riesgo 1) solo pasa delante de otra claramente mejor si su
    excedente supera al de la otra × el riesgo de la otra; el excedente neto desempata. Siempre por encima de las ofertas
    propias (score 100-300)."""
    s = max(0.0, f["surplus_now"] or 0.0)
    # desempate: a igual pérdida esperada, antes el deadline más próximo (luego, más excedente neto)
    return round(500 + s * vanish_risk(f) + s / 100.0 - f["ticks_left"] / 1000.0, 4)


def history_share(history: list, role: str, days: bool) -> Optional[float]:
    """Mediana del margen de PRECIO capturado / límite en tratos puntuables comparables (mismo rol y mismos temas), o None
    si hay menos de PARAMS["HISTORY_MIN"] casos: con poca historia no se usa."""
    shares = []
    for h in history or []:
        if (h.get("session") == 1 or h.get("status") != "deal" or
                isinstance(h.get("result"), (int, float)) and h["result"] <= 0 or h.get("role") != role or
                ("days" in (h.get("issues") or [])) != days):
            continue
        if h.get("price") is None or not h.get("your_limit"):
            continue
        pm = price_margin({"role": role, "your_limit": h["your_limit"]}, h["price"])
        if pm is not None and pm >= 0:
            shares.append(pm / float(h["your_limit"]))
    if len(shares) < PARAMS["HISTORY_MIN"]:
        return None
    shares.sort()
    return shares[len(shares) // 2]


def realistic_share(d: dict, left: int, history: Optional[list] = None) -> float:
    """Distancia al límite de nuestra oferta propia sin oferta rival: el ancla ANCHOR (0,30) solo con tiempo de sobra; al
    acercarse el deadline se acerca al límite (un ancla extrema ya no deja tiempo de cerrar). Con historial suficiente
    (≥ HISTORY_MIN tratos comparables, mismo rol y temas) no se supera la mediana capturada. Siempre dentro del límite."""
    share = PARAMS["ANCHOR"] * min(1.0, max(0.25, left / float(max(1, PARAMS["SPEAK_AT"]))))
    hs = history_share(history, d["role"], _is_days(d))
    if hs is not None:
        share = min(share, max(0.05, hs))
    return round(share, 4)


def brief_line(d: dict, f: dict, action: str, reason: str) -> str:
    """Línea breve y accionable para el log: duelo, rol, precio, días, total, ticks, tendencia, riesgo, decisión y razón."""
    v = f.get("agg") or {}
    risk = f"p_pérdida {v['p_lose']}/tick" if v else f"riesgo {f.get('risk_cost')}"
    return (f"#{d.get('duel')} {d.get('role')} precio {f.get('rival_price')} días {f.get('days')} total "
            f"{f.get('surplus_now')} quedan {f.get('ticks_left')} {f.get('trend')} {risk} ⇒ {action.upper()}: {reason}")


def duel_candidates(duels: list, tick: int, history: Optional[list] = None) -> list:
    """Candidatas para el tick, con el mismo espíritu que las del coordinador: dicts con `type`, `duel`, `score`, `du`
    (excedente), `why` y `facts`. type ∈ {"duel_accept", "duel_say"}. Ordenar por `score`; UNA aceptación por tick.
    Los duelos ya aceptados (pendientes de liquidar) NO deben pasarse: consumirían plazas del escalonado."""
    NOTES.clear()
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
        if m is not None and m > 0 and days_known(d):
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
        action, reason = decide_aggressive(d, f) if PARAMS["AGGRESSIVE"] else decide(d, f)
        prof = rival_profile(d) if PARAMS["PROFILES"] else None
        traj = rival_trajectory(d)
        safe = f["effective_safe_ticks"]
        if prof and f["rival_price"] is None and not traj:
            muted = _mute_offer(d, tick, left, history)
            if muted:
                muted["facts"] = f
                out.append(muted)
            continue
        if prof and prof != "desconocido" and f["surplus_now"] is not None and f["surplus_now"] > 0:
            pm = f["surplus_now"]  # en duelos de dos cuestiones decide el excedente TOTAL, incluidos los días
            profile_accept, _ = _profile_rule(
                prof, d, pm, f["own_limit"], traj, f["recent_improvement_rate"] or 0,
                f["trend"] == "STALLED", left, safe)
            if PARAMS["AGGRESSIVE"]:
                # el perfil solo puede ADELANTAR un cierre (Rojo/Noche/salto); nunca retrasar una aceptación agresiva
                if profile_accept and action != "accept":
                    action, reason = "accept", f"perfil {prof}: cerrar ya según el perfil del rival"
            else:
                action, reason = (("accept", f"perfil {prof}: aceptar según política del perfil")
                                  if profile_accept else ("wait", f"perfil {prof}: esperar según política del perfil"))
        if (PARAMS["PROBE"] and action == "wait" and f["trend"] == "STALLED" and
                f["rival_price"] is not None and f["surplus_now"] is not None and f["surplus_now"] > 0 and
                d.get("your_offer") is None and left > safe + 2):
            action, reason = "counter", "sondeo opt-in a rival estancado"
        learned = None
        if PARAMS["LEARN"] and HOOKS["refine"] is not None and f["surplus_now"] is not None and f["surplus_now"] > 0 \
                and action in ("accept", "wait"):
            try:   # un fallo del aprendizaje nunca puede frenar ni cambiar una acción legal
                action, reason, learned = HOOKS["refine"](d, f, action, reason)
            except Exception as e:  # noqa: BLE001
                learned = {"learned": False, "note": f"aprendizaje no disponible ({type(e).__name__})"}
        if (PARAMS["LOGROLL"] and days_duel and f["rival_price"] is not None and
                f["surplus_now"] is not None and f["surplus_now"] > 0 and prof != "empeora" and
                left > safe + 1 and not _our_messages(d)):
            lr = logroll_offer(d, f["rival_price"], f.get("days"))
            if lr and lr["gain"] >= PROFILE_PARAMS["LOGROLL_MIN_GAIN"] + _decay(d) * f["surplus_now"]:
                out.append({"type": "duel_say", "duel": d["duel"], "price": lr["price"], "days": lr["days"],
                            "du": f["surplus_now"] + lr["gain"], "score": 300,
                            "text": _say_text(lr["price"], lr["days"]),
                            "why": f"logroll: día {lr['days']} por precio (+{lr['gain']:.1f} P)", "facts": f})
                continue
        if action == "accept" and not ((f["surplus_now"] or 0) > 0 and (f.get("price_margin") or 0) >= 0 and f.get("days_known", True)):
            action, reason = "wait", "no se acepta: excedente total ≤ 0, precio fuera de límite o coste de días desconocido"
        f.update(action=action, reason=reason)
        f["brief"] = brief_line(d, f, action, reason)
        NOTES[d["duel"]] = {"action": action, "reason": reason, "learned": learned, "brief": f["brief"]}
        if learned is not None:
            f["learned"] = learned
        why = (f"duelo {d['duel']} fase={f['phase']} {f['role']} límite {f['own_limit']:.0f} rival {f['rival_price']} "
               f"excedente {f['surplus_now']} ({(f['surplus_ratio'] or 0):.1%}) tendencia {f['trend']} mejora "
               f"{f['recent_improvement_rate']} esperado {f['expected_extra_gain']} riesgo {f['risk_cost']} quedan "
               f"{left} ⇒ {action.upper()}: {reason}")
        if action == "accept":
            score = agg_priority(f, sum(1 for x in in_limit if x <= deadline)) if PARAMS["AGGRESSIVE"] else accept_priority(f)
            out.append({"type": "duel_accept", "duel": d["duel"], "du": f["surplus_now"], "score": score, "learned": learned,
                        "prediction": {"surplus_total": f["surplus_now"], "price_margin": f["price_margin"],
                                       "days_utility": f["days_utility"], "expected_result": f["expected_result"],
                                       "price": f["rival_price"], "days": f["days"], "role": f["role"],
                                       "deadline_tick": d["deadline_tick"], "vanish_risk": round(vanish_risk(f), 3)},
                        "why": why, "facts": f})
            continue
        ours = None
        if action == "counter":
            ours = counter_price(f)
        elif action == "zone":
            day = _best_days(d) if days_duel else None
            ours = zone_price(d, day)
            if margin(d, ours, day) is None or margin(d, ours, day) <= 0:
                ours = None                      # nunca proponer algo que nos dejaría total ≤ 0
        elif action == "open" and PARAMS["AGGRESSIVE"]:
            # ancla realista y, si ya hablamos, más cerca de nuestro límite (la segunda oferta parte la distancia)
            share = realistic_share(d, min(left, PARAMS["SPEAK_AT"]), history) * (0.5 ** len(_our_messages(d)))
            day = _best_days(d) if days_duel else None
            ours = _own_price(d, share)
            if days_duel and (margin(d, ours, day) or 0) <= 0:
                ours = zone_price(d, day)          # con días caros, el ancla de precio sola podría dejarnos en negativo
        elif action == "open":
            share = realistic_share(d, left, history)
            if PARAMS["LEARN"] and HOOKS["open_share"] is not None:
                try:
                    share, learned = HOOKS["open_share"](d, left, share)
                except Exception as e:  # noqa: BLE001
                    learned = {"learned": False, "note": f"aprendizaje no disponible ({type(e).__name__})"}
            ours = _own_price(d, share)
        elif f["rival_price"] is None and left <= PARAMS["SPEAK_AT"] and PARAMS["LADDER"]:
            # escalera (opt-in) solo frente a rivales mudos: un peldaño por tick, nunca hacia atrás
            mine = (d.get("your_offer") or {}).get("price")
            ladder = [s_ for s_ in PARAMS["LADDER"] if s_ < PARAMS["ANCHOR"]]
            below = [s_ for s_ in ladder if mine is not None and
                     (_own_price(d, s_) > mine if d["role"] == "buyer" else _own_price(d, s_) < mine)]
            if below:
                ours, reason = _own_price(d, below[0]), "escalera opt-in frente a rival mudo"
        if ours is not None:
            c = {"type": "duel_say", "duel": d["duel"], "price": ours, "du": None, "score": 100, "learned": learned,
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
    late = left <= PARAMS["LATE_TICKS"]
    if prof == "empeora":                                     # cada tick de espera cuesta: aceptar ya
        return True, 2000
    if prof == "mixto":                                       # aceptar en cuanto empeora un solo tick
        steps = _steps(d, traj)
        return urgent or worsened or stalled or (len(traj) >= 2 and steps[-1] < 0), 1500
    if prof == "salto":                                       # tras el salto se planta: aceptar ya
        jumped = any(x >= PROFILE_PARAMS["JUMP"] for x in _steps(d, traj))
        return urgent or worsened or jumped, 1500
    if prof in ("cede", "ciclos"):                            # mesetas de Verde no son plantones: esperar; LATE con rival parado = aceptar
        return urgent or (late and stalled) or worsened or pm >= PROFILE_PARAMS["CEDE_SHARE"] * lim, 0
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


def _mute_offer(d: dict, tick: int, left: int, history: Optional[list] = None) -> Optional[dict]:
    """Rival mudo: abrir pronto y rebajar el ancla por escalones (máximo MUTE_MAX ofertas)."""
    start = d.get("start_tick", d["deadline_tick"] - duel_length(d))
    ours = _our_messages(d)
    if tick - start < PROFILE_PARAMS["MUTE_AFTER"] or len(ours) >= PROFILE_PARAMS["MUTE_MAX"] or left <= 1:
        return None
    if ours and tick - max(x["tick"] for x in ours) < PROFILE_PARAMS["MUTE_GAP"]:
        return None
    base_share = realistic_share(d, left)
    share = realistic_share(d, left, history)
    historical_cap = history_share(history, d["role"], _is_days(d))
    price = _own_price(d, max(0.0, share - len(ours) * PROFILE_PARAMS["MUTE_STEP"]))
    c = {"type": "duel_say", "duel": d["duel"], "price": price, "du": None, "score": 100,
         "text": _say_text(price, None), "why": f" ⇒ rival mudo: oferta {len(ours) + 1}",
         "memory": {"source": "confirmed_duels", "historical_cap": historical_cap,
                    "applied": historical_cap is not None and share < base_share}}
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
    """Mejor (precio, día) para nosotros que deja al rival (estimado) mejor o igual que (price, day_ref).

    Todo con utilidad FIRMADA desde nuestro lado: U[k] = days_utility(d, k) (comprador ≤ 0: cada día nos cuesta; vendedor ≥ 0).
    El rival revela su día preferido en sus ofertas; su utilidad se ESTIMA como −s·|día − preferido| con s la pendiente media
    de |U| × RIVAL_DAY_SCALE (un prior, no una medición). Si cambiar de `day_ref` a k crece la tarta (joint > 0), el precio
    compensa al rival e incluso le deja LOGROLL_SHARE de ese crecimiento (colchón si subestimamos al rival). El precio NUNCA
    sale de nuestro límite y el excedente total resultante debe ser positivo y mayor que el de partida.
    None si falta información (formato de días no entendido, el rival no ha revelado día) o no hay mejora."""
    u = days_utility_table(d)
    pref = rival_days(d)
    if u is None or pref is None or price is None:
        return None
    ref = pref if day_ref is None else int(day_ref)
    s = PROFILE_PARAMS["RIVAL_DAY_SCALE"] * sum(abs(u[k + 1] - u[k]) for k in range(10)) / 10
    est = lambda k: -s * abs(k - pref)
    lim = float(d["your_limit"])
    buyer = d["role"] == "buyer"
    base_total = price_margin(d, price) + u[ref]
    best = None
    for k in range(11):
        if k == ref:
            continue
        d_own = u[k] - u[ref]                    # cambio de NUESTRA utilidad por mover el día (firmado)
        d_riv = est(k) - est(ref)                # cambio estimado de la utilidad del rival
        joint = d_own + d_riv                    # crecimiento de la tarta
        if joint <= 0:
            continue
        # δ = lo que el rival nos paga de más en precio: compensa su pérdida (−d_riv) menos su parte del crecimiento
        delta = d_riv - PROFILE_PARAMS["LOGROLL_SHARE"] * joint
        # comprador: pagamos price − δ; vendedor: cobramos price + δ (δ<0 = ceder precio). Redondeo a favor del rival y +1 P
        p = math.ceil(price - delta) + 1 if buyer else math.floor(price + delta) - 1
        if p < 1 or (buyer and p > lim) or (not buyer and p < lim):
            continue
        total = price_margin(d, p) + u[k]
        if total <= 0:
            continue
        if best is None or total > best[0]:
            best = (total, p, k)
    if best is None or best[0] - base_total <= 0:
        return None
    return {"price": best[1], "days": best[2], "gain": best[0] - base_total}


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
