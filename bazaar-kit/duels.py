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
Duelos de precio + días: omitidos por duel_candidates hasta verificar la fórmula del servidor.
_days_value y margin conservan una aproximación exploratoria para análisis offline.

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
}


def _days_value(duel: dict, days: Optional[int]) -> float:
    """Utilidad (en primas) del día de entrega, si el duelo negocia días. El formato de `your_days_weight` no está
    documentado: se aceptan lista por día, dict por día o peso escalar por día."""
    w = duel.get("your_days_weight")
    if days is None or not w:
        return 0.0
    if isinstance(w, dict):
        return float(w.get(str(days), w.get(days, 0)) or 0)
    if isinstance(w, list) and 0 <= days < len(w):
        return float(w[days])
    if isinstance(w, (int, float)):
        return float(w) * days
    return 0.0


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
    return max(range(11), key=lambda k: _days_value(duel, k))


def duel_candidates(duels: list, tick: int) -> list:
    """Candidatas para el tick, con el mismo espíritu que las del coordinador: dicts con `type`, `duel`, `score`, `du`
    (excedente) y `why`. type ∈ {"duel_accept", "duel_say"}. Ordenar por `score` y enviar como mucho UNA aceptación."""
    by_deadline: dict = {}
    for d in duels:
        if d.get("status") == "live":
            by_deadline[d.get("deadline_tick")] = by_deadline.get(d.get("deadline_tick"), 0) + 1
    out = []
    for d in duels:
        if d.get("status") != "live" or d.get("deadline_tick") is None:
            continue
        deadline = d["deadline_tick"]
        left = deadline - tick
        if left <= 0:
            continue  # no enviar acciones sobre un snapshot caducado
        if "days" in (d.get("issues") or []):
            continue  # pendiente de verificar la fórmula de utilidad de días con el servidor
        ro = d.get("rival_offer") or {}
        price, days = ro.get("price"), ro.get("days")
        m = margin(d, price, days)
        lim = float(d["your_limit"])
        traj = rival_trajectory(d)
        slope = 0.0                                   # mejora del rival a nuestro favor, primas/tick (últimos 3 ticks)
        if len(traj) >= 2:
            (t0, p0), (t1, p1) = traj[max(0, len(traj) - 4)], traj[-1]
            if t1 > t0:
                slope = ((p0 - p1) if d["role"] == "buyer" else (p1 - p0)) / (t1 - t0)
        n = PARAMS["STALL_TICKS"]
        stalled = len(traj) >= n and all(p == traj[-1][1] for _, p in traj[-n:])
        safe = PARAMS["SAFE_TICKS"] + max(0, by_deadline.get(deadline, 1) - 1)
        why = f"duelo {d['duel']} {d['role']} límite {lim:.0f} rival {price} días {days} quedan {left} pendiente {slope:.1f}"

        if m is not None and m >= 0:
            if left <= safe or m >= PARAMS["GOOD_SHARE"] * lim or slope < 0 or stalled:
                # urgencia primero; entre urgentes, antes el rival que menos cede (el que más cede, al final)
                score = (1000 if left <= safe + 1 else 500) + max(0, 10 - int(slope))
                out.append({"type": "duel_accept", "duel": d["duel"], "du": m, "score": score, "why": why + " ⇒ aceptar"})
                continue
        if (price is None or (m is not None and m < 0)) and left <= PARAMS["SPEAK_AT"] and d.get("your_offer") is None:
            a = PARAMS["ANCHOR"]
            ours = int(round(lim * (1 - a))) if d["role"] == "buyer" else int(round(lim * (1 + a)))
            c = {"type": "duel_say", "duel": d["duel"], "price": ours, "du": None, "score": 100,
                 "text": f"{ours} P y cerramos ahora.", "why": why + " ⇒ una oferta propia"}
            if "days" in (d.get("issues") or []):
                c["days"] = _best_days(d)
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
