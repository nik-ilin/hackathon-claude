"""Quick health + score report:  python3 ops/report.py"""
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db = sqlite3.connect(os.path.join(ROOT, "state", "journal.db"))
rows = db.execute("SELECT tick, payload FROM scores ORDER BY ts").fetchall()
if rows:
    first, last = json.loads(rows[0][1]), json.loads(rows[-1][1])
    print(f"tick {rows[0][0]} → {rows[-1][0]}")
    for k in ("score", "rank", "negotiating", "market", "neg_points", "duel_points", "ladder_points",
              "mm_points", "bench_points", "bench_efficiency"):
        print(f"  {k:17} {first.get(k)!s:>10} → {last.get(k)!s:>10}")
print("\nintents (last 2 h):")
for r in db.execute("SELECT source, kind, status, count(*), round(sum(coalesce(delta,0)),1) FROM intents "
                    "WHERE ts > ? GROUP BY 1,2,3 ORDER BY 1,2", (time.time() - 7200,)):
    print("  ", r)
for name in ("heartbeat.agent", "heartbeat.broker"):
    p = os.path.join(ROOT, "state", name)
    if os.path.exists(p):
        print(f"{name}: {time.time() - os.path.getmtime(p):.0f}s ago")
alerts = os.path.join(ROOT, "state", "alerts.log")
if os.path.exists(alerts):
    print("\nalerts:\n" + "".join(open(alerts).readlines()[-5:]))
