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
Opt-in PARAMS["LADDER"]: frente a un rival que no habla, en vez de una sola oferta, una escalera de ofertas que se
acerca al límite (por defecto desactivada: una sola oferta).

Repetición individual retrospectiva (sin límite compartido de aceptaciones ni validación fuera de muestra): 98 % del margen máximo disponible (562 de 574 P) con los parámetros por defecto, frente
a 77 % aceptando en cuanto hay un 30 % de margen. El viernes, sin módulo de duelos, se capturó 0.
"""
from __future__ import annotations

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
            if left <= safe or pm >= PARAMS["GOOD_SHARE"] * lim or slope < 0 or stalled:
                # urgencia primero (antes el deadline más próximo); entre iguales, antes el rival que menos cede
                score = (1000 + 10 * max(0, 50 - left) if left <= safe + 1 else 500) + max(0, 10 - int(slope))
                out.append({"type": "duel_accept", "duel": d["duel"], "du": m, "score": score, "why": why + " ⇒ aceptar"})
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
