"""Market-intelligence collector: read-only. Stores everything the public API
exposes into intel/market.db so nothing is lost when the 500-event feed rolls over.

  python3 intel/collector.py backfill   # one-shot: feed files + card provenance + our threads
  python3 intel/collector.py loop       # keep polling feed/leaderboard/boards (run in background)

Never posts, accepts, or opens anything.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from bazaar_sdk import Bazaar, BazaarError  # noqa: E402

DB_PATH = os.path.join(HERE, "market.db")
URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
KEY = os.environ.get("BAZAAR_KEY", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, tick INTEGER, type TEXT, actor TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY, tick INTEGER, kind TEXT, venue TEXT, persona TEXT,
  party_a TEXT, party_b TEXT, price INTEGER, fee INTEGER, n_items INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS settlement_items (
  settlement INTEGER, asset_id INTEGER, kind TEXT, ref TEXT, rarity TEXT, set_id TEXT,
  serial INTEGER, frm TEXT, too TEXT, PRIMARY KEY (settlement, asset_id));
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY, tick INTEGER, maker TEXT, too TEXT, venue TEXT, thread INTEGER,
  give_cash INTEGER, give_assets TEXT, give_types TEXT,
  want_cash INTEGER, want_assets TEXT, want_types TEXT, final INTEGER, status TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS thread_messages (
  id INTEGER PRIMARY KEY, thread INTEGER, tick INTEGER, kind TEXT, team TEXT, with_ TEXT,
  sender TEXT, text TEXT, offer_id INTEGER, price INTEGER, final INTEGER);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, kind TEXT, team TEXT, with_ TEXT, topic TEXT,
  opened_tick INTEGER, status TEXT, closed_reason TEXT);
CREATE TABLE IF NOT EXISTS provenance (
  asset_id INTEGER, idx INTEGER, tick INTEGER, frm TEXT, too TEXT, why TEXT,
  ref TEXT, rarity TEXT, owner_now TEXT, PRIMARY KEY (asset_id, idx));
CREATE TABLE IF NOT EXISTS leaderboard (
  snap_ts REAL, tick INTEGER, team TEXT, name TEXT, rank INTEGER, score REAL,
  negotiating REAL, market REAL, deals INTEGER, level INTEGER, album_filled INTEGER, luck REAL,
  PRIMARY KEY (snap_ts, team));
CREATE TABLE IF NOT EXISTS my_values (
  snap_ts REAL, tick INTEGER, ref TEXT, value_next REAL, held INTEGER, PRIMARY KEY (snap_ts, ref));
CREATE TABLE IF NOT EXISTS me_snapshots (snap_ts REAL PRIMARY KEY, tick INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS board_snapshots (
  snap_ts REAL, tick INTEGER, venue TEXT, offer_id INTEGER, maker TEXT, too TEXT,
  give_cash INTEGER, give_assets TEXT, give_types TEXT, want_cash INTEGER, want_types TEXT,
  created_tick INTEGER, expires_tick INTEGER, PRIMARY KEY (snap_ts, venue, offer_id));
CREATE TABLE IF NOT EXISTS json_snapshots (snap_ts REAL, tick INTEGER, kind TEXT, payload TEXT,
  PRIMARY KEY (snap_ts, kind));
CREATE TABLE IF NOT EXISTS duel_snapshots (snap_ts REAL, tick INTEGER, duel INTEGER, payload TEXT,
  PRIMARY KEY (snap_ts, duel));
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    return db


def _price_of(offer: dict) -> int | None:
    """Cash side of a dealer offer: whichever side carries cash."""
    for side in ("want", "give"):
        c = (offer.get(side) or {}).get("cash")
        if c:
            return c
    return None


def store_offer(db, offer: dict, tick: int) -> None:
    if not offer or offer.get("id") is None:
        return
    g, w = offer.get("give") or {}, offer.get("want") or {}
    db.execute(
        "INSERT OR REPLACE INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (offer["id"], tick, offer.get("maker"), offer.get("to"), offer.get("venue"),
         offer.get("thread"), g.get("cash"), json.dumps(g.get("assets", [])),
         json.dumps(g.get("types", [])), w.get("cash"), json.dumps(w.get("assets", [])),
         json.dumps(w.get("types", [])), int(bool(offer.get("final"))), offer.get("status"),
         json.dumps(offer)))


def store_event(db, e: dict) -> bool:
    if db.execute("SELECT 1 FROM events WHERE id=?", (e["id"],)).fetchone():
        return False
    p = e.get("payload") or {}
    tick = e.get("tick")
    db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
               (e["id"], tick, e.get("type"), e.get("actor"), json.dumps(p)))
    t = e.get("type")
    if t == "settlement":
        parties = p.get("parties") or [None, None]
        db.execute("INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (p.get("settlement"), tick, p.get("kind"), p.get("venue"), p.get("persona"),
                    parties[0], parties[1] if len(parties) > 1 else None, p.get("price"),
                    p.get("fee"), len(p.get("items", [])), json.dumps(p)))
        for it in p.get("items", []):
            db.execute("INSERT OR REPLACE INTO settlement_items VALUES (?,?,?,?,?,?,?,?,?)",
                       (p.get("settlement"), it.get("id"), it.get("kind"), it.get("ref"),
                        it.get("rarity"), it.get("set"), it.get("serial"), it.get("frm"), it.get("to")))
    elif t == "offer.listed":
        store_offer(db, p.get("offer", p), tick)
    elif t == "offer.cancelled":
        oid = p.get("offer") if isinstance(p.get("offer"), int) else (p.get("offer") or {}).get("id")
        db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (oid,))
    elif t == "thread.opened":
        db.execute("INSERT OR IGNORE INTO threads VALUES (?,?,?,?,?,?,?,?)",
                   (p.get("thread"), p.get("kind"), p.get("team"), p.get("with"),
                    json.dumps(p.get("topic")), tick, "open", None))
    elif t == "thread.closed":
        db.execute("UPDATE threads SET status=?, closed_reason=? WHERE id=?",
                   (p.get("status", "closed"), p.get("closed_reason") or p.get("reason"), p.get("thread")))
    elif t == "thread.message":
        off = p.get("offer") or {}
        store_offer(db, off, tick)
        db.execute("INSERT OR IGNORE INTO thread_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (p.get("message"), p.get("thread"), tick, p.get("kind"), p.get("team"),
                    p.get("with"), p.get("sender"), p.get("text"), off.get("id"),
                    _price_of(off) if off else None, int(bool(off.get("final"))) if off else 0))
    return True


def ingest_feed(db, events: list) -> int:
    n = sum(1 for e in events if store_event(db, e))
    db.commit()
    return n


def snapshot_leaderboard(b, db, tick: int) -> None:
    lb = b.leaderboard()
    rows = lb.get("teams", lb.get("rows", [])) if isinstance(lb, dict) else lb
    ts = time.time()
    for r in rows:
        db.execute("INSERT OR REPLACE INTO leaderboard VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, tick, r.get("team") or r.get("id"), r.get("name"), r.get("rank"),
                    r.get("score"), r.get("negotiating"), r.get("market"), r.get("deals"),
                    r.get("level"), r.get("album_filled"), r.get("luck")))
    db.commit()


def snapshot_me(b, db, tick: int) -> dict:
    me = b.me()
    ts = time.time()
    db.execute("INSERT OR REPLACE INTO me_snapshots VALUES (?,?,?)", (ts, tick, json.dumps(me)))
    cat = b.catalog()
    held = {}
    for a in me.get("assets", []):
        if a.get("kind") == "card":
            held[a["ref"]] = held.get(a["ref"], 0) + 1
    for s in cat.get("sets", []):
        for c in s.get("cards", []):
            v = b.value(c["id"]).get("your_value")
            db.execute("INSERT OR REPLACE INTO my_values VALUES (?,?,?,?,?)",
                       (ts, tick, c["id"], v, held.get(c["id"], 0)))
    db.commit()
    return me


def backfill_threads(b, db) -> None:
    """Our own threads, full message history (includes our side of every haggle)."""
    data = b.my_threads()
    for t in data.get("threads", data) if isinstance(data, dict) else data:
        th = b.thread(t["id"])
        db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?)",
                   (th["id"], th.get("kind"), th.get("team"), th.get("with"),
                    json.dumps(th.get("topic")), th.get("created_tick"), th.get("status"),
                    th.get("closed_reason")))
        for m in th.get("messages", []):
            off = m.get("offer") or {}
            store_offer(db, off, m.get("tick"))
            db.execute("INSERT OR REPLACE INTO thread_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (m.get("id"), th["id"], m.get("tick"), th.get("kind"), th.get("team"),
                        th.get("with"), m.get("sender"), m.get("text"), off.get("id"),
                        _price_of(off) if off else None, int(bool(off.get("final"))) if off else 0))
    db.commit()


def backfill_provenance(b, db, max_misses: int = 40) -> int:
    """Walk asset ids 1..N; every transfer of every card with tick + trade id."""
    aid, misses, n = 1, 0, 0
    while misses < max_misses:
        try:
            c = b.card(aid)
        except BazaarError:
            misses += 1
            aid += 1
            continue
        misses = 0
        for i, h in enumerate(c.get("history", [])):
            db.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?,?,?,?)",
                       (aid, i, h.get("tick"), h.get("from"), h.get("to"), h.get("why"),
                        c.get("ref"), c.get("rarity"), c.get("owner")))
        n += 1
        aid += 1
        if n % 50 == 0:
            db.commit()
    db.commit()
    return n


def snapshot_json(db, tick: int, kind: str, payload, last: dict) -> bool:
    """Store a JSON snapshot only when it changed since the last one of that kind."""
    blob = json.dumps(payload, sort_keys=True)
    if last.get(kind) == blob:
        return False
    last[kind] = blob
    db.execute("INSERT OR REPLACE INTO json_snapshots VALUES (?,?,?,?)", (time.time(), tick, kind, blob))
    db.commit()
    return True


def snapshot_boards(b, db, tick: int, venues: list) -> int:
    ts, n = time.time(), 0
    for v in venues:
        if v.get("status") != "open":
            continue
        try:
            offers = b.board(v["venue"]).get("offers", [])
        except BazaarError:
            continue
        for o in offers:
            g, w = o.get("give") or {}, o.get("want") or {}
            db.execute("INSERT OR REPLACE INTO board_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ts, tick, v["venue"], o.get("id"), o.get("maker"), o.get("to"), g.get("cash"),
                        json.dumps(g.get("assets", [])), json.dumps(g.get("types", [])), w.get("cash"),
                        json.dumps(w.get("types", [])), o.get("created_tick"), o.get("expires_tick")))
            n += 1
    db.commit()
    return n


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    b = Bazaar(URL, KEY, wait_on_tick=False)
    db = connect()
    if mode == "backfill":
        for path in ("/tmp/feed.json",):
            if os.path.exists(path):
                d = json.load(open(path))
                print(path, "new events:", ingest_feed(db, d.get("events", d)))
        f = b.feed(limit=500)
        print("live feed new events:", ingest_feed(db, f.get("events", f)))
        tick = b.clock().get("tick", 0)
        backfill_threads(b, db)
        print("threads done")
        snapshot_leaderboard(b, db, tick)
        snapshot_me(b, db, tick)
        print("snapshots done")
        print("assets crawled:", backfill_provenance(b, db))
    elif mode == "loop":
        # read-only: public endpoints every cycle; our own state (me, duels) with the key if present
        last_lb = last_board = last_slow = 0.0
        last_json: dict = {}
        venues: list = []
        seen_threads: set = set()
        while True:
            try:
                f = b.feed(limit=500)
                n = ingest_feed(db, f.get("events", f))
                clock = b.clock()
                tick = clock.get("tick", 0)
                now = time.time()
                if now - last_lb > 120:
                    snapshot_leaderboard(b, db, tick)
                    last_lb = now
                if now - last_board > 60 and not clock.get("paused"):
                    venues = b.venues().get("venues", [])
                    snapshot_json(db, tick, "venues", venues, last_json)
                    snapshot_boards(b, db, tick, venues)
                    last_board = now
                if now - last_slow > 300:
                    snapshot_json(db, tick, "dealers", b.dealers(), last_json)
                    snapshot_json(db, tick, "levels", b.levels(), last_json)
                    snapshot_json(db, tick, "schedule", b.schedule().get("upcoming", []), last_json)
                    snapshot_json(db, tick, "clock", {k: clock.get(k) for k in ("round", "round_name", "tick_seconds", "limits", "doors")}, last_json)
                    if KEY:
                        snapshot_me(b, db, tick)
                    last_slow = now
                if KEY and not clock.get("paused"):
                    for d in b.duels().get("duels", []):
                        db.execute("INSERT OR REPLACE INTO duel_snapshots VALUES (?,?,?,?)",
                                   (now, tick, d.get("duel"), json.dumps(d)))
                    db.commit()
                if n:
                    print(time.strftime("%H:%M:%S"), "tick", tick, "+", n, "events", flush=True)
            except Exception as exc:  # keep recording through network blips
                print("collector error:", exc, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
