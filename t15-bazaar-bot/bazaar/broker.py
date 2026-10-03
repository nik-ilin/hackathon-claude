"""Production broker for our `board` venue: the Market Test (bench) and our public offers.

Why a broker at all
-------------------
On the Market Test every venue receives the same synthetic book (`bench_offers`). Traders quote AWAY from a hidden
limit (a seller asks above its cost, a buyer bids below its value); most relax as their patience runs out, firm ones
never move, some leave early. The score is the share of the gains *between true limits* that our matches realise.
Crossing by quote (the free stall, the starter broker) earns half the points. Because a quote is never beyond its
limit, every pair that crosses on quotes realises a non-negative true gain: the only things a broker can get wrong
are *which* crossing pairs it executes and *when*. That is what this module improves on.

Layers (all in one process)
---------------------------
1. `floor_plan`  - the starter broker's `bench_plan` + `public_plan`, copied faithfully. Guarantees the stall's half.
2. `TraderModel` - follows every bench offer across polls (quote history, tenure, disappearance) and estimates its
                   hidden limit, how far it will still move this session, and whether it is about to leave.
3. `v2_plan`     - per bench run, a maximum-weight matching on *estimated* gains over pairs that cross now or are
                   expected to cross soon; executes crossing pairs of that plan (impatient ones at once, patient
                   ones after a short hold), reserves the rest for a few ticks, and in the last ticks of a session
                   crosses everything that still crosses.
4. `run`         - the live loop: modes 'floor' | 'shadow' | 'v2', plans once per book state, never retries a refused
                   pair on unchanged quotes, caps matches per tick, watchdog back to the floor, JSONL journals.
5. `simulate`    - offline evaluator: replays a book sequence against known true limits and scores efficiency.

Opt-in `--probe-spread`: v2 also proposes pairs whose *estimated limits* cross although their quotes do not, priced
inside the spread. It only pays if the venue validates bench matches against hidden limits rather than quotes, which
is unverified; a refused match costs nothing.

Parameters were tuned on the synthetic bench of tests/test_broker.py; replay state/bench_books.jsonl to re-check them
against the real bench once a few sessions are recorded.

CLI:  python3 -m bazaar.broker --mode shadow [--dry-run] [--probe-spread]   (BROKER_KEY env or state/broker_key.txt)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
BOOKS_LOG = os.path.join(STATE_DIR, "bench_books.jsonl")
MATCHES_LOG = os.path.join(STATE_DIR, "broker_matches.jsonl")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.broker")
KEY_FILE = os.path.join(STATE_DIR, "broker_key.txt")

MODES = ("floor", "shadow", "v2")
POLL_SECONDS = 0.5             # two reads a second, as the starter does
MAX_MATCHES_PER_TICK = 10      # the venue's per-tick cap (bench + public together)

SESSION_TICKS = 16             # a Market Test lasts 16 ticks (GET /api/schedule: params.ticks)
ENDGAME_TICKS = 3              # in the last 3 ticks cross everything that crosses
WATCHDOG_TICKS = 2             # v2 idle this many ticks while the floor would match -> run the floor

# Trader model
FIRM_AFTER = 3                 # ticks without any concession before a trader counts as firm (or converged)
DEFAULT_SHADE = 0.15           # prior for |first quote - limit| / first quote until relaxers teach us better
MAX_SHADE = 0.45               # an estimate never puts the limit further than this from the first quote
DEFAULT_RATIO = 0.6            # prior concession ratio r for a trader seen to move only once
MAX_RATIO = 0.9                # r is clipped to [0, MAX_RATIO]: the tail d*r/(1-r) stays finite
RATIO_WINDOW = 4               # per-tick concessions used to fit r
FAST_RATE = 0.03               # relaxing >= 3 % of the first quote per tick = impatient
DEFAULT_LEAVE_AGE = 5          # prior tenure (ticks) after which early leavers go

# v2 planner
HOLD_TICKS = 1                 # a patient pair that crosses waits this many ticks before execution
MAX_WAIT = 3                   # a reserved pair that does not cross yet is held at most this many ticks
P_SOON_PATIENT = 0.85          # chance a not-yet-crossing pair of patient traders crosses before anyone leaves
P_SOON_IMPATIENT = 0.4         # ... when one of them is impatient
P_SOON_NEW = 0.6               # ... when one of them has been seen only once (nothing learned yet)
# Bonuses and margins are fractions of the pair's mid quote, so they hold whatever the bench's price scale.
NOW_BONUS = 0.01               # a pair that crosses now: a certain gain beats a hoped-for one
FLOOR_BONUS = 0.12             # ... and the floor takes that very pair (or it is on hold): deviate on a clear edge only
RESERVE_MARGIN = 0.0           # a hoped-for pair must be worth this much before it may displace a crossing one
PROBE_MARGIN = 0.05            # probe_spread: estimated gain needed to propose a pair whose quotes do not cross

Match = Tuple[str, str, int]   # (sell offer id, buy offer id, price)


# ---------------------------------------------------------------------------------------------------------------------
# Book helpers. The bench format is the starter broker's: a seller has want.cash > 0, a buyer give.cash > 0, and an
# offer keeps its id while it re-quotes (verify on the first recorded session: state/bench_books.jsonl).
# ---------------------------------------------------------------------------------------------------------------------
def run_of(offer_id: str) -> str:
    """The bench run an offer belongs to: "b12" in "b12-7". Matches pair two offers of one run."""
    return str(offer_id).split("-")[0]


def is_seller(offer: dict) -> bool:
    """A bench seller asks for cash (want.cash > 0); a bench buyer bids it (give.cash)."""
    return bool((offer.get("want") or {}).get("cash"))


def quote_of(offer: dict) -> int:
    return int(offer["want"]["cash"]) if is_seller(offer) else int(offer["give"]["cash"])


def bench_runs(book: dict) -> Dict[str, Tuple[List[dict], List[dict]]]:
    """run -> (sellers, buyers), in book order."""
    runs: Dict[str, Tuple[List[dict], List[dict]]] = {}
    for o in book.get("bench_offers") or []:
        sellers, buyers = runs.setdefault(run_of(o["id"]), ([], []))
        (sellers if is_seller(o) else buyers).append(o)
    return runs


def midpoint(ask: int, bid: int) -> int:
    """The price we propose: the midpoint of the two quotes, so ask <= price <= bid whenever bid >= ask."""
    return (ask + bid) // 2


# ---------------------------------------------------------------------------------------------------------------------
# 1. Floor: the starter broker, faithfully
# ---------------------------------------------------------------------------------------------------------------------
def bench_plan(book: dict) -> List[Match]:
    """[(sell id, buy id, price)]: in each bench run, the highest bid against the lowest ask while the bid covers it,
    at the midpoint. sorted() keeps the book's order among equal quotes, as the stall does."""
    plan: List[Match] = []
    runs: Dict[str, Tuple[list, list]] = {}
    for o in book.get("bench_offers") or []:
        asks, bids = runs.setdefault(o["id"].split("-")[0], ([], []))
        if o["want"]["cash"]:
            asks.append((o["want"]["cash"], o["id"]))
        else:
            bids.append((o["give"]["cash"], o["id"]))
    for asks, bids in runs.values():
        for (ask, sell), (bid, buy) in zip(sorted(asks, key=lambda a: a[0]), sorted(bids, key=lambda b: -b[0])):
            if bid < ask:
                break
            plan.append((sell, buy, (ask + bid) // 2))
    return plan


def public_plan(book: dict) -> List[Match]:
    """[(sell id, buy id, price)]: the venue's real offers, card by card, the lowest ask against the highest bid for
    that card, at the midpoint, lowered until the buyer can also pay the fee. At most 10 matches per tick."""
    def fee(price: int) -> int:  # as the venue charges it, rounded up
        return math.ceil(book["fee_bps"] * price / 10000) + book["fee_per_card"]

    plan: List[Match] = []
    offers = book.get("offers") or []
    bids = sorted((o for o in offers if o["give"]["cash"] and len(o["want"]["types"]) == 1),
                  key=lambda o: -o["give"]["cash"])
    for s in sorted((o for o in offers if len(o["give"]["assets"]) == 1 and o["want"]["cash"]),
                    key=lambda o: o["want"]["cash"]):
        ask, card = s["want"]["cash"], "{kind}:{ref}".format(**s["give"]["assets"][0])
        b = next((b for b in bids if b["want"]["types"] == [card] and b["maker"] != s["maker"]
                  and ask + fee(ask) <= b["give"]["cash"]), None)
        if b:
            bids.remove(b)
            price = next(p for p in range((ask + b["give"]["cash"]) // 2, ask - 1, -1)
                         if p + fee(p) <= b["give"]["cash"])
            plan.append((s["id"], b["id"], price))
    return plan[:10]


def floor_plan(book: dict) -> List[Match]:
    """The starter broker's whole plan: bench crossings by quote, then the public offers."""
    return bench_plan(book) + public_plan(book)


# ---------------------------------------------------------------------------------------------------------------------
# 2. Trader model: quote histories -> estimated limits
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Trader:
    """Everything observed about one bench offer id."""
    oid: str
    run: str
    side: str                                   # "sell" | "buy"
    quotes: List[Tuple[int, int]] = field(default_factory=list)   # [(tick, price)], one entry per tick
    first_seen: int = 0
    last_seen: int = 0
    gone: bool = False                          # no longer in the book
    matched: bool = False                       # ... because we matched it (otherwise it left)

    @property
    def first_quote(self) -> int:
        return self.quotes[0][1]

    @property
    def quote(self) -> int:
        return self.quotes[-1][1]

    @property
    def age(self) -> int:
        """Ticks since first seen (0 on the first tick)."""
        return self.last_seen - self.first_seen

    def concessions(self) -> List[int]:
        """Per-tick moves toward the counterparty (positive = relaxing), oldest first. Tightening counts as 0."""
        sign = -1 if self.side == "sell" else 1     # a seller relaxes downwards, a buyer upwards
        prices = [p for _, p in self.quotes]
        return [max(0, sign * (b - a)) for a, b in zip(prices, prices[1:])]


@dataclass
class Estimate:
    """What the model believes about one trader right now."""
    limit: float          # estimated true limit (a seller's cost, a buyer's value)
    reach: float          # the quote we expect it to reach before the session ends (or it leaves)
    kind: str             # "new" (seen once) | "firm" | "relaxer" | "converged"
    impatient: bool       # likely to leave soon: match it while we can


class TraderModel:
    """Tracks every bench offer id across polls and learns, across runs, how bench traders behave:
    the typical shade of a first quote (per side), the typical concession ratio, the tenure of early leavers."""

    def __init__(self) -> None:
        self.traders: Dict[str, Trader] = {}
        self.run_start: Dict[str, int] = {}     # run -> first tick we saw it (session start)
        self.tick: int = 0
        self._learned: Optional[Dict[str, float]] = None   # cache, invalidated by observe()

    # -- observation ---------------------------------------------------------------------------------------------
    def observe(self, tick: int, book: dict) -> None:
        """Fold one book snapshot in. Safe to call several times per tick (the last price of a tick wins)."""
        self.tick = tick
        present: Set[str] = set()
        for o in book.get("bench_offers") or []:
            oid = str(o["id"])
            present.add(oid)
            price = quote_of(o)
            t = self.traders.get(oid)
            if t is None:
                t = self.traders[oid] = Trader(oid, run_of(oid), "sell" if is_seller(o) else "buy",
                                               first_seen=tick, last_seen=tick)
                self.run_start.setdefault(t.run, tick)
            if t.quotes and t.quotes[-1][0] == tick:
                t.quotes[-1] = (tick, price)
            else:
                t.quotes.append((tick, price))
            t.last_seen, t.gone = tick, False
        for t in self.traders.values():
            if not t.gone and t.oid not in present:
                t.gone = True
        self._learned = None

    def note_matched(self, *offer_ids: str) -> None:
        """Our match took these offers: their disappearance is not a departure."""
        for oid in offer_ids:
            t = self.traders.get(str(oid))
            if t is not None:
                t.matched = True

    def session_tick(self, run: str, tick: Optional[int] = None) -> int:
        """Ticks elapsed in this run's session (0 on its first tick)."""
        tick = self.tick if tick is None else tick
        return tick - self.run_start.get(run, tick)

    # -- learned parameters --------------------------------------------------------------------------------------
    def learned(self) -> Dict[str, float]:
        """Medians learned from the traders seen so far, falling back to the priors."""
        if self._learned is not None:
            return self._learned
        shades: Dict[str, List[float]] = {"sell": [], "buy": []}
        ratios: List[float] = []
        tenures: List[int] = []
        live_runs = {t.run for t in self.traders.values() if not t.gone}
        for t in self.traders.values():
            moves = [d for d in t.concessions() if d > 0]
            if len(moves) >= 2:
                r = _fit_ratio(t.concessions())
                if r is not None:
                    ratios.append(r)
                lim = self._relaxer_limit(t, r if r is not None else DEFAULT_RATIO)
                shades[t.side].append(abs(t.first_quote - lim) / max(1, t.first_quote))
            # An unmatched trader that vanished while its run was still live left early: learn that tenure.
            if t.gone and not t.matched and t.run in live_runs:
                tenures.append(t.age + 1)
        self._learned = {
            "shade_sell": statistics.median(shades["sell"]) if shades["sell"] else DEFAULT_SHADE,
            "shade_buy": statistics.median(shades["buy"]) if shades["buy"] else DEFAULT_SHADE,
            "ratio": statistics.median(ratios) if ratios else DEFAULT_RATIO,
            "leave_age": statistics.median(tenures) if tenures else DEFAULT_LEAVE_AGE,
        }
        return self._learned

    # -- estimation ----------------------------------------------------------------------------------------------
    @staticmethod
    def _relaxer_limit(t: Trader, r: float) -> float:
        """Geometric asymptote of the concessions: last quote moved by d_last * r / (1 - r)."""
        steps = t.concessions()
        moves = [d for d in steps if d > 0]
        d_last = steps[-1] if steps and steps[-1] > 0 else (moves[-1] if moves else 0)
        tail = d_last * r / (1 - r)
        return t.quote - tail if t.side == "sell" else t.quote + tail

    def _clamp(self, t: Trader, limit: float) -> float:
        """A limit is never on the near side of the current quote (quotes are shaded away from it) and never
        further than MAX_SHADE from the first quote."""
        if t.side == "sell":
            return min(t.quote, max(limit, t.first_quote * (1 - MAX_SHADE)))
        return max(t.quote, min(limit, t.first_quote * (1 + MAX_SHADE)))

    def estimate(self, oid: str, tick: Optional[int] = None) -> Estimate:
        """Estimated limit, expected reach this session, kind and impatience of one trader."""
        t = self.traders[oid]
        tick = self.tick if tick is None else tick
        p = self.learned()
        sign = -1 if t.side == "sell" else 1
        k = p["shade_sell"] if t.side == "sell" else p["shade_buy"]
        steps = t.concessions()
        moves = [d for d in steps if d > 0]
        idle_tail = len(steps) - max((i + 1 for i, d in enumerate(steps) if d > 0), default=0)
        ticks_left = max(0, SESSION_TICKS - 1 - self.session_tick(t.run, tick))

        if not moves:
            limit = t.quote * (1 + sign * k)
            # Firm or not yet known: count on no movement (what you see is what you get), so v2 never holds a
            # partner for a trader that may turn out firm.
            kind, reach = ("firm" if t.age >= FIRM_AFTER else "new"), float(t.quote)
            rate = 0.0
        elif idle_tail >= FIRM_AFTER:
            kind, limit, reach, rate = "converged", float(t.quote), float(t.quote), 0.0
        else:
            r = _fit_ratio(steps) if len(moves) >= 2 else None
            r = p["ratio"] if r is None else r
            limit = self._relaxer_limit(t, r)
            d_last = steps[-1] if steps[-1] > 0 else moves[-1]
            more = d_last * sum(r ** i for i in range(1, ticks_left + 1))   # geometric moves left this session
            reach = t.quote + sign * more
            kind = "relaxer"
            rate = sum(moves) / max(1, t.first_quote) / max(1, t.age)

        limit = self._clamp(t, limit)
        reach = self._clamp(t, reach)
        reach = max(reach, limit) if t.side == "sell" else min(reach, limit)   # never beyond the limit
        impatient = rate >= FAST_RATE or t.age + 1 >= p["leave_age"] - 1
        return Estimate(limit=limit, reach=reach, kind=kind, impatient=impatient)


def _fit_ratio(steps: Sequence[int]) -> Optional[float]:
    """Concession ratio r of a geometric relaxation, fitted on the last RATIO_WINDOW per-tick moves counted from the
    first real move: sum(later) / sum(earlier). With two moves this is exactly d2 / d1. Clipped to [0, MAX_RATIO]."""
    first = next((i for i, d in enumerate(steps) if d > 0), None)
    if first is None:
        return None
    w = list(steps[first:])[-RATIO_WINDOW:]
    if len(w) < 2 or sum(w[:-1]) <= 0:
        return None
    return min(MAX_RATIO, max(0.0, sum(w[1:]) / sum(w[:-1])))


# ---------------------------------------------------------------------------------------------------------------------
# 3. v2 planner
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class PlanMemory:
    """What v2 remembers between plans: the tick each trader first sat in a crossing pair of the plan (its hold
    clock, which never restarts, so holds cannot cascade), the pairs on hold (they keep the floor bonus, so a hold
    is not undone by a tie in the next book), since when each reservation stands, and the reservations that ran out
    of patience (never made again)."""
    crossing_since: Dict[str, int] = field(default_factory=dict)
    on_hold: Set[Tuple[str, str]] = field(default_factory=set)
    reserved_since: Dict[Tuple[str, str], int] = field(default_factory=dict)
    expired: Set[Tuple[str, str]] = field(default_factory=set)

    def held(self, sell: str, buy: str, tick: int) -> int:
        """Ticks the longer-waiting trader of this crossing pair has been held."""
        return tick - min(self.crossing_since.setdefault(sell, tick), self.crossing_since.setdefault(buy, tick))

    def reserved(self, pair: Tuple[str, str], tick: int) -> int:
        return tick - self.reserved_since.setdefault(pair, tick)


@dataclass
class Decision:
    """v2's output for one book state."""
    matches: List[Match] = field(default_factory=list)
    reasons: Dict[Tuple[str, str], str] = field(default_factory=dict)     # executed pair -> why now
    waiting: List[Tuple[str, str, str]] = field(default_factory=list)     # (sell, buy, "hold"|"reserve")
    fallback_runs: List[str] = field(default_factory=list)                # runs planned by the floor instead

    def as_log(self) -> dict:
        return {"matches": [list(m) + [self.reasons.get(m[:2], "")] for m in self.matches],
                "waiting": [list(w) for w in self.waiting], "fallback_runs": self.fallback_runs}


def v2_plan(book: dict, models: TraderModel, tick: Optional[int] = None,
            memory: Optional[PlanMemory] = None, probe_spread: bool = False) -> Decision:
    """Choose which bench pairs to execute now. `models` must already have observed `book`.

    Per run:
      * estimate every trader's limit and the quote it should reach this session;
      * weight each pair by its estimated gain (value - cost): fully if it crosses on quotes now (plus small bonuses,
        a larger one if the floor would take that very pair, so v2 only departs from the floor on a clear edge),
        discounted by the chance it crosses soon if it does not cross yet but should (reach_buy >= reach_sell);
      * take the maximum-weight matching: it keeps high-value buyers for low-cost sellers and does not spend an
        intramarginal trader on an extramarginal one while an intramarginal partner is expected;
      * execute its crossing pairs now if a trader is impatient or the session is ending, else after HOLD_TICKS;
        keep its not-yet-crossing pairs reserved at most MAX_WAIT ticks;
      * cross any remaining crossing pair between traders the plan does not need (a crossing pair never loses);
      * in the last ENDGAME_TICKS ticks only crossing pairs count, so everything that crosses is matched;
      * if anything goes wrong while planning a run, that run falls back to the floor's crossings.

    `probe_spread` (off by default; an unverified hypothesis) also proposes pairs whose quotes do not cross but whose
    estimated limits do, priced between the estimated limits inside the spread. A refused match costs nothing.
    """
    tick = models.tick if tick is None else tick
    memory = memory if memory is not None else PlanMemory()
    decision = Decision()
    reserved: Set[Tuple[str, str]] = set()
    held_before, memory.on_hold = memory.on_hold, set()

    for run, (sellers, buyers) in bench_runs(book).items():
        try:
            _plan_run(run, sellers, buyers, models, tick, memory, decision, reserved, probe_spread, held_before)
        except Exception:  # noqa: BLE001 - one bad run must not stop the others: cross it by quote
            decision.fallback_runs.append(run)
            for s, b in _quote_crossings(sellers, buyers):
                _execute(decision, s["id"], b["id"], midpoint(quote_of(s), quote_of(b)), "fallback")

    # A reservation's clock only runs while it is renewed plan after plan.
    for pair in list(memory.reserved_since):
        if pair not in reserved:
            del memory.reserved_since[pair]
    return decision


def _plan_run(run: str, sellers: List[dict], buyers: List[dict], models: TraderModel, tick: int,
              memory: PlanMemory, decision: Decision, reserved: Set[Tuple[str, str]], probe_spread: bool,
              held_before: Set[Tuple[str, str]]) -> None:
    endgame = models.session_tick(run, tick) >= SESSION_TICKS - ENDGAME_TICKS
    est = {o["id"]: models.estimate(o["id"], tick) for o in sellers + buyers}
    quote = {o["id"]: quote_of(o) for o in sellers + buyers}
    anchored = {(s["id"], b["id"]) for s, b in _quote_crossings(sellers, buyers)} | held_before

    # Edges: (buyer index, seller index) -> weight = expected realised gain, and the price if executable now.
    weights: Dict[Tuple[int, int], float] = {}
    price_now: Dict[Tuple[int, int], int] = {}
    for i, b in enumerate(buyers):
        for j, s in enumerate(sellers):
            sid, bid_id = s["id"], b["id"]
            ask, bid = quote[sid], quote[bid_id]
            eb, es = est[bid_id], est[sid]
            gain = eb.limit - es.limit
            scale = (ask + bid) / 2
            if bid >= ask:                                        # crosses on quotes: a certain, non-negative gain
                bonus = NOW_BONUS + (FLOOR_BONUS if (sid, bid_id) in anchored else 0.0)
                weights[i, j], price_now[i, j] = max(0.0, gain) + bonus * scale, midpoint(ask, bid)
            elif probe_spread and gain > PROBE_MARGIN * scale and "new" not in (eb.kind, es.kind):
                inside = int(round((eb.limit + es.limit) / 2))    # between the estimated limits, inside the spread
                weights[i, j], price_now[i, j] = gain, min(ask, max(bid, inside))
            elif not endgame and gain > 0 and eb.reach >= es.reach and (sid, bid_id) not in memory.expired:
                if eb.kind == "new" or es.kind == "new":
                    p = P_SOON_NEW
                elif eb.impatient or es.impatient:
                    p = P_SOON_IMPATIENT
                else:
                    p = P_SOON_PATIENT
                if p * gain > RESERVE_MARGIN * scale:
                    weights[i, j] = p * gain - RESERVE_MARGIN * scale

    used: Set[str] = set()
    for i, j in _max_weight_matching(len(buyers), len(sellers), weights):
        b, s = buyers[i]["id"], sellers[j]["id"]
        used.update((s, b))
        if (i, j) in price_now:
            urgent = endgame or est[b].impatient or est[s].impatient
            if urgent or memory.held(s, b, tick) >= HOLD_TICKS:
                why = "endgame" if endgame else "impatient" if urgent else "held"
                _execute(decision, s, b, price_now[i, j], why if quote[b] >= quote[s] else why + "+probe")
            else:
                decision.waiting.append((s, b, "hold"))
                memory.on_hold.add((s, b))
        elif memory.reserved((s, b), tick) >= MAX_WAIT:
            memory.expired.add((s, b))          # waited long enough: free both sides from the next plan on
        else:
            decision.waiting.append((s, b, "reserve"))
            reserved.add((s, b))

    # Crossing pairs among traders the plan does not need: a crossing pair always realises a non-negative gain.
    rest_s = [o for o in sellers if o["id"] not in used]
    rest_b = [o for o in buyers if o["id"] not in used]
    for s, b in _quote_crossings(rest_s, rest_b):
        _execute(decision, s["id"], b["id"], midpoint(quote_of(s), quote_of(b)), "free")


def _execute(decision: Decision, sell: str, buy: str, price: int, reason: str) -> None:
    decision.matches.append((sell, buy, price))
    decision.reasons[(sell, buy)] = reason


def _quote_crossings(sellers: List[dict], buyers: List[dict]) -> List[Tuple[dict, dict]]:
    """The floor's rule on a subset: lowest asks against highest bids while the bid covers the ask."""
    out = []
    for s, b in zip(sorted(sellers, key=quote_of), sorted(buyers, key=lambda o: -quote_of(o))):
        if quote_of(b) < quote_of(s):
            break
        out.append((s, b))
    return out


def _max_weight_matching(n_rows: int, n_cols: int, weights: Dict[Tuple[int, int], float]) -> List[Tuple[int, int]]:
    """Exact maximum-weight bipartite matching for the small books of the Market Test (DP over subsets of the
    smaller side); greedy by weight beyond 16 columns. `weights[(row, col)]` lists the allowed edges."""
    if not weights:
        return []
    if n_cols > 16:
        out, rows, cols = [], set(), set()
        for (i, j), _ in sorted(weights.items(), key=lambda kv: -kv[1]):
            if i not in rows and j not in cols:
                out.append((i, j))
                rows.add(i)
                cols.add(j)
        return out
    adj = [[(j, w) for (i2, j), w in weights.items() if i2 == i] for i in range(n_rows)]
    memo: Dict[Tuple[int, int], Tuple[float, Tuple[Tuple[int, int], ...]]] = {}

    def best(i: int, mask: int) -> Tuple[float, Tuple[Tuple[int, int], ...]]:
        if i == n_rows:
            return 0.0, ()
        key = (i, mask)
        if key not in memo:
            top = best(i + 1, mask)                                 # leave row i unmatched
            for j, w in adj[i]:
                if not mask & (1 << j):
                    v, picks = best(i + 1, mask | (1 << j))
                    if v + w > top[0] + 1e-9:
                        top = (v + w, ((i, j),) + picks)
            memo[key] = top
        return memo[key]

    return list(best(0, 0)[1])


# ---------------------------------------------------------------------------------------------------------------------
# Planner: mode switch, v2 memory and the watchdog (shared by the live loop and the simulator)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class PlanResult:
    matches: List[Tuple[str, str, int, str]]          # (sell, buy, price, source) in execution order
    shadow: Optional[Decision] = None                 # v2's decision when it was computed
    notes: List[str] = field(default_factory=list)


class Planner:
    """Turns a book into the matches to send, according to the mode:
    'floor'  - the starter's plan only;
    'shadow' - the floor's plan, with v2 computed alongside for the journal;
    'v2'     - v2 for the bench, the floor for public offers, and the floor for the bench when v2 raises or stays
               idle WATCHDOG_TICKS ticks while the floor would match."""

    def __init__(self, mode: str = "v2", probe_spread: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.probe_spread = probe_spread
        self.models = TraderModel()
        self.memory = PlanMemory()
        self._idle_since: Optional[int] = None

    def plan(self, tick: int, book: dict) -> PlanResult:
        self.models.observe(tick, book)
        floor_bench = bench_plan(book)
        public = [(s, b, p, "public") for s, b, p in public_plan(book)]
        result = PlanResult(matches=[])

        if self.mode == "floor":
            result.matches = [(s, b, p, "floor") for s, b, p in floor_bench] + public
            return result

        decision: Optional[Decision] = None
        try:
            decision = v2_plan(book, self.models, tick, self.memory, self.probe_spread)
        except Exception as e:  # noqa: BLE001 - the watchdog's job is precisely to survive this
            result.notes.append(f"v2 raised {type(e).__name__}: {e}")
        result.shadow = decision

        if self.mode == "shadow":
            result.matches = [(s, b, p, "floor") for s, b, p in floor_bench] + public
            return result

        if decision is None:
            bench = [(s, b, p, "watchdog-error") for s, b, p in floor_bench]
        elif self._idle(tick, decision, floor_bench):
            result.notes.append(f"v2 idle {WATCHDOG_TICKS} ticks while the floor would match: floor")
            bench = [(s, b, p, "watchdog-idle") for s, b, p in floor_bench]
        else:
            bench = [(s, b, p, "v2:" + decision.reasons.get((s, b), "")) for s, b, p in decision.matches]
        result.matches = bench + public
        return result

    def _idle(self, tick: int, decision: Decision, floor_bench: List[Match]) -> bool:
        """True once v2 has neither matched nor deliberately waited for WATCHDOG_TICKS ticks in which the floor
        had crossings. Deliberate waits (hold / reserve) are v2 working, not v2 stuck."""
        if decision.matches or decision.waiting or not floor_bench:
            self._idle_since = None
            return False
        if self._idle_since is None:
            self._idle_since = tick
        return tick - self._idle_since + 1 >= WATCHDOG_TICKS

    def note_matched(self, sell: str, buy: str) -> None:
        self.models.note_matched(sell, buy)


# ---------------------------------------------------------------------------------------------------------------------
# 4. Live loop
# ---------------------------------------------------------------------------------------------------------------------
def append_jsonl(path: str, record: dict) -> None:
    """Append one JSON line; journaling must never take the broker down."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    except OSError as e:
        print(f"journal {path}: {e}", file=sys.stderr)


def heartbeat(path: str, **info: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), **info}, f)
    except OSError:
        pass


def read_broker_key() -> str:
    key = os.environ.get("BROKER_KEY", "").strip()
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise SystemExit(f"no broker key: export BROKER_KEY or write it to {KEY_FILE}")
    return key


def book_fingerprint(tick: int, book: dict) -> tuple:
    """One state of the book: we plan once per state, so a refused match is not resent every poll."""
    offers = (book.get("bench_offers") or []) + (book.get("offers") or [])
    return tick, tuple(sorted((str(o.get("id")), json.dumps(o.get("want")), json.dumps(o.get("give")))
                              for o in offers))


def run(broker_key: Optional[str] = None, mode: str = "shadow", *, client: Any = None, poll: float = POLL_SECONDS,
        dry_run: bool = False, probe_spread: bool = False, max_loops: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        books_log: str = BOOKS_LOG, matches_log: str = MATCHES_LOG, heartbeat_file: str = HEARTBEAT_FILE,
        rate_limit: bool = True) -> Planner:
    """The broker's main loop. Never returns on its own (unless `max_loops`); every error is logged and survived.

    `client` defaults to the SDK's Broker via bazaar.api.broker_client; tests pass a fake one."""
    limiter = None
    if client is None:
        from bazaar.api import TokenBucket, broker_client   # lazy: the planner itself needs no network stack
        client = broker_client(broker_key or read_broker_key())
        limiter = TokenBucket(4.5, 10) if rate_limit else None   # 5 req/s per key; keep a margin

    def call(fn: Callable[..., Any], *a: Any) -> Any:
        if limiter is not None:
            limiter.take()
        return fn(*a)

    planner = Planner(mode, probe_spread)
    seen: Optional[tuple] = None
    refused: Dict[Tuple[str, str], Tuple[int, int, int]] = {}    # pair -> (ask, bid, price) it was refused at
    sent_tick, sent_count = -1, 0
    failures, loops = 0, 0
    print(f"broker: mode={mode} dry_run={dry_run} probe_spread={probe_spread}", flush=True)

    while max_loops is None or loops < max_loops:
        loops += 1
        heartbeat(heartbeat_file, mode=mode, tick=planner.models.tick, failures=failures, pid=os.getpid())
        try:
            tick = int(call(client.clock)["tick"])
            book = call(client.book)
            failures = 0
        except Exception as e:  # noqa: BLE001 - the server restarting, a network blip: keep going
            failures += 1
            print(f"cannot read the book ({type(e).__name__}: {e}), retrying", flush=True)
            sleep(min(10.0, poll * 2 ** min(failures, 4)))
            continue

        state = book_fingerprint(tick, book)
        if state == seen:
            sleep(poll)
            continue
        seen = state
        if tick != sent_tick:
            sent_tick, sent_count = tick, 0

        bench = book.get("bench_offers") or []
        if bench:
            append_jsonl(books_log, {"ts": time.time(), "tick": tick, "runs": sorted({run_of(o["id"]) for o in bench}),
                                     "bench_offers": bench})

        try:
            result = planner.plan(tick, book)
        except Exception as e:  # noqa: BLE001 - last resort: the starter's plan, as is
            result = PlanResult(matches=[(s, b, p, "panic-floor") for s, b, p in floor_plan(book)],
                                notes=[f"planner raised {type(e).__name__}: {e}"])
        for note in result.notes:
            print(f"tick {tick}: {note}", flush=True)
        if result.shadow is not None and bench:
            append_jsonl(matches_log, {"ts": time.time(), "tick": tick, "kind": "v2_decision", "mode": mode,
                                       "v2": result.shadow.as_log(),
                                       "floor": [list(m) for m in bench_plan(book)]})

        quotes = {str(o["id"]): quote_of(o) for o in bench}
        quotes.update({str(o["id"]): (o["want"].get("cash") or o["give"].get("cash") or 0)
                       for o in book.get("offers") or []})
        for sell, buy, price, source in result.matches:
            if sent_count >= MAX_MATCHES_PER_TICK:
                break
            at = (quotes.get(str(sell), 0), quotes.get(str(buy), 0))
            if refused.get((sell, buy)) == at + (price,):
                continue                                  # refused on these very quotes and price: do not insist
            rec = {"ts": time.time(), "tick": tick, "kind": "match", "mode": mode, "source": source,
                   "sell": sell, "buy": buy, "price": price, "ask": at[0], "bid": at[1]}
            if dry_run:
                append_jsonl(matches_log, {**rec, "ok": None, "dry_run": True})
                continue
            sent_count += 1
            try:
                call(client.match, sell, buy, price)
                planner.note_matched(sell, buy)
                append_jsonl(matches_log, {**rec, "ok": True})
            except Exception as e:  # noqa: BLE001 - an offer taken since the read, a shape the venue cannot cross
                refused[(sell, buy)] = at + (price,)
                append_jsonl(matches_log, {**rec, "ok": False, "error": getattr(e, "code", type(e).__name__),
                                           "message": str(e)[:300]})
                print(f"tick {tick}: {sell} x {buy} at {price} refused ({e})", flush=True)
        sleep(poll)
    return planner


# ---------------------------------------------------------------------------------------------------------------------
# 5. Offline evaluator
# ---------------------------------------------------------------------------------------------------------------------
def possible_gains(true_limits: Dict[str, float], sides: Dict[str, str]) -> float:
    """Maximum total gain between true limits, per run: values sorted down against costs sorted up."""
    total = 0.0
    runs: Dict[str, Tuple[List[float], List[float]]] = {}
    for oid, lim in true_limits.items():
        costs, values = runs.setdefault(run_of(oid), ([], []))
        (costs if sides[oid] == "sell" else values).append(lim)
    for costs, values in runs.values():
        for c, v in zip(sorted(costs), sorted(values, reverse=True)):
            if v <= c:
                break
            total += v - c
    return total


def simulate(book_sequence: Iterable[Tuple[int, dict]], true_limits: Dict[str, float], mode: str = "v2",
             planner: Optional[Planner] = None, accept: str = "quote") -> dict:
    """Replay `book_sequence` ([(tick, book)], one book per tick, as the market would show it if nobody matched)
    against a planner and score the share of possible gains realised at the true limits.

    Offers we match vanish from later books, as they would live. A match is valid, as the venue requires, when
    both offers are present and unused, belong to the same run, are a seller and a buyer, and the price satisfies
    the acceptance rule: accept="quote" (assumed live) ask <= price <= bid; accept="limit" (the hypothesis behind
    probe_spread) cost <= price <= value at the true limits. At most MAX_MATCHES_PER_TICK accepted matches per tick.
    Refused proposals are counted, not executed."""
    planner = planner or Planner(mode)
    taken: Set[str] = set()
    sides: Dict[str, str] = {}
    realised, invalid, matches = 0.0, 0, []
    for tick, book in book_sequence:
        offers = [o for o in book.get("bench_offers") or [] if o["id"] not in taken]
        for o in book.get("bench_offers") or []:
            sides[o["id"]] = "sell" if is_seller(o) else "buy"
        live = {o["id"]: o for o in offers}
        result = planner.plan(tick, {**book, "bench_offers": offers, "offers": []})
        done = 0
        for sell, buy, price, _ in result.matches:
            s, b = live.get(sell), live.get(buy)
            ok = (done < MAX_MATCHES_PER_TICK and s is not None and b is not None and sell not in taken
                  and buy not in taken and run_of(sell) == run_of(buy) and is_seller(s) and not is_seller(b))
            if ok and accept == "quote":
                ok = quote_of(s) <= price <= quote_of(b)
            elif ok:
                ok = true_limits[sell] <= price <= true_limits[buy]
            if not ok:
                invalid += 1
                continue
            taken.update((sell, buy))
            planner.note_matched(sell, buy)
            realised += true_limits[buy] - true_limits[sell]
            matches.append((tick, sell, buy, price))
            done += 1
    possible = possible_gains({k: v for k, v in true_limits.items() if k in sides}, sides)
    return {"efficiency": realised / possible if possible > 0 else 1.0, "realised": realised,
            "possible": possible, "matches": matches, "invalid": invalid}


# ---------------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python3 -m bazaar.broker", description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=MODES, default="shadow",
                    help="floor: starter only; shadow: floor + logged v2; v2: v2 with floor fallback")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS, help="seconds between book reads")
    ap.add_argument("--dry-run", action="store_true", help="read and plan, but send no match (journaled)")
    ap.add_argument("--probe-spread", action="store_true",
                    help="v2 also proposes pairs whose limits (not quotes) cross; verify the server accepts them first")
    args = ap.parse_args(argv)
    try:
        run(read_broker_key(), args.mode, poll=args.poll, dry_run=args.dry_run, probe_spread=args.probe_spread)
    except KeyboardInterrupt:
        print("broker stopped")


if __name__ == "__main__":
    main()
