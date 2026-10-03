"""Safety tests on a real `me()` snapshot recorded in intel/market.db (no network writes).

    python3 tests/test_guard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bazaar.executor import Executor, Intent, Journal  # noqa: E402
from bazaar.strategy import dealers as SD  # noqa: E402
from bazaar.values import PROTECTED, ValueBook  # noqa: E402


class FakeApi:
    """Answers value() from the recorded my_values table; refuses any write."""
    def __init__(self, values):
        self.values = values
        self.writes = []

    def value(self, ref):
        return {"your_value": self.values[ref]}

    def __getattr__(self, name):
        def w(*a, **k):
            self.writes.append((name, a, k))
            return {"id": 1}
        return w


def load():
    db = sqlite3.connect(os.path.join(ROOT, "intel", "market.db"))
    me = json.loads(db.execute("SELECT payload FROM me_snapshots ORDER BY snap_ts DESC LIMIT 1").fetchone()[0])
    ts = db.execute("SELECT max(snap_ts) FROM my_values").fetchone()[0]
    values = dict(db.execute("SELECT ref, value_next FROM my_values WHERE snap_ts=?", (ts,)))
    sys.path.insert(0, ROOT)
    from bazaar_sdk import Bazaar  # catalog is public and static
    cat = Bazaar("https://bazaar.causaprima.ai", "", wait_on_tick=False).catalog()
    return me, cat, values


def main():
    me, cat, values = load()
    api = FakeApi(values)
    vb = ValueBook(api, me, cat)
    lat = [a for a in me["assets"] if a.get("ref") == "LAT-02"][0]
    assert vb.loss(lat) >= 99, f"breaking a complete page loses its bonus, got {vb.loss(lat)}"
    dups = [a for a in me["assets"] if a.get("ref") == "LAT-05"]
    keeper = [a for a in dups if vb.keeper["LAT-05"] == a["id"]][0]
    spare = [a for a in dups if a is not keeper][0]
    assert vb.loss(keeper) >= 99 and vb.loss(spare) < 3, "keep one copy, spares are cheap"
    ev = vb.pack_ev("sobre_barrio")
    assert ev < 20, f"pack EV for us should be low, got {ev}"
    # a pack-buying thread's reservation equals EV: a 30 P final must be refused
    th = {"id": 9, "with": "abuela", "topic": {"buy": {"pack": "sobre_barrio"}},
          "standing_offers": [{"id": 5, "maker": "abuela", "status": "open", "final": True,
                               "want": {"cash": 30}, "give": {"cash": 0}}], "messages": []}
    out = SD.haggle(th, {"reservation": ev, "sent": [10]}, vb, 1)
    assert not any(i.kind == "accept" for i in out), "must not accept a pack at 30"
    # executor: one accept per tick, guard blocks negatives
    j = Journal(path=":memory:")
    ex = Executor(api, j, lambda m: None)
    its = [Intent("accept", "p2p", {"offer_id": 1}, priority=20, delta=-5),
           Intent("accept", "p2p", {"offer_id": 2}, priority=20, delta=6),
           Intent("accept", "duel", {"offer_id": 3}, priority=100, delta=10)]
    done = ex.run_tick(1, {"accepts_per_team_per_tick": 1}, its, 10**12)
    assert [d.args["offer_id"] for d in done] == [3], done
    print("OK — guard, keeper copy, page protection, pack EV", round(ev, 1), "and one-accept arbiter")


if __name__ == "__main__":
    main()


def test_regressions():
    """Round-3 audit regressions."""
    from bazaar.strategy import duels as D
    d = {"duel": 1, "role": "buyer", "your_limit": 100, "your_days_weight": [50] * 11, "status": "live"}
    assert D.margin(d, 120, 5) < 0, "days must never excuse a price outside the limit"
    import bazaar.agent as A
    ag = A.Agent.__new__(A.Agent)
    ag.known_threads, ag.started_tick = set(), 10
    ag.j = Journal(path=":memory:")
    import os as _os
    stop = _os.path.join(A.STATE, "STOP_TRADING")
    existed = _os.path.exists(stop)
    ag.check_foreign_activity([{"id": 135, "team": "t04", "with": "t15", "created_tick": 20}])
    assert existed or not _os.path.exists(stop), "a thread opened by another team must not trigger STOP"
    print("OK — regressions (days margin, foreign-thread detector)")


if __name__ == "__main__":
    test_regressions()
