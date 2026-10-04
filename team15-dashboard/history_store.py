"""Private local snapshots used for dashboard trends and agent evaluation."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any


SNAPSHOT_KEYS = (
    "score", "scoring", "leaderboard", "rank_race_all", "operations", "market_activity",
    "trade_history", "trade_summary", "ladder", "catalog_rows", "inventory", "needs",
    "teams", "trades", "history_summary", "impact_events", "radio", "radio_summary",
    "verified", "cash", "collection_value", "buy_capacity", "tick", "clock", "built_at",
)


class HistoryStore:
    """One detailed, credential-free JSON snapshot per tick; 90-day rolling retention."""

    def __init__(self, path: Path, retention_days: int = 90):
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with closing(self._connect(create=True)):
            pass

    def _connect(self, create: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=8)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                tick INTEGER PRIMARY KEY,
                captured_at REAL NOT NULL,
                payload TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS snapshots_captured_at ON snapshots(captured_at)")
            conn.commit()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return conn

    @staticmethod
    def snapshot_payload(data: dict) -> dict:
        """Allow-list application state; never persist request headers or credentials."""
        return {key: data.get(key) for key in SNAPSHOT_KEYS if key in data}

    def record(self, data: dict) -> bool:
        tick = data.get("tick")
        if not isinstance(tick, int):
            return False
        payload = json.dumps(self.snapshot_payload(data), ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
        now = time.time()
        cutoff = now - self.retention_days * 86400
        with self._lock, closing(self._connect(create=True)) as conn:
            cur = conn.execute("INSERT OR IGNORE INTO snapshots(tick,captured_at,payload) VALUES(?,?,?)",
                               (tick, now, payload))
            conn.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount == 1

    def ticks(self, *, limit: int = 1000, from_tick: int | None = None,
              to_tick: int | None = None) -> list[dict]:
        limit = min(5000, max(1, int(limit)))
        clauses, values = [], []
        if from_tick is not None:
            clauses.append("tick >= ?"); values.append(int(from_tick))
        if to_tick is not None:
            clauses.append("tick <= ?"); values.append(int(to_tick))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT tick,captured_at,payload FROM snapshots{where} ORDER BY tick DESC LIMIT ?",
                                (*values, limit)).fetchall()
        return [{"tick": row["tick"], "captured_at": row["captured_at"],
                 "payload": json.loads(row["payload"])} for row in reversed(rows)]

    def series(self, section: str = "score", *, ref: str | None = None,
               limit: int = 800) -> dict:
        if section not in {"score", "market", "card"}:
            raise ValueError("section debe ser score, market o card")
        rows = self.ticks(limit=limit)
        points = []
        for row in rows:
            p = row["payload"]
            if section == "score":
                score = p.get("score") or {}
                public_rank = p.get("scoring") or {}
                points.append({"tick": row["tick"], "score": score.get("score"),
                               "negotiating": score.get("negotiating"), "market": score.get("market"),
                               "duel_points": score.get("duel_points"), "ladder_points": score.get("ladder_points"),
                               "mm_points": score.get("mm_points"),
                               "rank": (public_rank.get("rank") or public_rank.get("position"))})
            elif section == "market":
                activity = p.get("market_activity") or {}
                summary = activity.get("summary") or {}
                points.append({"tick": row["tick"], "own_market_offers": summary.get("own_market_total"),
                               "open_offers": summary.get("open_offers_total"),
                               "active_sales": summary.get("active_sales"),
                               "own_market_trades": ((p.get("operations") or {}).get("market") or {}).get("trades")})
            elif section == "card":
                card = next((c for c in p.get("catalog_rows") or [] if c.get("ref") == ref), None)
                if card:
                    points.append({"tick": row["tick"], "ref": ref, "sold_median": card.get("sold_median"),
                                   "sold_count": len(card.get("sold_prices") or []),
                                   "sell_floor": card.get("sell_floor"), "buy_ceiling": card.get("buy_ceiling"),
                                   "stock": card.get("stock"), "wanted_by": card.get("wanted_by") or [],
                                   "held_by": card.get("held_by") or []})
        return {"section": section, "ref": ref, "count": len(points), "points": points,
                "retention_days": self.retention_days}

    def health(self) -> dict:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS count, MIN(tick) AS first_tick, MAX(tick) AS last_tick, "
                               "MIN(captured_at) AS first_at, MAX(captured_at) AS last_at FROM snapshots").fetchone()
        return {"status": "ready", "count": row["count"], "first_tick": row["first_tick"],
                "last_tick": row["last_tick"], "first_at": row["first_at"],
                "last_at": row["last_at"], "retention_days": self.retention_days}
