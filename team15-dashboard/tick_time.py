"""Convert game ticks to local wall time only when there is clock evidence."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")


def _label(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, MADRID).strftime("%d/%m %H:%M")


def estimate(tick: int | None, *, current_tick: int | None, captured_at: float | None,
             clock: dict | None, samples: list[dict] | None = None) -> dict:
    """Return a date label and provenance; never extrapolate backward across pauses."""
    if tick is None:
        return {"label": None, "quality": "unknown"}
    points = {int(row["tick"]): float(row["captured_at"]) for row in samples or []
              if isinstance(row, dict) and row.get("tick") is not None and row.get("captured_at") is not None}
    if tick in points:
        return {"label": _label(points[tick]), "quality": "observed"}
    ordered = sorted(points.items())
    before = max((point for point in ordered if point[0] < tick), default=None)
    after = min((point for point in ordered if point[0] > tick), default=None)
    if before and after:
        ratio = (tick - before[0]) / (after[0] - before[0])
        return {"label": _label(before[1] + (after[1] - before[1]) * ratio), "quality": "interpolated"}
    if tick == current_tick and captured_at is not None:
        return {"label": _label(float(captured_at)), "quality": "observed"}
    clock = clock or {}
    if current_tick is not None and tick > current_tick:
        if clock.get("paused"):
            return {"label": None, "quality": "paused"}
        try:
            interval = float(clock["tick_seconds"])
            next_in = float(clock.get("next_tick_in", interval))
            if interval <= 0 or captured_at is None:
                raise ValueError
            seconds = next_in + max(0, tick - current_tick - 1) * interval
            return {"label": _label(float(captured_at) + seconds), "quality": "estimated"}
        except (KeyError, TypeError, ValueError):
            return {"label": None, "quality": "unknown"}
    return {"label": None, "quality": "unknown"}


def context(clock: dict | None, captured_at: float | None, samples: list[dict] | None = None) -> dict:
    clock = clock or {}
    return {"tick": clock.get("tick"), "captured_at": captured_at,
            "tick_seconds": clock.get("tick_seconds"), "next_tick_in": clock.get("next_tick_in"),
            "paused": bool(clock.get("paused")),
            "samples": [{"tick": row["tick"], "captured_at": row["captured_at"]}
                        for row in samples or [] if row.get("tick") is not None and row.get("captured_at") is not None]}
