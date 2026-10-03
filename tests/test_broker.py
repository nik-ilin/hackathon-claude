"""Tests and offline evaluation for bazaar/broker.py.

Run:  python3 -m pytest -q tests/test_broker.py      or      python3 tests/test_broker.py   (prints the report)

The synthetic Market Test mirrors what the rules say about bench traders: every trader has a hidden limit, quotes
10-30 % away from it, and most relax toward it as their patience runs out (geometrically, or linearly to stress the
estimator); firm traders never move; impatient ones relax fast and leave after a few ticks; a few arrive late.
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import statistics
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bazaar import broker as B  # noqa: E402

STARTER = "/Users/Cibran/Downloads/bazaar-kit/starter_broker.py"
SEEDS = 300


# ---------------------------------------------------------------------------------------------------------------------
# Synthetic bench
# ---------------------------------------------------------------------------------------------------------------------
def offer(oid: str, side: str, price: int) -> dict:
    """A bench offer as the starter broker reads it."""
    if side == "sell":
        return {"id": oid, "want": {"cash": price, "types": []},
                "give": {"cash": 0, "assets": [{"kind": "x", "ref": "y"}]}}
    return {"id": oid, "want": {"cash": 0, "types": ["x:y"]}, "give": {"cash": price, "assets": []}}


def synthetic_session(seed: int, run: str = "b1", traders: int = 10, ticks: int = 16, start_tick: int = 100,
                      p_firm: float = 0.2, p_impatient: float = 0.3, relax: str = "geometric"
                      ) -> Tuple[List[Tuple[int, dict]], Dict[str, float]]:
    """One Market Test: ([(tick, book)], {offer id: true limit}). Books show every trader present at that tick,
    as if nobody matched (the simulator removes what we match)."""
    rng = random.Random(seed)
    people = []
    for i in range(traders):
        side = "sell" if i % 2 == 0 else "buy"
        limit = rng.uniform(20, 120) if side == "sell" else rng.uniform(40, 140)
        shade = rng.uniform(0.10, 0.30)
        kind = "firm" if rng.random() < p_firm else "impatient" if rng.random() < p_impatient else "patient"
        arrive = 0 if rng.random() < 0.8 else rng.randint(1, 4)
        stay = rng.randint(3, 7) if kind == "impatient" else ticks          # impatient ones leave early
        rho = rng.uniform(0.45, 0.70) if kind == "impatient" else rng.uniform(0.75, 0.92)
        horizon = stay if kind == "impatient" else rng.randint(10, 22)      # linear relaxers reach the limit here
        people.append(dict(oid=f"{run}-{i}", side=side, limit=limit, shade=shade, kind=kind, arrive=arrive,
                           leave=arrive + stay, rho=rho, horizon=horizon))
    seq = []
    for t in range(ticks):
        offers = []
        for p in people:
            if not p["arrive"] <= t < p["leave"]:
                continue
            age = t - p["arrive"]
            if p["kind"] == "firm":
                left = 1.0
            elif relax == "geometric":
                left = p["rho"] ** age
            else:
                left = max(0.0, 1 - age / p["horizon"])
            gap = p["shade"] * p["limit"] * left
            # Rounded away from the limit: a quote is never on the wrong side of it.
            price = int(-(-(p["limit"] + gap) // 1)) if p["side"] == "sell" else int((p["limit"] - gap) // 1)
            offers.append(offer(p["oid"], p["side"], max(1, price)))
        seq.append((start_tick + t, {"bench_offers": offers, "offers": [], "fee_bps": 0, "fee_per_card": 0}))
    return seq, {p["oid"]: p["limit"] for p in people}


def evaluate(n: int = SEEDS, **kw) -> Dict[str, List[float]]:
    """Efficiency of floor and v2 over n seeds. A fresh planner per seed (no cross-session learning) is the harsh
    case for v2; `warm=True` keeps one planner across sessions, as the live broker does."""
    warm = kw.pop("warm", False)
    out: Dict[str, List[float]] = {"floor": [], "v2": []}
    planners = {"floor": B.Planner("floor"), "v2": B.Planner("v2")}
    for seed in range(n):
        seq, limits = synthetic_session(seed, run=f"b{seed}", start_tick=seed * 20, **kw)
        for mode in ("floor", "v2"):
            planner = planners[mode] if warm else B.Planner(mode)
            out[mode].append(B.simulate(seq, limits, planner=planner)["efficiency"])
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Floor fidelity
# ---------------------------------------------------------------------------------------------------------------------
def _load_starter():
    if not os.path.exists(STARTER):
        return None
    spec = importlib.util.spec_from_file_location("starter_broker", STARTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # its main loop is behind __name__ == "__main__"
    return mod


def _random_book(rng: random.Random) -> dict:
    bench = []
    for r in range(rng.randint(1, 3)):
        for i in range(rng.randint(0, 8)):
            bench.append(offer(f"b{r}-{i}", rng.choice(["sell", "buy"]), rng.randint(20, 80)))
    rng.shuffle(bench)
    offers = []
    for i in range(rng.randint(0, 12)):
        card = {"kind": "card", "ref": rng.choice(["RET-01", "LAT-02", "MAL-03"])}
        maker = rng.choice(["A", "B", "C"])
        if rng.random() < 0.5:
            offers.append({"id": i, "maker": maker, "give": {"cash": 0, "assets": [card]},
                           "want": {"cash": rng.randint(5, 60), "types": []}})
        else:
            offers.append({"id": i, "maker": maker, "give": {"cash": rng.randint(5, 60), "assets": []},
                           "want": {"cash": 0, "types": ["{kind}:{ref}".format(**card)]}})
    return {"bench_offers": bench, "offers": offers,
            "fee_bps": rng.choice([0, 150]), "fee_per_card": rng.choice([0, 1])}


def test_floor_plan_is_the_starter_plan():
    starter = _load_starter()
    if starter is None:
        print("  (starter kit not found: skipped)")
        return
    rng = random.Random(7)
    for _ in range(500):
        book = _random_book(rng)
        assert B.bench_plan(book) == starter.bench_plan(book)
        assert B.public_plan(book) == starter.public_plan(book)
        assert B.floor_plan(book) == starter.bench_plan(book) + starter.public_plan(book)


# ---------------------------------------------------------------------------------------------------------------------
# Trader model
# ---------------------------------------------------------------------------------------------------------------------
def _feed(model: B.TraderModel, oid: str, side: str, prices: List[int], start: int = 0) -> None:
    for k, p in enumerate(prices):
        model.observe(start + k, {"bench_offers": [offer(oid, side, p)]})


def test_relaxer_asymptote():
    m = B.TraderModel()
    # Seller conceding 16 then 8 (r = 0.5): the remaining tail is 8 * 0.5 / 0.5 = 8, so the cost is 54 - 8 = 46.
    _feed(m, "b1-0", "sell", [78, 62, 54])
    e = m.estimate("b1-0")
    assert e.kind == "relaxer" and abs(e.limit - 46) < 0.01, e
    m = B.TraderModel()
    _feed(m, "b1-1", "buy", [80, 88, 92])           # mirrored: 8 then 4, value 92 + 4 = 96
    assert abs(m.estimate("b1-1").limit - 96) < 0.01


def test_ratio_is_clipped():
    m = B.TraderModel()
    _feed(m, "b1-0", "sell", [100, 95, 90, 85])     # linear: r = 1 -> clipped to 0.9 -> tail 9*5 = 45
    e = m.estimate("b1-0")
    assert abs(e.limit - max(85 - 45, 100 * (1 - B.MAX_SHADE))) < 0.01, e


def test_firm_trader_uses_learned_shade():
    m = B.TraderModel()
    _feed(m, "b1-0", "buy", [80, 80, 80, 80, 80])
    e = m.estimate("b1-0")
    assert e.kind == "firm" and e.reach == 80
    assert abs(e.limit - 80 * (1 + B.DEFAULT_SHADE)) < 0.01


def test_limits_never_inside_the_quote():
    seq, _ = synthetic_session(3)
    m = B.TraderModel()
    for tick, book in seq:
        m.observe(tick, book)
        for o in book["bench_offers"]:
            e = m.estimate(o["id"])
            if B.is_seller(o):
                assert e.limit <= B.quote_of(o) and e.reach >= e.limit
            else:
                assert e.limit >= B.quote_of(o) and e.reach <= e.limit


def test_departure_learning():
    m = B.TraderModel()
    for t in range(6):
        offers = [offer("b1-0", "sell", 50)] + ([offer("b1-1", "buy", 40)] if t < 3 else [])
        m.observe(t, {"bench_offers": offers})
    assert m.traders["b1-1"].gone and not m.traders["b1-1"].matched
    assert m.learned()["leave_age"] == 3


# ---------------------------------------------------------------------------------------------------------------------
# v2 planner
# ---------------------------------------------------------------------------------------------------------------------
def test_max_weight_matching_exact():
    w = {(0, 0): 5.0, (0, 1): 6.0, (1, 0): 4.0}
    assert sorted(B._max_weight_matching(2, 2, w)) == [(0, 1), (1, 0)]   # 10 beats greedy's 6


def test_v2_matches_cross_and_prices_inside_quotes():
    for seed in range(40):
        seq, limits = synthetic_session(seed)
        res = B.simulate(seq, limits, mode="v2")
        assert res["invalid"] == 0, (seed, res["invalid"])
        assert res["realised"] >= -1e-9          # crossing pairs never lose at the true limits


def test_v2_keeps_low_cost_seller_for_high_value_buyer():
    """A buyer (value 100) crosses an extramarginal seller (cost 85) now, while a low-cost seller (40) is relaxing
    towards it. The floor takes the 15; v2 waits and takes the 60."""
    seq = []
    s_low = [104, 80, 66, 58, 54, 52, 51]                  # cost ~50, halving concessions
    for t in range(7):
        seq.append((t, {"bench_offers": [offer("b1-0", "sell", s_low[t]), offer("b1-1", "sell", 88),
                                         offer("b1-2", "buy", 92)]}))
    limits = {"b1-0": 50.0, "b1-1": 85.0, "b1-2": 100.0}
    floor = B.simulate(seq, limits, mode="floor")
    v2 = B.simulate(seq, limits, mode="v2")
    assert floor["realised"] == 15 and v2["realised"] == 50, (floor, v2)


def test_probe_spread_is_opt_in_and_priced_inside_the_spread():
    """At tick 3 the quotes (ask 66, bid 60) do not cross but the estimated limits (cost ~64, value ~72) do. Only
    probe_spread proposes the pair, priced inside the spread; it pays only if the venue accepts limit-feasible
    prices."""
    seq = [(t, {"bench_offers": [offer("b1-0", "sell", a), offer("b1-1", "buy", b)]})
           for t, (a, b) in enumerate([(80, 50), (72, 54), (68, 57), (66, 60)])]
    limits = {"b1-0": 62.0, "b1-1": 70.0}
    assert B.simulate(seq, limits, mode="v2", accept="limit")["matches"] == []
    probe = B.simulate(seq, limits, planner=B.Planner("v2", probe_spread=True), accept="limit")
    assert len(probe["matches"]) == 1 and probe["realised"] == 8, probe
    assert 60 <= probe["matches"][0][3] <= 66
    refused = B.simulate(seq, limits, planner=B.Planner("v2", probe_spread=True), accept="quote")
    assert refused["matches"] == [] and refused["invalid"] == 1


def test_endgame_crosses_everything():
    m, mem = B.TraderModel(), B.PlanMemory()
    book = {"bench_offers": [offer("b1-0", "sell", 50), offer("b1-1", "buy", 60)]}
    m.observe(0, book)
    m.observe(B.SESSION_TICKS - B.ENDGAME_TICKS, book)
    d = B.v2_plan(book, m, memory=mem)
    assert [x[:2] for x in d.matches] == [("b1-0", "b1-1")]
    assert d.reasons[("b1-0", "b1-1")] == "endgame"


def test_watchdog_falls_back_to_floor_when_v2_breaks():
    p = B.Planner("v2")
    book = {"bench_offers": [offer("b1-0", "sell", 50), offer("b1-1", "buy", 60)], "offers": []}
    original = B.v2_plan
    try:
        B.v2_plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r = p.plan(0, book)
    finally:
        B.v2_plan = original
    assert [m[:3] for m in r.matches] == [("b1-0", "b1-1", 55)] and r.matches[0][3] == "watchdog-error"


def test_watchdog_idle():
    p = B.Planner("v2")
    book = {"bench_offers": [offer("b1-0", "sell", 50), offer("b1-1", "buy", 60)], "offers": []}
    original = B.v2_plan
    try:
        B.v2_plan = lambda *a, **k: B.Decision()
        assert p.plan(0, book).matches == []
        assert [m[3] for m in p.plan(1, book).matches] == ["watchdog-idle"]
    finally:
        B.v2_plan = original


# ---------------------------------------------------------------------------------------------------------------------
# Live loop (fake client: no network)
# ---------------------------------------------------------------------------------------------------------------------
class FakeBroker:
    def __init__(self, books: List[Tuple[int, dict]], refuse: Optional[set] = None):
        self.books, self.i, self.refuse, self.sent = books, 0, refuse or set(), []

    def clock(self):
        return {"tick": self.books[min(self.i, len(self.books) - 1)][0]}

    def book(self):
        book = self.books[min(self.i, len(self.books) - 1)][1]
        self.i += 1
        return book

    def match(self, sell, buy, price):
        self.sent.append((sell, buy, price))
        if (sell, buy) in self.refuse:
            raise RuntimeError("taken")
        return {"ok": True}


def _loop(books, mode, refuse=None, loops=None):
    tmp = tempfile.mkdtemp()
    fake = FakeBroker(books, refuse)
    B.run(mode=mode, client=fake, max_loops=loops or len(books), sleep=lambda s: None,
          books_log=os.path.join(tmp, "b.jsonl"), matches_log=os.path.join(tmp, "m.jsonl"),
          heartbeat_file=os.path.join(tmp, "hb"))
    return fake, tmp


def test_loop_never_retries_a_refused_pair_on_same_quotes():
    book = {"bench_offers": [offer("b1-0", "sell", 50), offer("b1-1", "buy", 60)], "offers": []}
    # The same book read 6 times in one tick, then again in a later tick with the same quotes.
    fake, _ = _loop([(5, book)] * 6 + [(6, book)] * 3, "floor", refuse={("b1-0", "b1-1")})
    assert fake.sent == [("b1-0", "b1-1", 55)], fake.sent


def test_loop_caps_matches_per_tick_and_journals():
    bench = [offer(f"b1-{i}", "sell", 10) for i in range(15)] + [offer(f"b1-{i+15}", "buy", 90) for i in range(15)]
    fake, tmp = _loop([(1, {"bench_offers": bench, "offers": []})], "floor")
    assert len(fake.sent) == B.MAX_MATCHES_PER_TICK
    assert os.path.getsize(os.path.join(tmp, "b.jsonl")) > 0 and os.path.exists(os.path.join(tmp, "hb"))
    with open(os.path.join(tmp, "m.jsonl")) as f:
        assert sum(1 for _ in f) == B.MAX_MATCHES_PER_TICK


def test_shadow_executes_floor_and_logs_v2():
    seq, _ = synthetic_session(11)
    fake, tmp = _loop(seq, "shadow")
    with open(os.path.join(tmp, "m.jsonl")) as f:
        kinds = [json.loads(line)["kind"] for line in f]
    assert "v2_decision" in kinds
    assert all(s.startswith("b") for s, _, _ in fake.sent)


# ---------------------------------------------------------------------------------------------------------------------
# Efficiency: v2 vs floor
# ---------------------------------------------------------------------------------------------------------------------
SCENARIOS = {
    "standard (10 traders, geometric)": dict(),
    "linear relaxation": dict(relax="linear"),
    "hard (12 traders, firmer, more impatient)": dict(traders=12, p_firm=0.35, p_impatient=0.5),
    "hard + linear": dict(traders=12, p_firm=0.35, p_impatient=0.5, relax="linear"),
    "standard, warm model across sessions": dict(warm=True),
}


def _summary(res: Dict[str, List[float]]) -> Tuple[float, float, float, float]:
    diff = [v - f for f, v in zip(res["floor"], res["v2"])]
    losing = sum(d < -1e-9 for d in diff) / len(diff)
    return statistics.mean(res["floor"]), statistics.mean(res["v2"]), min(diff), losing


def test_v2_beats_floor_on_average_and_never_collapses():
    """v2 beats the floor on average in every scenario. "Never catastrophically worse": no single session loses
    more than 40 points of efficiency, and no block of 8 sessions (a day's Market Tests, as a round averages them)
    loses more than 5 points."""
    for name, kw in SCENARIOS.items():
        res = evaluate(SEEDS, **kw)
        floor, v2, worst, losing = _summary(res)
        assert v2 > floor, (name, floor, v2)
        assert worst > -0.40, (name, worst)
        diff = [v - f for f, v in zip(res["floor"], res["v2"])]
        rounds = [statistics.mean(diff[i:i + 8]) for i in range(0, len(diff) - 7, 8)]
        assert min(rounds) > -0.05, (name, min(rounds))
        assert losing < 0.15, (name, losing)


def report() -> None:
    print(f"\nEfficiency over {SEEDS} seeds (share of possible gains at the true limits)")
    print(f"{'scenario':44s} {'floor':>7s} {'v2':>7s} {'gain':>7s} {'worst':>7s} {'v2<floor':>9s}")
    for name, kw in SCENARIOS.items():
        f, v, worst, lose = _summary(evaluate(SEEDS, **kw))
        print(f"{name:44s} {f:7.3f} {v:7.3f} {v - f:+7.3f} {worst:+7.3f} {lose:9.1%}")
    print("\nIf the venue accepted any price between the true limits (unverified; see probe_spread):")
    for name, kw in list(SCENARIOS.items())[:3]:
        floor, probe = [], []
        for seed in range(SEEDS):
            seq, limits = synthetic_session(seed, run=f"b{seed}", start_tick=seed * 20, **kw)
            floor.append(B.simulate(seq, limits, mode="floor", accept="limit")["efficiency"])
            probe.append(B.simulate(seq, limits, planner=B.Planner("v2", probe_spread=True),
                                    accept="limit")["efficiency"])
        f, v = statistics.mean(floor), statistics.mean(probe)
        print(f"{name:44s} {f:7.3f} {v:7.3f} {v - f:+7.3f}   (floor vs v2 + probe_spread)")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    report()
    sys.exit(1 if failed else 0)
