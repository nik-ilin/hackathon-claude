"""The only place that writes to the server.

Strategies return Intents; the executor applies the per-tick limits from `clock.limits`,
the single team-wide accept (arbitrated by priority), the value guard, the kill switches
(state/STOP, state/STOP_TRADING), dry-run mode, and journals every attempt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .api import BazaarError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
os.makedirs(STATE, exist_ok=True)

# Minimum Δ private value per kind of trade (not tunable at runtime).
GUARD_MIN = {"p2p": 2.0, "dealer": 0.0, "duel": 0.0}

# Accept priorities (higher first).
PRIO_DUEL_DEADLINE = 100
PRIO_DEALER_FINAL = 80
PRIO_DUEL = 60
PRIO_DEALER = 50
PRIO_P2P = 20


@dataclass
class Intent:
    kind: str                     # accept | say | duel_say | duel_accept | list | cancel | open_thread | close_thread | open_pack | open_venue | flag
    source: str                   # dealer | duel | p2p | broker | ops
    args: dict
    priority: int = 0
    delta: Optional[float] = None  # Δ private value if it moves value (None = not a value move)
    why: str = ""
    result: Any = field(default=None, compare=False)


class Journal:
    def __init__(self, path: str = os.path.join(STATE, "journal.db")):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS intents (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tick INTEGER, kind TEXT, source TEXT,
          args TEXT, priority INTEGER, delta REAL, why TEXT, status TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS scores (
          ts REAL, tick INTEGER, payload TEXT);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)

    def log(self, tick: int, it: Intent, status: str, result: Any = None) -> int:
        cur = self.db.execute(
            "INSERT INTO intents (ts,tick,kind,source,args,priority,delta,why,status,result) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), tick, it.kind, it.source, json.dumps(it.args, default=str), it.priority,
             it.delta, it.why, status, json.dumps(result, default=str)[:4000] if result is not None else None))
        self.db.commit()
        return cur.lastrowid

    def score(self, tick: int, score: dict) -> None:
        self.db.execute("INSERT INTO scores VALUES (?,?,?)", (time.time(), tick, json.dumps(score)))
        self.db.commit()

    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    def our_message_ids(self) -> set:
        """Thread/offer ids we created (for the second-agent detector)."""
        ids = set()
        for (res,) in self.db.execute("SELECT result FROM intents WHERE status='ok' AND result IS NOT NULL"):
            try:
                r = json.loads(res)
            except Exception:
                continue
            if isinstance(r, dict):
                for k in ("id", "offer_id", "message_id", "thread_id"):
                    if r.get(k) is not None:
                        ids.add(r[k])
                off = r.get("offer")
                if isinstance(off, dict) and off.get("id") is not None:
                    ids.add(off["id"])
        return ids


class Executor:
    def __init__(self, api, journal: Journal, log, dry_run: bool = False):
        self.api, self.j, self.log, self.dry = api, journal, log, dry_run

    @staticmethod
    def stopped(scope: str = "all") -> bool:
        if os.path.exists(os.path.join(STATE, "STOP")):
            return True
        return scope == "trading" and os.path.exists(os.path.join(STATE, "STOP_TRADING"))

    def _guard_ok(self, it: Intent) -> bool:
        if it.delta is None:                   # fail closed for value moves
            return it.kind not in ("accept", "duel_accept", "list")
        floor = GUARD_MIN.get(it.source, 2.0)
        return it.delta >= floor

    def _call(self, tick: int, it: Intent):
        if self.dry:
            self.j.log(tick, it, "dry")
            self.log(f"[DRY] {it.kind} {it.source} {it.args} Δ={it.delta} — {it.why}")
            return {"dry": True}
        a = it.args
        fn = {
            "accept": lambda: self.api.accept(a["offer_id"], assets=a.get("assets")),
            "say": lambda: self.api.say(a["thread_id"], text=a.get("text", ""), price=a.get("price")),
            "duel_say": lambda: self.api.duel_say(a["duel_id"], text=a.get("text", ""), price=a.get("price"), days=a.get("days")),
            "duel_accept": lambda: self.api.duel_accept(a["duel_id"]),
            "list": lambda: self.api.list_offer(give=a["give"], want=a["want"], venue=a.get("venue"), to=a.get("to"),
                                                expires_in_ticks=a.get("expires_in_ticks")),
            "cancel": lambda: self.api.cancel(a["offer_id"]),
            "open_thread": lambda: self.api.open_thread(a["with"], topic=a.get("topic"), venue=a.get("venue")),
            "close_thread": lambda: self.api.close_thread(a["thread_id"]),
            "open_pack": lambda: self.api.open_pack(a["asset_id"]),
            "flag": lambda: self.api.flag(a["message_id"], a.get("reason", "")),
        }[it.kind]
        try:
            out = fn()
            self.j.log(tick, it, "ok", out)
            self.log(f"{it.source.upper():6} {it.kind} {a} Δ={it.delta} — {it.why}")
            return out
        except BazaarError as e:
            self.j.log(tick, it, f"err:{e.code}", {"message": e.message, **(e.extra or {})})
            self.log(f"{it.source.upper():6} {it.kind} refused {e.code}: {e.message} — {a}")
            it.result = e
            return e

    def run_tick(self, tick: int, limits: dict, intents: list, deadline: float) -> list:
        """Execute one tick's intents in order: one accept, then messages, then the rest."""
        done = []
        accepts = [i for i in intents if i.kind in ("accept", "duel_accept")]
        others = [i for i in intents if i.kind not in ("accept", "duel_accept")]
        max_acc = int(limits.get("accepts_per_team_per_tick", 1))
        max_list = int(limits.get("offers_per_team_per_tick", 12))
        listed = 0
        msg_threads: set = set()

        for it in sorted(accepts, key=lambda i: (-i.priority, -(i.delta or 0))):
            if max_acc <= 0:
                self.j.log(tick, it, "deferred")
                continue
            scope = "all" if it.source in ("duel", "broker") else "trading"
            if self.stopped(scope):
                self.j.log(tick, it, "stopped")
                continue
            if not self._guard_ok(it):
                self.j.log(tick, it, "guard_block")
                self.log(f"GUARD blocked {it.kind} {it.args} Δ={it.delta} — {it.why}")
                continue
            res = self._call(tick, it)
            it.result = res
            if not isinstance(res, BazaarError):
                max_acc -= 1
                done.append(it)
            elif res.code == "wait_for_tick":
                max_acc = 0

        order = {"duel_say": 0, "say": 1, "close_thread": 2, "open_pack": 3, "cancel": 4,
                 "open_thread": 5, "list": 6, "flag": 7}
        for it in sorted(others, key=lambda i: (order.get(i.kind, 9), -i.priority)):
            if time.time() > deadline:
                self.j.log(tick, it, "late")
                continue
            scope = "all" if it.source in ("duel", "broker", "ops") else "trading"
            if self.stopped(scope):
                continue
            if it.kind in ("say", "duel_say"):
                key = (it.kind, it.args.get("thread_id") or it.args.get("duel_id"))
                if key in msg_threads:
                    continue
                msg_threads.add(key)
            if it.kind in ("list", "cancel"):           # a cancelled listing still counts (RULES)
                if listed >= max_list or (it.kind == "list" and not self._guard_ok(it)):
                    self.j.log(tick, it, "guard_block" if listed < max_list else "limit")
                    continue
                listed += 1
            res = self._call(tick, it)
            it.result = res
            if not isinstance(res, BazaarError):
                done.append(it)
        return done
