"""Comparación explícita CERRAR AHORA frente a ESPERAR para un activo excedente. Lógica pura, sin red.

    A  aceptar la mejor oferta ejecutable      B  contraofertar / proponer al objetivo
    C  buscar otra contraparte                 D  mantener el activo

Principios (información incompleta):
  · Cada opción se expresa como RANGO de excedente económico (neto − pérdida marginal) con una CONFIANZA (baja/media/alta);
    no se inventan probabilidades de ejecución. El efectivo (neto cobrado) y los puntos son magnitudes distintas: aquí solo
    se compara beneficio económico y efectivo; los puntos del juego no se estiman.
  · Una oferta EJECUTABLE y válida no se penaliza por una alternativa hipotética. Solo una alternativa con EVIDENCIA
    (puja vigente, o ≥3 cierres comparables recientes con cota inferior por encima) que supere la oferta por más de
    `min_step` P y con tiempo para intentarla justifica esperar.
  · El silencio, la cancelación o la caducidad de una propuesta nuestra no son un rechazo del precio.
  · No se prolonga una negociación por incrementos menores que `min_step` sin evidencia de mejora.
  · Con urgencia de caja (w > 0 en transición/tesorería), plazo agotado o una oferta a punto de caducar, se cierra.
"""
from __future__ import annotations

from typing import Optional

MIN_COMPARABLE = 3        # cierres comparables mínimos para tratarlos como evidencia (no como anécdota)


def confidence(n: int, executable: bool) -> str:
    if executable:
        return "alta"
    return "media" if n >= MIN_COMPARABLE else "baja"


def alternatives(ev: dict, selected_offer=None, tick: int = 0) -> list:
    """Alternativas VISIBLES por activo (nunca compradores inventados): pujas vigentes distintas de la elegida, rango de
    cierres comparables y compradores con puja reciente. Cada una indica si es ejecutable y su antigüedad."""
    rows = []
    for b in ev.get("bids", []):
        if b["offer"] != selected_offer:
            rows.append({"kind": "puja vigente", "team": b["team"], "net_lo": b["net"], "net_hi": b["net"], "executable": True,
                         "confidence": "alta", "age": None, "expires": b.get("expires"), "offer": b["offer"]})
    closes = sorted(c["price"] for c in ev.get("closes", []))
    if closes:
        rows.append({"kind": "cierres comparables", "team": None, "net_lo": closes[0], "net_hi": closes[-1], "n": len(closes),
                     "executable": False, "confidence": confidence(len(closes), False),
                     "age": (tick - max(c["tick"] for c in ev["closes"])) if tick else None})
    for b in ev.get("recent_buyers", []):
        rows.append({"kind": "comprador con puja reciente", "team": b["team"], "net_lo": None, "net_hi": None, "executable": False,
                     "confidence": "baja", "age": (tick - b["last_bid_tick"]) if tick else None})
    return rows


def compare(*, loss: float, minimum: int, objective: int, best: Optional[dict], alts: list, ticks_left: Optional[int],
            w: float = 0.0, deadline: bool = False, own_offer: bool = False, min_step: int = 2) -> dict:
    """Devuelve {choice, action, reason, confidence, options}. `best` = {net, expires_in} de la mejor oferta ejecutable
    válida (net ≥ minimum) o None."""
    options = []
    if best is not None:
        options.append({"code": "A", "label": "aceptar la mejor oferta ejecutable", "cash_now": best["net"],
                        "surplus": [round(best["net"] - loss, 2)] * 2, "confidence": "alta",
                        "note": "oferta verificable; se revalida contra el servidor antes de enviar"})
    better = [a for a in alts if a.get("net_lo") is not None and a["confidence"] in ("media", "alta") and
              (best is None or a["net_lo"] > best["net"] + min_step)]
    c_lo = min((a["net_lo"] for a in alts if a.get("net_lo") is not None), default=None)
    c_hi = max((a["net_hi"] for a in alts if a.get("net_hi") is not None), default=None)
    options.append({"code": "B", "label": "contraofertar / proponer al objetivo", "cash_now": 0,
                    "surplus": [0.0, round(objective - loss, 2)],
                    "confidence": "baja", "note": f"hasta {objective} P si responde; puede no responder (no es un rechazo)"})
    options.append({"code": "C", "label": "otra contraparte", "cash_now": 0,
                    "surplus": [round(c_lo - loss, 2), round(c_hi - loss, 2)] if c_lo is not None else None,
                    "confidence": max((a["confidence"] for a in alts), key=("baja", "media", "alta").index) if alts else "baja",
                    "note": "solo alternativas con evidencia visible" if alts else "sin alternativas visibles: no se inventan"})
    hold_cost = "caja necesaria antes del cierre" if w > 0 else "capital inmovilizado sin urgencia"
    options.append({"code": "D", "label": "mantener el activo", "cash_now": 0, "surplus": [0.0, 0.0], "confidence": "alta",
                    "note": hold_cost})

    def out(choice, action, reason, conf):
        return {"choice": choice, "action": action, "reason": reason, "confidence": conf, "options": options}

    if best is not None:
        net = best["net"]
        exp = best.get("expires_in")
        if net >= objective:
            return out("A", "accept", f"{net} netos ≥ objetivo {objective} P: cerrar sin esperar", "alta")
        if w > 0:
            return out("A", "accept", f"{net} netos entre mínimo {minimum} P y objetivo {objective} P y hay urgencia de caja "
                                      f"(w={w:.2f}): se cobra antes", "alta")
        if deadline:
            return out("A", "accept", f"{net} netos ≥ mínimo {minimum} P y presupuesto de ticks agotado", "alta")
        if exp is not None and exp <= 1:
            return out("A", "accept", f"{net} netos ≥ mínimo y la oferta caduca en {exp} tick(s)", "alta")
        if better and (ticks_left is None or ticks_left >= 2):
            a = max(better, key=lambda x: x["net_lo"])
            return out("B", "wait_list", f"{net} netos ≥ mínimo, pero hay evidencia de mejora ({a['kind']}: ≥ {a['net_lo']} P, "
                                         f"confianza {a['confidence']}, n={a.get('n', 1)}) por más de {min_step} P y hay tiempo", a["confidence"])
        return out("A", "accept", f"{net} netos ≥ mínimo {minimum} P sin evidencia de mejora probable "
                                  f"(no se prolonga por menos de {min_step} P ni por alternativas hipotéticas)", "media")
    if own_offer:
        return out("D", "hold", "propuesta nuestra en pie: se espera sin repetir mensajes", "media")
    return out("B", "list", f"sin oferta ejecutable válida: propuesta concreta al objetivo {objective} P (mínimo {minimum} P)",
               "baja")
