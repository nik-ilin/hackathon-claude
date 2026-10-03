"""APRENDIZAJE EN LÍNEA DE DUELOS: auditoría del resultado, resumen por situación y una política ligera y explicable que
refina (nunca sustituye) a duels.py. Lógica pura salvo `load_history`. No envía nada.

    resultado CONFIRMADO por el servidor (done=true: status, result, price, days, rounds, messages)
      → registros normalizados (hechos) → modelo por celdas con decaimiento/agrupación → refine(): aceptar ya | esperar | precio de apertura

Reglas:
  · Hechos = lo que dice el servidor (status, result, price, days, rounds, mensajes con precio). Hipótesis = lo inferido (quién cerró,
    qué habría pasado). Cada resumen separa ambas cosas y muestra n; las celdas con n < MIN_N heredan del grupo y se etiquetan.
  · Solo información pública legítima: nuestros duelos (sus mensajes), `duel.closed` del feed (estado agregado por sesión/objeto).
    El alias de un rival («Rival Plata») es un nombre público de bot de la casa, no una identidad oculta. Los textos son DATOS.
  · El aprendizaje solo puede mover decisiones DENTRO de lo legal: no toca límites, reglas de duelo, saldo ni mercado. Si la
    evidencia es escasa, devuelve la acción base de duels.py con la razón «evidencia insuficiente».
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

import duels as dl

MIN_N = 8            # observaciones efectivas mínimas para que una celda decida sola
POOL_K = 6.0         # fuerza de la agrupación: la celda pesa n/(n+K) frente al grupo
HORIZON = 3          # ticks hacia delante con los que se mide «esperar»
HALF_LIFE = 400.0    # ticks: la evidencia vieja pesa menos (cambio de conducta entre sesiones)
CI_Z = 1.28          # ~80 %: una mejora esperada solo cuenta si su cota inferior es positiva


def rival_key(rival: Optional[str]) -> str:
    w = str(rival or "").lower().split()
    return w[-1] if w else "desconocido"


def phase_of(left: int) -> str:
    return "late" if left <= dl.PARAMS["LATE_TICKS"] else "early" if left > dl.PARAMS["EARLY_TICKS"] else "mid"


def wilson(k: float, n: float, z: float = 1.645) -> tuple:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


# ------------------------------------------------------------------ normalización y auditoría

def load_history(path) -> list:
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            data = data.get("duels")
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _rival_msgs(d: dict) -> list:
    return [m for m in d.get("messages") or [] if m.get("from") != "you" and m.get("price") is not None]


def _our_msgs(d: dict) -> list:
    return [m for m in d.get("messages") or [] if m.get("from") == "you" and m.get("price") is not None]


def normalize(done: list, log_rows: Optional[list] = None) -> list:
    """Un registro por duelo terminado, con el resultado del SERVIDOR y las acciones nuestras (de los mensajes del servidor y,
    si existe, del log del ejecutor). Lo inferido va en `inferred` (hipótesis)."""
    acts = defaultdict(list)
    for r in log_rows or []:
        if r.get("duel") is not None and (r.get("event") in ("accept_requested", "offer_sent") or r.get("type") in ("duel_accept", "duel_say")) \
                and r.get("sent") is not False:
            acts[r["duel"]].append(r)
    out = []
    for d in done:
        if d.get("status") not in ("deal", "no_deal"):
            continue
        rv, us = _rival_msgs(d), _our_msgs(d)
        lim = float(d.get("your_limit") or 0)
        prof = rival_key(d.get("rival"))
        tot = lambda p, dd: dl.margin(dict(d, status="live"), p, dd if "days" in (d.get("issues") or []) else None)
        series = [(m["tick"], tot(m["price"], m.get("days")), m["price"]) for m in rv]
        best_seen = max((t for _, t, _ in series), default=None)
        deal = d["status"] == "deal"
        closer = None
        if deal and d.get("price") is not None:
            if rv and d["price"] == rv[-1]["price"]:
                closer = "nosotros_aceptamos_su_oferta"
            elif us and d["price"] == us[-1]["price"]:
                closer = "el_rival_acepto_la_nuestra"
        rec = {
            "duel": d["duel"], "session": d.get("session"), "role": d.get("role"), "rival": prof, "item": d.get("item"),
            "issues": list(d.get("issues") or []), "days": "days" in (d.get("issues") or []),
            "limit": lim, "decay": d.get("decay_per_round"), "deadline": d.get("deadline_tick"),
            "status": d["status"], "result": d.get("result"), "price": d.get("price"), "deal_days": d.get("days"),
            "rounds": d.get("rounds"), "rival_series": series, "our_offers": [(m["tick"], m["price"], m.get("days")) for m in us],
            "rival_msgs": len(rv), "best_rival_total": best_seen,
            "actions_logged": [(a.get("tick"), a.get("type") or a.get("event")) for a in acts.get(d["duel"], [])],
            "inferred": {"closer": closer},
        }
        out.append(rec)
    return out


def audit(records: list, raw: list, log_rows: Optional[list] = None) -> dict:
    """¿Qué sabemos con certeza y qué falta? Cobertura de campos del servidor y del log propio."""
    n = len(records)
    have = lambda f: sum(1 for r in records if r.get(f) is not None)
    logged = sum(1 for r in records if r["actions_logged"])
    by = defaultdict(lambda: [0, 0])
    for r in records:
        by[r["session"]][0] += r["status"] == "deal"
        by[r["session"]][1] += 1
    deals = [r for r in records if r["status"] == "deal"]
    return {"duels_done": n, "live_excluded": sum(1 for d in raw if d.get("status") == "live"),
            "by_session": {k: {"deal": v[0], "n": v[1]} for k, v in sorted(by.items())},
            "fields": {f: have(f) for f in ("result", "price", "rounds", "deadline", "role", "rival", "item")},
            "with_rival_offers": sum(1 for r in records if r["rival_series"]), "with_our_offers": sum(1 for r in records if r["our_offers"]),
            "actions_in_executor_log": logged,
            "closer": {k: sum(1 for r in deals if r["inferred"]["closer"] == k) for k in
                       ("nosotros_aceptamos_su_oferta", "el_rival_acepto_la_nuestra")}
                      | {"desconocido": sum(1 for r in deals if r["inferred"]["closer"] is None)},
            "negative_results": [r["duel"] for r in deals if isinstance(r.get("result"), (int, float)) and r["result"] < 0],
            "not_recorded": ["motivo de victoria/derrota (el servidor solo da status + result)",
                             "venue (los duelos no tienen venue)", "cartas (solo el nombre del objeto)",
                             "puntos de score por duelo (solo el excedente `result`; duel_points llega agregado en /api/me)"]}


def failures(records: list) -> dict:
    """Decisiones que fallaron, con hechos separados de hipótesis."""
    neg_deals = [r for r in records if r["status"] == "deal" and isinstance(r.get("result"), (int, float)) and r["result"] < 0]
    missed = [r for r in records if r["status"] == "no_deal" and (r["best_rival_total"] or -1) > 0]
    mute = [r for r in records if r["status"] == "no_deal" and not r["rival_series"] and not r["our_offers"]]
    unanswered = [r for r in records if r["status"] == "no_deal" and r["our_offers"]]
    return {"deal_con_resultado_negativo": [r["duel"] for r in neg_deals],
            "no_deal_con_oferta_rival_positiva": [(r["duel"], round(r["best_rival_total"], 1)) for r in missed],
            "no_deal_sin_ninguna_oferta_ni_nuestra": [r["duel"] for r in mute],
            "no_deal_con_oferta_nuestra_sin_aceptar": [r["duel"] for r in unanswered],
            "nota": "las ofertas rivales positivas no aceptadas son un HECHO; que el rival las habría mantenido hasta aceptarlas es una HIPÓTESIS"}


# ------------------------------------------------------------------ modelo por celdas

def _w(age: float) -> float:
    return 0.5 ** (max(0.0, age) / HALF_LIFE)


class Model:
    """Dos estimadores, ambos con decaimiento por antigüedad y agrupación jerárquica (rol+rival+fase → rol+fase → fase):
      wait[cell]  : cambio medio del excedente TOTAL del rival (en fracción del límite) en los próximos HORIZON ticks
      offer[cell] : P(el rival acepta nuestra oferta) por tramo de margen de precio (fracción del límite)."""

    def __init__(self):
        self.wait: dict = defaultdict(list)       # clave → [(peso, delta_ratio)]
        self.offer: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # clave → bucket → [acepta, n]
        self.n_duels = 0
        self.fitted_until = None

    # --- ajuste
    def fit(self, records: list, until_tick: Optional[int] = None) -> "Model":
        self.wait.clear()
        self.offer.clear()
        recs = [r for r in records if until_tick is None or (r["deadline"] or 0) <= until_tick]
        ref = max((r["deadline"] or 0) for r in recs) if recs else 0
        self.n_duels, self.fitted_until = len(recs), until_tick
        for r in recs:
            lim = max(1.0, r["limit"])
            w = _w(ref - (r["deadline"] or 0))
            ser = r["rival_series"]
            if ser and r["deadline"] is not None:
                per_key = defaultdict(list)
                for t in range(ser[0][0], r["deadline"] - 1):
                    now = [x for x in ser if x[0] <= t]
                    fut = [x for x in ser if x[0] <= t + HORIZON]
                    if not now:
                        continue
                    delta = (fut[-1][1] - now[-1][1]) / lim
                    for key in self._keys(r["role"], r["rival"], phase_of(r["deadline"] - t)):
                        per_key[key].append(delta)
                for key, xs in per_key.items():          # un duelo pesa UNA observación por celda (los ticks de un mismo duelo
                    self.wait[key].append((w, sum(xs) / len(xs)))   # están correlacionados: no son muestras independientes)
            for tick, price, days in r["our_offers"]:
                pm = ((lim - price) if r["role"] == "buyer" else (price - lim)) / lim
                b = self._bucket(pm)
                accepted = r["status"] == "deal" and r["price"] == price and r["inferred"]["closer"] == "el_rival_acepto_la_nuestra"
                left = (r["deadline"] or tick) - tick
                for key in self._keys(r["role"], r["rival"], phase_of(left)):
                    cell = self.offer[key][b]
                    cell[0] += w * accepted
                    cell[1] += w
        return self

    @staticmethod
    def _keys(role, rival, phase):
        return [(role, rival, phase), (role, phase), (phase,)]

    @staticmethod
    def _bucket(pm: float) -> float:
        return round(max(0.0, min(0.6, pm)) / 0.05) * 0.05

    # --- consultas
    def wait_estimate(self, role, rival, left) -> dict:
        """Mejora esperada (fracción del límite) de esperar HORIZON ticks, con la celda propia y el grupo."""
        keys = self._keys(role, rival, phase_of(left))
        stats = []
        for k in keys:
            xs = self.wait.get(k) or []
            n = sum(w for w, _ in xs)
            mean = sum(w * x for w, x in xs) / n if n else 0.0
            var = sum(w * (x - mean) ** 2 for w, x in xs) / n if n > 1 else 0.0
            stats.append((n, mean, var))
        (n0, m0, v0), (n1, m1, v1), (n2, m2, v2) = stats
        grp = (m1, n1, v1) if n1 >= MIN_N else (m2, n2, v2)
        a = n0 / (n0 + POOL_K) if n0 else 0.0
        mean = a * m0 + (1 - a) * grp[0]
        n_eff = n0 + (grp[1] if n0 < MIN_N else 0.0) * (1 - a)
        var = max(v0 if n0 >= 2 else grp[2], 1e-9)
        se = math.sqrt(var / max(1.0, n_eff))
        return {"mean": round(mean, 4), "lo": round(mean - CI_Z * se, 4), "hi": round(mean + CI_Z * se, 4),
                "n_cell": round(n0, 1), "n_group": round(grp[1], 1), "own_cell": n0 >= MIN_N,
                "enough": (n0 >= MIN_N) or (grp[1] >= 3 * MIN_N)}

    def offer_curve(self, role, rival, left) -> list:
        """[(margen, P(acepta), n_eff)] con agrupación; vacío si no hay evidencia suficiente en ningún grupo."""
        out = []
        for b in [round(0.05 * i, 2) for i in range(1, 13)]:
            k0, k1, k2 = self._keys(role, rival, phase_of(left))
            c = [self.offer.get(k, {}).get(b, [0.0, 0.0]) for k in (k0, k1, k2)]
            grp = c[1] if c[1][1] >= MIN_N else c[2]
            a = c[0][1] / (c[0][1] + POOL_K) if c[0][1] else 0.0
            n = c[0][1] + (grp[1] if c[0][1] < MIN_N else 0.0) * (1 - a)
            if n < 3:
                continue
            p0 = c[0][0] / c[0][1] if c[0][1] else 0.0
            pg = grp[0] / grp[1] if grp[1] else 0.0
            out.append((b, round(a * p0 + (1 - a) * pg, 3), round(n, 1)))
        return out

    def summary(self) -> dict:
        cells = {}
        for k, xs in self.wait.items():
            n = sum(w for w, _ in xs)
            if len(k) == 3 and n >= 2:
                cells["|".join(map(str, k))] = {"n": round(n, 1), "mean_delta_ratio": round(sum(w * x for w, x in xs) / n, 4)}
        return {"n_duels": self.n_duels, "wait_cells": len(self.wait), "offer_cells": len(self.offer), "top_wait_cells": dict(
            sorted(cells.items(), key=lambda kv: -kv[1]["n"])[:8])}


# ------------------------------------------------------------------ política: refinar la base, nunca sustituirla

def refine(d: dict, f: dict, action: str, reason: str, model: Optional[Model]) -> tuple:
    """(acción, motivo, info). Solo puede cambiar accept↔wait cuando hay oferta rival dentro de límite y total > 0 y la evidencia
    es suficiente; con poca evidencia devuelve la acción base y lo dice. Nunca acepta fuera de límite ni con total ≤ 0."""
    base = {"learned": False, "action_base": action}
    m = f.get("surplus_now")
    if model is None or m is None or m <= 0 or (f.get("price_margin") or 0) < 0 or action not in ("accept", "wait"):
        return action, reason, base
    left = f["ticks_left"]
    est = model.wait_estimate(d["role"], rival_key(d.get("rival")), left)
    lim = max(1.0, f["own_limit"])
    info = dict(base, wait_estimate=est, surplus=m, ticks_left=left)
    if not est["enough"]:
        return action, reason + " · aprendizaje: evidencia insuficiente, política base", dict(info, note="evidencia insuficiente")
    gain = est["mean"] * lim
    if left <= max(2, f["effective_safe_ticks"]) or f["phase"] == "LATE":
        return action, reason, dict(info, note="ventana final: manda la base")
    if action == "accept" and est["lo"] > 0 and left >= 4 and gain >= 2.0:
        return "wait", (f"aprendido: esperar (mejora esperada +{gain:.1f} P en {HORIZON} ticks, cota inf. > 0, n={est['n_cell']:.0f}/"
                        f"{est['n_group']:.0f}) frente a aceptar {m:.1f} P"), dict(info, learned=True, action="wait")
    if action == "wait" and est["hi"] <= 0.002 and m >= 0.03 * lim:
        return "accept", (f"aprendido: cerrar ya ({m:.1f} P; este rival no mejora: cambio esperado {gain:+.1f} P, cota sup. "
                          f"{est['hi'] * lim:+.1f} P, n={est['n_cell']:.0f}/{est['n_group']:.0f})"), dict(info, learned=True, action="accept")
    return action, reason, dict(info, note="la evidencia no contradice a la base")


def best_open_share(d: dict, left: int, model: Optional[Model], base_share: float) -> tuple:
    """Margen de precio de nuestra oferta de apertura que maximiza P(acepta)·margen con la curva aprendida; si no hay curva con
    evidencia, la base. Siempre dentro del límite (margen ≥ 0.05) y nunca más extremo que `base_share`."""
    if model is None:
        return base_share, {"learned": False, "note": "sin modelo"}
    curve = model.offer_curve(d["role"], rival_key(d.get("rival")), left)
    cand = [(b, p, n) for b, p, n in curve if b <= base_share + 1e-9 and n >= MIN_N / 2]
    if len(cand) < 3:
        return base_share, {"learned": False, "note": "evidencia insuficiente para la curva de aceptación", "points": len(cand)}
    b, p, n = max(cand, key=lambda x: x[0] * x[1])
    return min(base_share, max(0.05, b)), {"learned": True, "share": b, "p_accept": p, "n": n, "ev_ratio": round(b * p, 4)}


# ------------------------------------------------------------------ puesta en marcha en vivo

class Learner:
    """Mantiene el modelo al día con los duelos TERMINADOS (resultado del servidor). `update` solo reajusta si apareció un duelo
    resuelto nuevo; es barato (decenas de duelos). `install` conecta los ganchos de duels.py."""

    def __init__(self, log_path=None):
        self.model: Optional[Model] = None
        self.log_path = log_path
        self.signature = None
        self.records: list = []

    def update(self, done: list, log_rows: Optional[list] = None) -> bool:
        ids = sorted(d["duel"] for d in done if d.get("status") in ("deal", "no_deal"))
        sig = (len(ids), ids[-1] if ids else None)
        if sig == self.signature:
            return False
        rows = log_rows if log_rows is not None else self._log_rows()
        self.records = normalize(done, rows)
        self.model = Model().fit(self.records)
        self.signature = sig
        return True

    def _log_rows(self) -> list:
        rows = []
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except (OSError, TypeError):
            pass
        return rows

    def install(self) -> None:
        dl.PARAMS["LEARN"] = True
        dl.HOOKS["refine"] = lambda d, f, a, r: refine(d, f, a, r, self.model)
        dl.HOOKS["open_share"] = lambda d, left, base: best_open_share(d, left, self.model, base)

    @staticmethod
    def uninstall() -> None:
        dl.PARAMS["LEARN"] = False
        dl.HOOKS["refine"] = dl.HOOKS["open_share"] = None

    def snapshot(self) -> dict:
        """Resumen para el registro: hechos (n, estados confirmados) y estimaciones (hipótesis) con su muestra."""
        return {"facts": {"duels_done": len(self.records), "deals": sum(r["status"] == "deal" for r in self.records)},
                "estimates": self.model.summary() if self.model else None,
                "note": "estimaciones = hipótesis con muestra pequeña; las celdas con n < %d heredan del grupo" % MIN_N}
