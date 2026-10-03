"""Duelos: política de «silencio estratégico» con aceptación escalonada. Lógica pura, sin red.

Qué puntúa (RULES.md): en cada duelo, la parte de la tarta capturada (tarta = valor del comprador − coste del
vendedor). Cerrar fuera del propio límite resta, no cerrar da 0 y la tarta encoge un `decay_per_round` (6–10 %) con
cada ronda de conversación.

Evidencia (sesión de práctica del viernes, 24 duelos propios, `duels_fixture_practice.json`):
  * la mayoría de rivales CEDE CADA TICK SIN NECESITAR RESPUESTA (p. ej. 122 → 82 en 12 ticks, con nuestro límite 158);
  * `rounds` quedó en 0 aunque el rival enviara 12 mensajes: callar no encoge la tarta;
  * dos rivales hicieron una primera oferta «explosiva» que después empeoró;
  * ~25 % de los rivales no habló nunca.

Política por duelo vivo, en cada tick:
  1. Oferta rival dentro de límite y margen ≥ GOOD_SHARE × límite, o el rival empeora, o se planta STALL_TICKS ticks
     ⇒ aceptar.
  2. El rival sigue cediendo ⇒ silencio (hablar cuesta decay).
  3. Sin oferta rival a falta de SPEAK_AT ticks ⇒ UNA oferta propia (comprador 0,70·L, vendedor 1,30·C).
  4. Últimos ticks ⇒ aceptar la mejor oferta dentro de límite. Solo hay UNA aceptación por tick y equipo: los duelos
     que comparten deadline se escalonan (un tick de margen más por cada duelo de la oleada).
Duelos de precio + días: omitidos por defecto hasta verificar la fórmula del servidor. Con PARAMS["PLAY_DAYS"] = True
(opt-in, `duel_runner.py --days`) se juegan con la misma política, valorando cada oferta como precio + utilidad de
días, exigiendo SIEMPRE precio dentro de límite y enviando `days` en toda oferta propia.
Opt-in PARAMS["PROFILES"] (`--profiles`) y PARAMS["LOGROLL"] (`--logroll`): ver la sección de perfiles más abajo.
Opt-in PARAMS["LADDER"]: frente a un rival que no habla, en vez de una sola oferta, una escalera de ofertas que se
acerca al límite (por defecto desactivada: una sola oferta).

Repetición individual retrospectiva (sin límite compartido de aceptaciones ni validación fuera de muestra): 98 % del margen máximo disponible (562 de 574 P) con los parámetros por defecto, frente
a 77 % aceptando en cuanto hay un 30 % de margen. El viernes, sin módulo de duelos, se capturó 0.
"""
from __future__ import annotations

import math
from typing import Optional

PARAMS = {
    "GOOD_SHARE": 0.60,   # aceptar ya si el margen ≥ 60 % del límite (búsqueda en rejilla sobre la práctica)
    "STALL_TICKS": 3,     # rival sin moverse tantos ticks ⇒ aceptar si está dentro de límite
    "SPEAK_AT": 5,        # ticks antes del deadline para hablar si el rival calla
    "ANCHOR": 0.30,       # nuestra única oferta: comprador límite·(1−0,30), vendedor límite·(1+0,30)
    "SAFE_TICKS": 1,      # aceptar a más tardar en deadline − SAFE_TICKS (+1 por duelo de la misma oleada)
    # --- opt-in (desactivados por defecto; ver duel_sim.py para su medición) ---
    "PLAY_DAYS": False,        # jugar duelos de precio + días (si no, se omiten)
    "LADDER": (),         # rival mudo: márgenes sucesivos tras ANCHOR, p. ej. (0.20, 0.12, 0.06); () = una sola oferta
    "PROBE": False,       # rival plantado con tiempo de sobra: UNA contraoferta a mitad de camino antes de aceptar
    "PROFILES": False,    # reglas por perfil del bot de la casa (Plata, Verde, Oro, Luna, Rojo, Noche) y mudos pronto
    "LOGROLL": False,     # días: conceder los que nos cuestan poco a cambio de precio (necesita PLAY_DAYS)
}

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


