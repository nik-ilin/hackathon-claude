"""Serie temporal del equipo y de los rivales, para ver la trayectoria y no sólo la foto.

El panel sólo mostraba el estado actual. Sin historia no se puede responder a lo único
que importa para subir en el ranking: **qué acción movió los puntos**. El salto medido
de hoy (SAL-08 vendida a 25 P, score 23.54 -> 24.91) sólo se ve comparando dos
instantes.

Un registro por `tick`, en JSON Lines, idempotente: volver a muestrear el mismo tick
sustituye la fila en vez de duplicarla, así que el panel puede refrescar cada 12 s sin
inflar el fichero. No abre la red.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Iterable, Optional

FIELDS = ("score", "negotiating", "market", "rank", "cash", "collection_value",
          "deals", "album_filled", "pages_complete", "ladder_points", "duel_points",
          "neg_points", "mm_points")


def sample(tick: int, t_hours: Optional[float], score: dict, leaderboard: dict,
           cash=None, collection_value=None, team: str = "t15") -> dict:
    """Una fila. `score` es `me["score"]`; lo que falte se completa del leaderboard público,
    que trae `negotiating`, `market`, `deals`, `album_filled` y `pages_complete` de todos."""
    rows = {r.get("team"): r for r in (leaderboard or {}).get("teams") or []}
    mine = rows.get(team) or {}
    out = {"tick": int(tick), "t_hours": t_hours, "team": team}
    for key in FIELDS:
        value = (score or {}).get(key)
        if value is None:
            value = mine.get(key)
        out[key] = value
    if out.get("cash") is None:
        out["cash"] = cash
    if out.get("collection_value") is None:
        out["collection_value"] = collection_value
    out["rivals"] = {t: r.get("score") for t, r in rows.items()
                     if isinstance(r.get("score"), (int, float))}
    return out


def append(path: Path | str, row: dict) -> int:
    """Escribe la fila sustituyendo la del mismo tick. Devuelve el número de filas."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = {r["tick"]: r for r in load(p)}
    rows[row["tick"]] = row
    ordered = [rows[t] for t in sorted(rows)]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered), encoding="utf-8")
    return len(ordered)


def load(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row.get("tick"), int):
            out.append(row)
    return sorted(out, key=lambda r: r["tick"])


def series(rows: Iterable[dict], key: str) -> list[tuple[int, float]]:
    """(tick, valor) para un campo, saltando los huecos en vez de rellenarlos con ceros:
    un cero inventado en una gráfica se lee como una caída real."""
    return [(r["tick"], float(r[key])) for r in rows
            if isinstance(r.get(key), (int, float))]


def efficiency_series(rows: Iterable[dict]) -> list[tuple[int, float]]:
    """negotiating / deals a lo largo del tiempo: rango capturado por trato, que es lo que
    paga la escalera. Sube cuando se regatea mejor y baja cuando se cierra barato."""
    out = []
    for r in rows:
        neg, deals = r.get("negotiating"), r.get("deals")
        if isinstance(neg, (int, float)) and isinstance(deals, (int, float)) and deals:
            out.append((r["tick"], round(neg / deals, 4)))
    return out


def deltas(rows: list[dict], key: str = "score", window: int = 1) -> list[dict]:
    """Variación entre muestras, con lo que cambió a la vez. Es la atribución: si el score
    sube 1.37 y `deals` sube 1, ese trato valió 1.37."""
    out = []
    picked = rows[-(window + 1):] if window else rows
    for before, after in zip(picked, picked[1:]):
        if not isinstance(before.get(key), (int, float)) or not isinstance(after.get(key), (int, float)):
            continue
        change = {"from_tick": before["tick"], "to_tick": after["tick"],
                  "delta": round(after[key] - before[key], 3)}
        for extra in ("deals", "negotiating", "market", "cash", "collection_value", "album_filled"):
            a, b = before.get(extra), after.get(extra)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a != b:
                change[f"delta_{extra}"] = round(b - a, 3)
        if change.get("delta_deals"):
            change["points_per_deal"] = round(change["delta"] / change["delta_deals"], 3)
        out.append(change)
    return out


def rank_race(rows: list[dict], team: str = "t15", around: int = 1) -> dict:
    """Nuestra serie y la de los rivales inmediatos, que son los que deciden el puesto.

    Los vecinos se eligen por el ÚLTIMO estado, no por el primero: adelantar a quien ya
    quedó atrás no cambia el puesto."""
    if not rows:
        return {"series": {}, "neighbours": []}
    last = rows[-1].get("rivals") or {}
    order = sorted(last.items(), key=lambda kv: -kv[1])
    names = [t for t, _ in order]
    if team not in names:
        return {"series": {}, "neighbours": []}
    i = names.index(team)
    picked = names[max(0, i - around): i + around + 1]
    out = {t: [] for t in picked}
    for r in rows:
        for t in picked:
            value = (r.get("rivals") or {}).get(t)
            if isinstance(value, (int, float)):
                out[t].append((r["tick"], float(value)))
    return {"series": out, "neighbours": [t for t in picked if t != team],
            "position": i + 1, "teams": len(names)}


def summary(rows: list[dict], team: str = "t15") -> dict:
    """Lo que se lee de un vistazo: desde dónde venimos, la tendencia y el mejor trato."""
    if len(rows) < 2:
        return {"samples": len(rows), "status": "insufficient_history"}
    first, last = rows[0], rows[-1]
    moves = deltas(rows, "score", window=0)
    scoring = [m for m in moves if m.get("points_per_deal") is not None]
    best = max(scoring, key=lambda m: m["points_per_deal"]) if scoring else None
    recent = [m["delta"] for m in moves[-5:]]
    return {
        "samples": len(rows), "status": "ok",
        "span_ticks": last["tick"] - first["tick"],
        "score_from": first.get("score"), "score_to": last.get("score"),
        "score_change": (round(last["score"] - first["score"], 2)
                         if isinstance(first.get("score"), (int, float))
                         and isinstance(last.get("score"), (int, float)) else None),
        "rank_from": first.get("rank"), "rank_to": last.get("rank"),
        "recent_trend": round(statistics.mean(recent), 3) if recent else None,
        "best_deal": best,
        "note": ("`points_per_deal` atribuye la subida de score a los tratos cerrados en ese "
                 "intervalo. Es una atribución, no una causa aislada: pueden coincidir varias cosas."),
    }
