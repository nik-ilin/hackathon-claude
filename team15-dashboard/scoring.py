"""Las cuatro cosas que el panel tenía delante y no mostraba.

Lógica pura: sin red, sin escrituras, biblioteca estándar. `app.py` ya pedía
`GET /api/me` y guardaba `me["score"]` en `data["score"]`, pero `strategy_export()`
sólo sacaba de ahí el total y descartaba el desglose; `coordinator.py:71` y
`market_agent.py:508` sí lo leen. Sin el desglose no se puede saber si falta
escalera o faltan duelos, que son decisiones distintas.

Cuatro bloques:

- `scoring_block`  el desglose de /api/me y la distancia por componente a los líderes.
- `ladder_block`   el contador de casillas: la escalera paga los 3 mejores tratos de
                   cada nivel y una casilla vacía cuenta CERO, así que lo que importa
                   no es cuántos tratos hay, sino cuántos huecos quedan y en qué nivel.
- `feed_health`    antigüedad del almacén del feed. Todo lo que cuelga de `feed_oracle`
                   (precios, rivales, playbook) razona sobre este fichero, y si está
                   parado el panel sigue dando cifras sin avisar.
- `peers_block`    qué variables separan de verdad a los equipos de cabeza, medido,
                   y en qué arquetipo cae cada uno.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Iterable, Optional

SCORE_FIELDS = ("score", "negotiating", "market", "neg_points", "ladder_points",
                "duel_points", "mm_points", "deals", "rank")

# La escalera cuenta los 3 mejores tratos de cada nivel (RULES.md, «The dealers' ladder»).
SLOTS_PER_LEVEL = 3


def _nums(rows: Iterable[dict], key: str) -> list[float]:
    return [r[key] for r in rows if isinstance(r.get(key), (int, float))]


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return round(num / den, 3) if den else None


def scoring_block(score: dict, leaderboard: dict, team: str = "t15") -> dict:
    """Desglose propio y distancia por componente al mejor de cada componente.

    Separar las distancias importa: subir `negotiating` de 16 a 21 y subir `market`
    de 7.5 a 12.5 valen casi lo mismo en total, pero una no necesita capital y la
    otra cuesta 270 P de fianza."""
    score = score or {}
    out = {k: score.get(k) for k in SCORE_FIELDS}
    rows = [r for r in (leaderboard or {}).get("teams") or [] if isinstance(r.get("score"), (int, float))]
    active = [r for r in rows if (r.get("score") or 0) > 8]
    mine = next((r for r in rows if r.get("team") == team), None)
    components = {}
    for part in ("negotiating", "market"):
        values = _nums(active, part)
        if not values or not mine or not isinstance(mine.get(part), (int, float)):
            continue
        best = max(values)
        leader = next((r["team"] for r in active if r.get(part) == best), None)
        components[part] = {
            "ours": mine[part], "best": round(best, 2), "best_team": leader,
            "gap_to_best": round(best - mine[part], 2),
            "median": round(statistics.median(values), 2),
            "teams_above": sum(1 for v in values if v > mine[part]),
            "weight_in_total": 30.0,
        }
    out["components"] = components
    out["missing_from_api"] = [k for k in ("ladder_points", "duel_points", "neg_points")
                               if score.get(k) is None]
    out["note"] = ("ladder_points y duel_points sólo llegan con X-Team-Key; sin ellos no se distingue "
                   "«la escalera está vacía» de «no jugamos duelos», que piden acciones distintas.")
    return out


def ladder_block(dealers: dict, settled_by_dealer: dict, unlocked: Iterable[str] = ()) -> dict:
    """Contador de casillas por nivel.

    `settled_by_dealer` es {dealer_id: [precio, ...]} de tratos ya liquidados con cada
    vendedor. Una casilla vacía cuenta cero y los niveles altos pesan más, así que el
    hueco más caro es el del nivel más alto, no el del vendedor más accesible."""
    unlocked = set(unlocked or ())
    levels, total_empty, weighted = [], 0, 0.0
    for did, dealer in sorted((dealers or {}).items(),
                              key=lambda kv: -(kv[1].get("level") or 0)):
        level = dealer.get("level") or 0
        prices = [p for p in (settled_by_dealer or {}).get(did, []) if isinstance(p, (int, float))]
        filled = min(len(prices), SLOTS_PER_LEVEL)
        empty = SLOTS_PER_LEVEL - filled
        available = did in unlocked or bool(dealer.get("open_to_all"))
        total_empty += empty if available else 0
        weighted += (empty * level) if available else 0
        buys = [r.get("rarity") for r in (dealer.get("menu") or {}).get("buys", []) if r.get("rarity")]
        levels.append({
            "dealer": did, "name": dealer.get("name"), "level": level,
            "available": available,
            "slots_total": SLOTS_PER_LEVEL, "slots_filled": filled, "slots_empty": empty,
            "settled_deals": len(prices),
            "deals_beyond_scoring": max(0, len(prices) - SLOTS_PER_LEVEL),
            "buys_rarities": buys,
            "best_prices": sorted(prices, reverse=True)[:SLOTS_PER_LEVEL],
        })
    return {
        "slots_per_level": SLOTS_PER_LEVEL,
        "levels": levels,
        "empty_slots_available": total_empty,
        "empty_slots_weighted_by_level": round(weighted, 1),
        "next_best_slot": next((l["dealer"] for l in levels if l["available"] and l["slots_empty"]), None),
        "note": ("Un trato de más en un nivel ya lleno no suma: `deals_beyond_scoring` es munición gastada. "
                 "Se ordena por nivel descendente porque los niveles altos pesan más."),
    }


def feed_health(stores: Iterable[Path | str], now_tick: Optional[int] = None,
                stale_after: int = 40) -> dict:
    """Antigüedad del almacén del feed, que es de quien dependen los precios.

    `stale_after` en ticks: por debajo, el oráculo se considera utilizable."""
    ticks, events, files = [], 0, []
    for path in stores or ():
        p = Path(path)
        if not p.exists():
            files.append({"path": str(p), "exists": False})
            continue
        n, lo, hi = 0, None, None
        with p.open() as fh:
            for line in fh:
                try:
                    tick = json.loads(line).get("tick")
                except (ValueError, TypeError):
                    continue
                n += 1
                if isinstance(tick, int):
                    lo = tick if lo is None else min(lo, tick)
                    hi = tick if hi is None else max(hi, tick)
        events += n
        if hi is not None:
            ticks.append(hi)
        files.append({"path": str(p), "exists": True, "events": n,
                      "first_tick": lo, "last_tick": hi,
                      "span_ticks": (hi - lo) if lo is not None and hi is not None else None})
    last = max(ticks) if ticks else None
    behind = (now_tick - last) if now_tick is not None and last is not None else None
    status = "no_data" if last is None else ("stale" if behind is not None and behind > stale_after else "fresh")
    out = {"status": status, "events": events, "last_tick": last, "now_tick": now_tick,
           "ticks_behind": behind, "stale_after_ticks": stale_after, "files": files}
    if status != "fresh":
        out["impact"] = ("feed_oracle, playbook, rivals y signals leen este almacén. Con el almacén parado "
                         "siguen devolviendo cifras, pero de otro momento del mercado. "
                         "Recolector: python3 feed_stream.py --collect")
    return out


def peers_block(leaderboard: dict, team: str = "t15") -> dict:
    """Qué separa a los de cabeza, medido sobre el leaderboard, no supuesto.

    `deals` sale con correlación negativa de forma consistente: la escalera paga cuota
    del rango capturado, así que lo que cuenta es el rango por trato, no el número."""
    rows = [r for r in (leaderboard or {}).get("teams") or [] if isinstance(r.get("score"), (int, float))]
    active = [r for r in rows if (r.get("score") or 0) > 8]
    if len(active) < 3:
        return {"status": "insufficient_data", "teams": len(active)}
    ys = _nums(active, "score")
    drivers = []
    for key in ("negotiating", "market", "level", "pages_complete", "album_filled", "deals", "luck"):
        xs = [r.get(key) for r in active]
        if any(not isinstance(x, (int, float)) for x in xs):
            continue
        r = pearson([float(x) for x in xs], ys)
        if r is not None:
            drivers.append({"variable": key, "r_with_score": r})
    drivers.sort(key=lambda d: -abs(d["r_with_score"]))
    efficiency = [{"team": r["team"], "rank": r.get("rank"),
                   "negotiating_per_deal": round(r["negotiating"] / r["deals"], 3),
                   "negotiating": r["negotiating"], "deals": r["deals"],
                   "album_filled": r.get("album_filled"), "pages_complete": r.get("pages_complete")}
                  for r in active
                  if isinstance(r.get("negotiating"), (int, float)) and (r.get("deals") or 0) > 0]
    efficiency.sort(key=lambda e: -e["negotiating_per_deal"])
    mine = next((e for e in efficiency if e["team"] == team), None)
    negotiators = sorted([r for r in active if (r.get("negotiating") or 0) >= 20],
                         key=lambda r: -(r.get("negotiating") or 0))
    makers = sorted([r for r in active if (r.get("market") or 0) >= 9.2],
                    key=lambda r: -(r.get("market") or 0))
    return {
        "teams_measured": len(active),
        "drivers": drivers,
        "efficiency_ranking": efficiency,
        "ours": mine,
        "our_efficiency_position": (efficiency.index(mine) + 1) if mine else None,
        "archetypes": {
            "negotiation_specialists": [{"team": r["team"], "negotiating": r.get("negotiating"),
                                         "market": r.get("market")} for r in negotiators],
            "market_makers": [{"team": r["team"], "negotiating": r.get("negotiating"),
                               "market": r.get("market")} for r in makers],
        },
        "note": ("`deals` con r negativa y `album_filled` con r ~0 significan que acumular no puntúa: "
                 "la escalera paga cuota del rango de precio capturada por trato."),
    }