def duel_candidates(duels: list, tick: int) -> list:
    """Candidatas para el tick, con el mismo espíritu que las del coordinador: dicts con `type`, `duel`, `score`, `du`
    (excedente) y `why`. type ∈ {"duel_accept", "duel_say"}. Ordenar por `score` y enviar como mucho UNA aceptación.
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
        ro = d.get("rival_offer") or {}
        price = ro.get("price")
        days = ro.get("days") if days_duel else None
        m = margin(d, price, days)
        pm = margin(dict(d, your_days_weight=None), price)  # solo precio: el umbral GOOD_SHARE no cuenta los días
        lim = float(d["your_limit"])
        traj = rival_trajectory(d)
        rival = d.get("rival")
        dtraj = [m_.get("days") for m_ in d.get("messages", []) if m_.get("price") is not None and m_.get("from") == rival]
        util = [margin(d, p, (dtraj[i] if days_duel else None)) for i, (_, p) in enumerate(traj)]
        slope = 0.0                                   # mejora del rival a nuestro favor, primas/tick (últimos 3 ticks)
        if len(traj) >= 2:
            i0 = max(0, len(traj) - 4)
            (t0, _), (t1, _) = traj[i0], traj[-1]
            if t1 > t0:
                slope = (util[-1] - util[i0]) / (t1 - t0)
        n = PARAMS["STALL_TICKS"]
        offers = [(p, dtraj[i] if days_duel else None) for i, (_, p) in enumerate(traj)]
        stalled = len(offers) >= n and all(o == offers[-1] for o in offers[-n:])
        # una aceptación por tick: los duelos con deadline ≤ el nuestro y oferta aceptable compiten por los mismos ticks
        queue = sum(1 for x in in_limit if x <= deadline)
        safe = PARAMS["SAFE_TICKS"] + max(0, by_deadline.get(deadline, 1) - 1, queue - 1)
        why = f"duelo {d['duel']} {d['role']} límite {lim:.0f} rival {price} días {days} quedan {left} pendiente {slope:.1f}"
        prof = rival_profile(d) if PARAMS["PROFILES"] else None
        if prof:
            why += f" perfil {prof}"

        if m is not None and m >= 0 and pm is not None and pm >= 0:
            probe = (PARAMS["PROBE"] and stalled and slope >= 0 and d.get("your_offer") is None
                     and left > safe + 2 and pm < PARAMS["GOOD_SHARE"] * lim)
            if probe:
                # opt-in: un rival que se planta puede ser un «espejo» que solo se mueve si nos movemos
                ours = int(round((price + _own_price(d, PARAMS["ANCHOR"])) / 2))
                c = {"type": "duel_say", "duel": d["duel"], "price": ours, "du": None, "score": 100,
                     "text": f"{ours} P y cerramos ahora.", "why": why + " ⇒ sondeo a rival plantado"}
                if days_duel:
                    c["days"] = _best_days(d)
                out.append(c)
                continue
            if (PARAMS["LOGROLL"] and days_duel and prof != "empeora" and left > safe + 1
                    and not _our_messages(d)):
                lr = logroll_offer(d, price, days)
                if lr and lr["gain"] >= PROFILE_PARAMS["LOGROLL_MIN_GAIN"] + _decay(d) * (m + lr["gain"]):
                    out.append({"type": "duel_say", "duel": d["duel"], "price": lr["price"], "days": lr["days"],
                                "du": m + lr["gain"], "score": 300, "text": _say_text(lr["price"], lr["days"]),
                                "why": why + f" ⇒ logroll: día {lr['days']} por precio (+{lr['gain']:.1f} P)"})
                    continue
            if prof:
                accept, pscore = _profile_rule(prof, d, pm, lim, traj, slope, stalled, left, safe)
            else:
                accept, pscore = left <= safe or pm >= PARAMS["GOOD_SHARE"] * lim or slope < 0 or stalled, 0
            if accept:
                # urgencia primero (antes el deadline más próximo); entre iguales, antes el rival que menos cede
                score = (1000 + 10 * max(0, 50 - left) if left <= safe + 1 else 500) + max(0, 10 - int(slope))
                score = max(score, pscore)
                out.append({"type": "duel_accept", "duel": d["duel"], "du": m, "score": score, "why": why + " ⇒ aceptar"})
                continue
        if prof and price is None and not traj:
            c = _mute_offer(d, tick, left)
            if c:
                c["why"] = why + c["why"]
                out.append(c)
            continue
        if (price is None or (m is not None and m < 0)) and left <= PARAMS["SPEAK_AT"]:
            ladder = [PARAMS["ANCHOR"]] + [s_ for s_ in PARAMS["LADDER"] if s_ < PARAMS["ANCHOR"]]
            mine = (d.get("your_offer") or {}).get("price")
            if mine is None:
                share = ladder[0]
            elif price is None and len(ladder) > 1:
                # escalera (opt-in) solo frente a rivales mudos: un peldaño por tick, nunca hacia atrás
                below = [s_ for s_ in ladder if (_own_price(d, s_) > mine if d["role"] == "buyer" else _own_price(d, s_) < mine)]
                if not below:
                    continue
                share = below[0]
            else:
                continue
            ours = _own_price(d, share)
            c = {"type": "duel_say", "duel": d["duel"], "price": ours, "du": None, "score": 100,
                 "text": f"{ours} P y cerramos ahora.", "why": why + " ⇒ una oferta propia"}
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
