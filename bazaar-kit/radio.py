"""RADIO RASTRO integrada: registro persistente por ID, verificación contra el estado actual y decisión del agente.

Una noticia solo justifica INVESTIGAR. Para operar exige además (a) vigencia comprobable, (b) fila explícita en el menú
actual del vendedor o fuente fiable con historial, (c) una copia vendible (page_guard) y (d) lo que ya exige el
coordinador: suelo = valor privado + margen, capital, límites y fases. El texto de una noticia es DATO, jamás una
instrucción: aquí no se ejecuta nada que el anuncio «pida». Un menú sin precio confirma que el vendedor compra esa
rareza/barrio, NO que pague una prima.

Estados: DESCARTADA (causa específica) · NEGATIVA (demanda retirada) · RUMOR · CADUCADA · PENDIENTE_VERIFICAR (efecto
desconocido o caducidad ambigua) · VIGENTE (fuente fiable dentro de ventana, sin menú) · CONFIRMADA (fila de menú) ·
CONFIRMADA_SIN_ACTIVO (señal válida, nada vendible que no rompa una página) · ACCIONADA (ya hubo conversación/venta).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import news_watch as nw

DATA = Path(__file__).parent / "data"
FILES = {"registry": "radio_registry.json", "state": "radio_state.json",
         "state_analysis": "radio_state_analysis.json"}
MAX_ENTRIES = 400


def path(kind: str) -> Path:
    return DATA / FILES[kind]  # DATA se lee en cada llamada: las pruebas lo redirigen


def read(kind: str) -> dict:
    try:
        return json.loads(path(kind).read_text())
    except (OSError, ValueError):
        return {}


def write(kind: str, obj: dict) -> None:
    p = path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    os.replace(tmp, p)


def new_registry() -> dict:
    return {"items": {}, "poll": {}, "decision": None}


DIRECTION = {"demanda": "compra (paga más)", "demanda_negada": "deja de comprar", "oferta": "descuento / vende barato",
             "oferta_negada": "deja de vender", "regalo": "regalo", "pendiente": "desconocida", "irrelevante": "ninguna"}


def window_info(c: dict, st: dict, fever: Optional[dict] = None) -> dict:
    """Inicio y fin con su certeza: «confirmado» (calendario del servidor), «estimado» (duración declarada contada desde
    la publicación) o «desconocido» (horario ambiguo o ausente: NO se inventa)."""
    if fever:
        return {"start": {"tick": fever.get("start_tick"), "certainty": "confirmado"},
                "end": {"tick": fever.get("end_tick"), "certainty": "confirmado" if fever.get("end_tick") else "desconocido"}}
    start = {"tick": c.get("tick"), "certainty": "estimado" if c.get("tick") is not None else "desconocido"}
    end = {"tick": st.get("until_tick"), "certainty": "estimado" if st.get("until_tick") is not None else "desconocido"}
    if st.get("time_ambiguous"):
        end["note"] = f"horario ambiguo «{st.get('time_text')}»"
    return {"start": start, "end": end}


def epistemic(c: dict, st: dict, status: str) -> str:
    """Separa: hecho observado en el servidor · anuncio pendiente de confirmación · rumor · hipótesis del agente."""
    if c.get("rumour"):
        return "rumor"
    if st.get("confirmed_by_menu"):
        return "hecho observado (menú del servidor)"
    if status in ("PENDIENTE_VERIFICAR", "VIGENTE", "CADUCADA"):
        return "anuncio pendiente de confirmación"
    return "hipótesis del agente" if status in ("CONFIRMADA_SIN_ACTIVO",) else "anuncio pendiente de confirmación"


# ------------------------------------------------------------------ verificación contra el estado actual

def assess(c: dict, st: dict, has_asset: Optional[bool] = None) -> tuple:
    """(estado, causas). `has_asset`: None = aún no se sabe; False = nada vendible; True = hay copia que puede salir."""
    kind = c["kind"]
    if kind == "irrelevante":
        return "DESCARTADA", [c.get("reason") or "sin efecto de mercado"]
    if kind in ("demanda_negada", "oferta_negada"):
        return "NEGATIVA", [c.get("reason") or "la demanda se retira"]
    if kind == "pendiente":
        return "PENDIENTE_VERIFICAR", [c.get("reason") or "efecto desconocido"]
    if c.get("rumour"):
        return "RUMOR", ["rumor del Tablón: no justifica abrir conversaciones ni comprar"]
    if kind == "regalo":
        return "PENDIENTE_VERIFICAR", ["regalo anunciado: se confirma solo con gift.given; no se gasta nada por él"]
    if st.get("expired") and not st.get("confirmed_by_menu"):
        return "CADUCADA", [f"ventana cerrada en t{st['until_tick']}"]
    if st.get("actionable"):
        if has_asset is False:
            return "CONFIRMADA_SIN_ACTIVO", ["señal válida pero ninguna copia se puede vender sin romper una página o "
                                              "una oferta abierta"]
        return ("CONFIRMADA" if st.get("confirmed_by_menu") else "VIGENTE"), []
    causes = nw.not_actionable_causes(c, st)
    if st.get("time_ambiguous") or st.get("thin_source") or not st.get("confirmed_by_menu"):
        return "PENDIENTE_VERIFICAR", causes
    return "PENDIENTE_VERIFICAR", causes


def buy_check(c: dict, dealer: Optional[dict], value: Optional[float] = None, margin: float = 2.0) -> dict:
    """¿Una noticia de oferta justifica COMPRAR? Exige precio ejecutable (list_price del menú) por debajo de nuestro valor
    privado menos margen. Un menú sin precio no confirma ninguna rebaja."""
    row = next((r for r in ((dealer or {}).get("menu") or {}).get("sells", [])
                if (c.get("rarity") is None or r.get("rarity") == c.get("rarity"))), None)
    price = None if row is None else row.get("list_price")
    if price is None:
        return {"executable": False, "reason": "menú sin precio confirmado: la rebaja anunciada no está verificada"}
    if value is None:
        return {"executable": False, "price": price, "reason": "precio visible pero sin valoración de la carta concreta"}
    ok = price <= value - margin
    return {"executable": ok, "price": price,
            "reason": "precio bajo valor privado − margen" if ok else f"{price} P no deja margen sobre el valor {value:.1f} P"}


# ------------------------------------------------------------------ registro por ID

def ingest(reg: dict, items: list, tick: Optional[int], tick_seconds: Optional[float], catalog: Optional[dict],
           ticks_by_id: Optional[dict], rel: dict, obs: Optional[dict], dealers: dict, source: str = "agent",
           now: Optional[float] = None) -> list:
    """Clasifica, persiste y verifica cada noticia (una vez por ID; se reevalúa su vigencia en cada sondeo).
    Devuelve las entradas NUEVAS de este sondeo."""
    now = time.time() if now is None else now
    new = []
    for raw in items:
        c = nw.classify(raw, catalog)
        if c.get("tick") is None and ticks_by_id and c.get("id") in ticks_by_id:
            c["tick"] = ticks_by_id[c["id"]]
        if c.get("id") is None:
            continue
        key = str(c["id"])
        st = nw.status_of(c, tick, tick_seconds, rel, (dealers or {}).get(c.get("dealer")), obs)
        status, causes = assess(c, st)
        e = reg["items"].get(key)
        if e is None:
            e = reg["items"][key] = {"id": c["id"], "source": c.get("source"), "source_name": c.get("source_name"),
                                     "tick": c.get("tick"), "first_seen_tick": tick, "first_seen_ts": round(now),
                                     "headline": raw.get("headline"), "body": raw.get("body"), "actions": [],
                                     "polled_by": source}
            new.append(e)
        e["interpretation"] = {k: c.get(k) for k in ("kind", "dealer", "set", "rarity", "item", "window_hours", "negated",
                                                     "time_ambiguous", "time_text", "rumour", "reason")}
        e["interpretation"]["direction"] = DIRECTION.get(c["kind"], "desconocida")
        e["valid_until_tick"] = st["until_tick"]
        e["window"] = window_info(c, st)
        prev = (e.get("verification") or {}).get("base_status")
        e["verification"] = {"status": "ACCIONADA" if e["actions"] else status, "base_status": status, "causes": causes,
                             "confirmed_by_menu": st["confirmed_by_menu"], "reliability": st["reliability"],
                             "observations": st["observations"], "thin_source": st["thin_source"],
                             "price_confirmed": False, "checked_tick": tick, "epistemic": epistemic(c, st, status)}
        if prev != status:
            e.setdefault("history", []).append({"tick": tick, "status": status})
            if prev is not None:
                e["changed_tick"] = tick
        if st["confirmed_by_menu"]:
            add_evidence(reg, c["id"], "for", f"menú actual de {c.get('dealer')} con fila explícita", tick,
                         f"menu:{c.get('dealer')}:{c.get('set')}:{c.get('rarity')}")
        elif c["kind"] == "demanda" and st["expired"]:
            add_evidence(reg, c["id"], "against", f"ventana cerrada en t{st['until_tick']} sin fila de menú", tick,
                         "expired")
    if len(reg["items"]) > MAX_ENTRIES:  # recorta las más antiguas; las que tienen acciones se conservan
        for k in sorted(reg["items"], key=lambda k: (bool(reg["items"][k]["actions"]), reg["items"][k].get("tick") or 0))[
                :len(reg["items"]) - MAX_ENTRIES]:
            reg["items"].pop(k, None)
    return new


def acted(reg: dict, news_id, dealer: Optional[str] = None) -> bool:
    """¿Ya se actuó por esta noticia (con ese vendedor)? Persiste entre reinicios: ningún anuncio dispara dos veces.
    Solo NUEVA evidencia a favor posterior a la última acción reabre la puerta (nunca la mera repetición del texto)."""
    e = (reg.get("items") or {}).get(str(news_id))
    if not e:
        return False
    mine = [a for a in e.get("actions", []) if dealer is None or a.get("dealer") == dealer]
    if not mine:
        return False
    last = max(int(a.get("tick") or 0) for a in mine)
    fresh = [x for x in e.get("evidence", []) if x["dir"] == "for" and int(x.get("tick") or 0) > last]
    return not fresh


def add_evidence(reg: dict, news_id, direction: str, text: str, tick: Optional[int], key: str) -> bool:
    """Evidencia a favor ('for') o en contra ('against'), una vez por clave (idempotente tras reiniciar)."""
    e = (reg.get("items") or {}).get(str(news_id))
    if e is None or any(x["key"] == key for x in e.setdefault("evidence", [])):
        return False
    e["evidence"].append({"dir": direction, "text": text, "tick": tick, "key": key})
    return True


def link(reg: dict, news_id, bucket: str, ref: dict, key: str) -> bool:
    """Vincula a la noticia una oportunidad / decisión / oferta / liquidación (sin duplicar por clave)."""
    e = (reg.get("items") or {}).get(str(news_id))
    if e is None:
        return False
    lst = e.setdefault("related", {}).setdefault(bucket, [])
    if any(x.get("key") == key for x in lst):
        return False
    lst.append(dict(ref, key=key))
    return True


def record_action(reg: dict, news_id, action: dict) -> bool:
    e = (reg.get("items") or {}).get(str(news_id))
    if e is None:
        return False
    if any(a.get("dealer") == action.get("dealer") and a.get("type") == action.get("type") for a in e["actions"]):
        return False
    e["actions"].append(action)
    if e.get("verification"):
        e["verification"]["status"] = "ACCIONADA"
    return True


# ------------------------------------------------------------------ estado para dashboard y monitor

def summary(reg: dict, tick: Optional[int], decision: Optional[dict], source: str, n_polled: int, new_ids: list,
            now: Optional[float] = None) -> dict:
    items = sorted(reg["items"].values(), key=lambda e: e.get("tick") or 0, reverse=True)
    counts: dict = {}
    for e in items:
        s = (e.get("verification") or {}).get("status", "?")
        counts[s] = counts.get(s, 0) + 1
    last = items[0] if items else None
    return {"generated": time.time() if now is None else now, "tick": tick, "source": source,
            "poll": {"tick": tick, "ts": time.time() if now is None else now, "n_items": n_polled, "new_ids": new_ids},
            "last_news": {k: last.get(k) for k in ("id", "source", "tick", "headline", "valid_until_tick")}
            | {"status": (last.get("verification") or {}).get("status"),
               "causes": (last.get("verification") or {}).get("causes")} if last else None,
            "decision": decision, "counts": counts,
            "recent": [{"id": e["id"], "tick": e.get("tick"), "source": e.get("source"), "headline": e.get("headline"),
                        "kind": (e.get("interpretation") or {}).get("kind"),
                        "status": (e.get("verification") or {}).get("status"),
                        "causes": (e.get("verification") or {}).get("causes"),
                        "valid_until_tick": e.get("valid_until_tick"), "actions": e.get("actions")} for e in items[:12]]}


def fresh_state(max_age: float, now: Optional[float] = None) -> Optional[dict]:
    """Estado escrito por el agente si es reciente: el monitor lo lee en vez de sondear la API otra vez."""
    st = read("state")
    if st and (time.time() if now is None else now) - float(st.get("generated") or 0) <= max_age and st.get("source") == "agent":
        return st
    return None


# ------------------------------------------------------------------ plan de negociación (A, B, C, D separadas)

class PlanConfig:
    """Parámetros del plan; ninguno es un % de subida/descuento supuesto."""
    def __init__(self, probe_counters: int = 2, probe_ticks: int = 6, min_ticks_left: int = 3,
                 spec_budget: int = 0, margin: float = 2.0):
        self.probe_counters, self.probe_ticks, self.min_ticks_left = probe_counters, probe_ticks, min_ticks_left
        self.spec_budget, self.margin = spec_budget, margin


def sale_plan(g: dict, baseline: Optional[dict], observed_final: Optional[int], cfg: PlanConfig, tick: int) -> dict:
    """Plan de venta motivado por una noticia, sobre una sugerencia `g` de news_watch.sell_suggestions.

    A  valor marginal privado de la carta (g.value): la noticia NO lo cambia.
    B  precio de RESERVA = max(A + margen, alternativa actual disponible): no se vende a la radio por menos de lo que
       ya ofrece la ruta habitual (`baseline.expected`, solo si está disponible ahora).
    C  precio OBJETIVO inicial = max(B, precio de lista del vendedor, final observado de ese vendedor).
    D  disposición a pagar estimada = SOLO el final observado en operaciones previas (None = desconocida). La noticia
       no la garantiza.
    El ritmo depende de la hipótesis: sin confirmar por una oferta, sonda corta (pocas contraofertas y plazo breve);
    confirmada por oferta, paciencia normal y prioridad de cierre."""
    A = float(g["value"])
    alt = baseline["expected"] if baseline and baseline.get("available") and baseline.get("expected") is not None else None
    B = max(int(g["floor"]), int(alt) if alt is not None else 0)
    # Objetivo inicial: nunca más tímido que la apertura de la ruta habitual (misma escalera, otra contraparte); sin
    # porcentajes inventados. D (final observado) lo eleva solo si hay muestra.
    C = max(B, int(g["ask"]), int(observed_final or 0), int((baseline or {}).get("open") or 0))
    blockers = []
    if observed_final is not None and alt is not None and observed_final < alt:
        blockers.append(f"no mejora la ruta habitual: {g['dealer']} ha cerrado a {observed_final} P frente a {alt} P "
                        f"con {baseline['dealer']}")
    until = g.get("until_tick")
    if until is not None and until - tick < cfg.min_ticks_left:
        blockers.append(f"ventana de la noticia casi cerrada (t{until}): no hay tiempo de negociar")
    return {"news_id": g.get("news_id"), "dealer": g["dealer"], "asset": g["asset"], "ref": g["ref"],
            "A_value": round(A, 2), "B_reserve": B, "C_target": C,
            "D_wtp": ({"value": int(observed_final), "basis": "final observado en operaciones anteriores"}
                      if observed_final is not None else None),
            "baseline": baseline, "mode": "dirigida" if observed_final is not None else "sonda",
            "probe_counters": cfg.probe_counters, "deadline_tick": tick + cfg.probe_ticks, "state": "sin_probar",
            "confirmed_by_menu": bool(g.get("confirmed_by_menu")), "blockers": blockers, "opened_tick": None,
            "why": (f"A={A:.1f} P (valor privado, sin cambio por la noticia) · B={B} P reserva"
                    + (f" (incluye la alternativa {baseline['dealer']} ≈ {alt} P)" if alt is not None else "")
                    + f" · C={C} P objetivo · D="
                    + (f"{observed_final} P observado" if observed_final is not None else "desconocida"))}


def hypothesis_after_bid(plan: dict, bid: Optional[int], turns: int) -> tuple:
    """(estado, motivo) de la hipótesis «este vendedor paga mejor» tras ver SU puja estructurada. Nunca se repite una
    oferta solo para comprobar un rumor: sin confirmación tras `probe_counters` contraofertas se da por no confirmada."""
    if plan["state"] in ("no_confirmada", "settled"):
        return plan["state"], plan.get("state_why", "")
    if bid is None:
        return "sin_probar", "aún sin puja"
    if bid >= plan["B_reserve"]:
        return "confirmada_por_oferta", f"su puja de {bid} P alcanza la reserva de {plan['B_reserve']} P"
    if turns >= plan["probe_counters"]:
        return "no_confirmada", (f"su puja de {bid} P sigue bajo la reserva de {plan['B_reserve']} P tras {turns} "
                                 "peticiones: se actualiza la hipótesis y no se insiste")
    return "sin_probar", f"puja de {bid} P bajo la reserva de {plan['B_reserve']} P; quedan peticiones de sonda"


# ------------------------------------------------------------------ compras motivadas por noticias

def resale_case(buy_price: float, exit_price: Optional[float], exit_confirmed: bool, buy_fee: float = 0.0,
                exit_fee: float = 0.0, source_rumour: bool = False, signal_confirmed: bool = False) -> dict:
    """Compra PARA REVENDER: hay que justificar entrada, salida, comisiones y riesgo. Comparar dos precios de venta
    no es arbitraje: sin una salida viva (oferta ejecutable de otro equipo) la salida no está asegurada."""
    if exit_price is None:
        return {"viable": False, "net": None, "exposure": buy_price + buy_fee, "reason": "sin precio de salida"}
    net = exit_price - exit_fee - buy_price - buy_fee
    return {"viable": net > 0 and exit_confirmed and signal_confirmed and not source_rumour, "net": round(net, 2),
            "exposure": round(buy_price + buy_fee, 2), "exit_assured": exit_confirmed,
            "reason": ("salida respaldada" if exit_confirmed else "salida NO asegurada (la demanda anunciada puede cambiar "
                                                                   "antes de vender)")
            + ("; rumor" if source_rumour else "") + ("" if signal_confirmed else "; señal sin confirmar")}


def resale_gate(case: dict, spec_budget: int, current_exposure: float = 0.0) -> list:
    """Bloqueos de una compra especulativa. Por defecto (`spec_budget` = 0) no se compra inventario por una noticia."""
    out = []
    if not case.get("viable"):
        out.append("compra para reventa no justificada: " + case.get("reason", ""))
    if spec_budget <= 0:
        out.append("presupuesto especulativo 0 P (--radio-spec-budget): no se compra inventario por una noticia")
    elif current_exposure + case.get("exposure", 0) > spec_budget:
        out.append(f"exposición {current_exposure + case.get('exposure', 0):.0f} P > límite especulativo {spec_budget} P")
    return out


# ------------------------------------------------------------------ aprendizaje (asociación, no causalidad)

def dealer_buy_prices(events: list, dealer: str, catalog: Optional[dict], set_id=None, rarity=None) -> list:
    """Liquidaciones públicas donde `dealer` COMPRA a un equipo (no ventas suyas), deduplicadas por id de liquidación
    (la misma operación vista por varias fuentes cuenta una vez), con rareza/barrio de la carta y precio NETO."""
    cards = {c["id"]: (s["id"], c.get("rarity")) for s in (catalog or {}).get("sets", []) for c in s.get("cards", [])}
    seen, out = set(), []
    for e in events or []:
        if e.get("type") != "settlement":
            continue
        p = e.get("payload") or {}
        sid = p.get("settlement") if p.get("settlement") is not None else p.get("id")
        if sid is None or sid in seen or p.get("persona") != dealer:
            continue
        items = p.get("items") or []
        if len(items) != 1 or items[0].get("to") != dealer or items[0].get("kind") != "card":
            continue
        ref = items[0].get("ref")
        sset, srar = cards.get(ref, (None, None))
        if (set_id and sset != set_id) or (rarity and srar != rarity):
            continue
        seen.add(sid)
        out.append({"settlement": sid, "tick": p.get("tick", e.get("tick")), "price": p.get("price") or 0,
                    "fee": p.get("fee") or 0, "ref": ref, "venue": p.get("venue")})
    return out


def association(reg_entry: dict, events: list, catalog: Optional[dict], min_n: int = 3) -> dict:
    """Compara precios de operaciones EQUIVALENTES (mismo vendedor comprando, misma rareza y barrio) antes y después de la
    noticia. Es una asociación temporal: nunca se atribuye causalidad a la radio."""
    it = reg_entry.get("interpretation") or {}
    if not it.get("dealer") or reg_entry.get("tick") is None or it.get("kind") not in ("demanda", "demanda_negada"):
        return {"comparable": False, "why": "la noticia no describe compras de un vendedor comparables"}
    rows = dealer_buy_prices(events, it["dealer"], catalog, it.get("set"), it.get("rarity"))
    t = reg_entry["tick"]
    before = [r["price"] - r["fee"] for r in rows if r["tick"] is not None and r["tick"] < t]
    after = [r["price"] - r["fee"] for r in rows if r["tick"] is not None and r["tick"] >= t]
    if len(before) < min_n or len(after) < min_n:
        return {"comparable": False, "n_before": len(before), "n_after": len(after),
                "why": f"muestra insuficiente (< {min_n} por lado): no se concluye nada"}
    med = lambda xs: sorted(xs)[len(xs) // 2]
    return {"comparable": True, "n_before": len(before), "n_after": len(after), "median_before": med(before),
            "median_after": med(after), "causal": "NO atribuible: asociación temporal, otras causas posibles"}


def reaction(reg: dict) -> list:
    """Noticias cuyo estado cambió en el último sondeo (confirmada, caducada, contradicha): el coordinador recalcula
    candidatas y revisa conversaciones afectadas. Una oferta rentable NO se abandona por caducar la noticia."""
    return [{"id": e["id"], "to": e["verification"]["base_status"], "tick": e.get("changed_tick")}
            for e in reg.get("items", {}).values() if e.get("changed_tick") is not None and
            e["verification"].get("checked_tick") == e.get("changed_tick")]
