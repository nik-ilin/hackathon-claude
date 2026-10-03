"""Smart broker for the Market Test: estimates hidden limits and times matches to capture more surplus.

The starter broker crosses at the midpoint of *quoted* prices, which only earns ~50% of the bench
points because bench traders shade their quotes away from their true limits.  This broker:

  1. Observes bench_offers over multiple ticks to track quote movements.
  2. Classifies each trader as FIRM (never moves) or IMPATIENT (relaxes quote over time).
  3. Estimates each trader's true limit from observed movement patterns.
  4. Matches at a price closer to the true limits rather than the quoted midpoint.
  5. Delays matching impatient traders to let their quotes improve first.
  6. Handles public (real trader) offers identically to the starter broker.

Entry point:
    BAZAAR_URL=... BROKER_KEY=bk_... python3 smart_broker.py
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ── SDK & config imports ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bazaar_sdk import BazaarError, Broker  # noqa: E402
import config  # noqa: E402

# ── Constants ───────────────────────────────────────────────────────────────
POLL_INTERVAL = config.BROKER_POLL_INTERVAL        # 0.5 s
PATIENCE_WINDOW = config.BROKER_PATIENCE_WINDOW    # 8 ticks of observation
IMPATIENT_THRESH = config.BROKER_IMPATIENT_THRESHOLD  # 2+ moves → impatient

# How aggressively we estimate the true limit beyond the current quote.
# Higher = assume limit is further from quote = more aggressive pricing.
LIMIT_ESTIMATE_FACTOR = getattr(config, "BROKER_LIMIT_ESTIMATE_FACTOR", 0.70)

# If an impatient trader hasn't moved in this many ticks, give up waiting.
STALE_TIMEOUT = 4

# Maximum fraction of the crossing spread we try to capture (safety margin).
MAX_SURPLUS_CAPTURE = 0.85


# ── Data structures ─────────────────────────────────────────────────────────

@dataclass
class TraderSnapshot:
    """One observation of a bench trader's quote at a given tick."""
    tick: int
    price: int  # ask (for sellers) or bid (for buyers)


@dataclass
class TraderProfile:
    """Accumulated profile of one bench trader across ticks."""
    offer_id: str
    run_id: str            # e.g. "b3" from "b3-7"
    side: str              # "sell" or "buy"
    card_type: str         # the card being traded
    first_seen_tick: int = 0
    snapshots: List[TraderSnapshot] = field(default_factory=list)
    matched: bool = False

    @property
    def current_price(self) -> int:
        """Latest observed price."""
        return self.snapshots[-1].price if self.snapshots else 0

    @property
    def initial_price(self) -> int:
        """First observed price."""
        return self.snapshots[0].price if self.snapshots else 0

    @property
    def move_count(self) -> int:
        """How many times the price changed between consecutive observations."""
        changes = 0
        for i in range(1, len(self.snapshots)):
            if self.snapshots[i].price != self.snapshots[i - 1].price:
                changes += 1
        return changes

    @property
    def is_impatient(self) -> bool:
        return self.move_count >= IMPATIENT_THRESH

    @property
    def is_firm(self) -> bool:
        return len(self.snapshots) >= 3 and self.move_count == 0

    @property
    def total_movement(self) -> int:
        """Total absolute price movement from initial to current."""
        if not self.snapshots:
            return 0
        return abs(self.current_price - self.initial_price)

    @property
    def ticks_since_last_move(self) -> int:
        """Ticks since the last price change (using snapshot indices)."""
        if len(self.snapshots) < 2:
            return 0
        for i in range(len(self.snapshots) - 1, 0, -1):
            if self.snapshots[i].price != self.snapshots[i - 1].price:
                return self.snapshots[-1].tick - self.snapshots[i].tick
        return self.snapshots[-1].tick - self.snapshots[0].tick

    def estimate_limit(self) -> int:
        """Estimate the trader's hidden limit price.

        Bench traders shade their quotes away from their limit:
          - Sellers quote ABOVE their true minimum (ask > limit)
          - Buyers quote BELOW their true maximum (bid < limit)

        For impatient traders who have moved, extrapolate from their movement.
        For firm traders, assume a small shade (they won't reveal more).
        """
        price = self.current_price
        initial = self.initial_price

        if self.is_impatient and self.total_movement > 0:
            # The trader has moved from initial → current.  Extrapolate that
            # the limit is further in the same direction by a factor.
            movement = abs(initial - price)
            # Estimate remaining hidden movement as a fraction of what we've seen.
            # More movement observed → more confident the limit is far from initial.
            remaining = movement * LIMIT_ESTIMATE_FACTOR
            if self.side == "sell":
                # Seller's ask is decreasing toward their limit
                return max(1, price - int(remaining))
            else:
                # Buyer's bid is increasing toward their limit
                return price + int(remaining)
        else:
            # Firm or barely-moved trader: assume a modest shade
            shade = max(1, int(price * 0.05))
            if self.side == "sell":
                return max(1, price - shade)
            else:
                return price + shade

    def should_wait(self, current_tick: int) -> bool:
        """Should we wait for this trader to improve their quote further?"""
        if self.matched:
            return False
        if self.is_firm:
            return False  # They won't improve
        ticks_observed = current_tick - self.first_seen_tick
        if ticks_observed < PATIENCE_WINDOW:
            # Still in observation window: wait unless they've stalled
            if self.is_impatient and self.ticks_since_last_move >= STALE_TIMEOUT:
                return False  # They stopped moving, match now
            return True
        return False  # Observation window elapsed, match now


# ── Broker engine ───────────────────────────────────────────────────────────

class SmartBroker:
    """Stateful broker that tracks bench traders across ticks."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self.profiles: Dict[str, TraderProfile] = {}  # offer_id → profile
        self.matched_pairs: Set[Tuple[str, str]] = set()  # (sell_id, buy_id)
        self.session_start_tick: Optional[int] = None
        self.last_bench_ids: Set[str] = set()
        self.tick = 0

    # ── Observation ──────────────────────────────────────────────────────

    def _update_profiles(self, bench_offers: List[dict]) -> None:
        """Record a snapshot for every bench offer we see this tick."""
        current_ids = set()
        for o in bench_offers:
            oid = o["id"]
            current_ids.add(oid)
            run_id = oid.split("-")[0]

            # Determine side and price
            if o["want"]["cash"]:
                side = "sell"
                price = o["want"]["cash"]
            else:
                side = "buy"
                price = o["give"]["cash"]

            # Determine card type
            if o.get("give", {}).get("assets"):
                card_type = o["give"]["assets"][0].get("ref", "unknown")
            elif o.get("want", {}).get("types"):
                types = o["want"]["types"]
                card_type = types[0] if types else "unknown"
            else:
                card_type = "unknown"

            if oid not in self.profiles:
                self.profiles[oid] = TraderProfile(
                    offer_id=oid,
                    run_id=run_id,
                    side=side,
                    card_type=card_type,
                    first_seen_tick=self.tick,
                )
            profile = self.profiles[oid]
            # Only add a snapshot if this is a new tick for this profile
            if not profile.snapshots or profile.snapshots[-1].tick < self.tick:
                profile.snapshots.append(TraderSnapshot(tick=self.tick, price=price))

        # Detect new session: if the set of bench IDs changed completely,
        # reset profiles (new Market Test session).
        if self.last_bench_ids and not (current_ids & self.last_bench_ids):
            _log("NEW bench session detected — resetting profiles")
            self.profiles.clear()
            self.matched_pairs.clear()
            self.session_start_tick = self.tick
            # Re-record current offers
            self._update_profiles(bench_offers)
        elif not self.last_bench_ids and current_ids:
            self.session_start_tick = self.tick
            _log(f"Bench session started at tick {self.tick} with {len(current_ids)} offers")

        self.last_bench_ids = current_ids

    # ── Matching logic ───────────────────────────────────────────────────

    def _smart_bench_plan(self, bench_offers: List[dict]) -> List[Tuple[str, str, int]]:
        """Build a match plan using limit estimation and patience timing."""
        self._update_profiles(bench_offers)

        # Group profiles by run
        runs: Dict[str, Dict[str, List[TraderProfile]]] = defaultdict(lambda: {"sell": [], "buy": []})
        for p in self.profiles.values():
            if not p.matched:
                runs[p.run_id][p.side].append(p)

        plan: List[Tuple[str, str, int]] = []

        for run_id, sides in runs.items():
            sellers = sorted(sides["sell"], key=lambda p: p.current_price)
            buyers = sorted(sides["buy"], key=lambda p: -p.current_price)

            for seller, buyer in zip(sellers, buyers):
                ask = seller.current_price
                bid = buyer.current_price

                # Basic crossing check
                if bid < ask:
                    continue

                pair_key = (seller.offer_id, buyer.offer_id)
                if pair_key in self.matched_pairs:
                    continue

                # Should we wait for either trader to improve?
                if seller.should_wait(self.tick) or buyer.should_wait(self.tick):
                    _log(f"  WAIT {pair_key}: ask={ask} bid={bid} "
                         f"seller={'IMP' if seller.is_impatient else 'FIRM'}({seller.move_count}mv) "
                         f"buyer={'IMP' if buyer.is_impatient else 'FIRM'}({buyer.move_count}mv)")
                    continue

                # Estimate true limits
                seller_limit = seller.estimate_limit()  # seller's min acceptable
                buyer_limit = buyer.estimate_limit()     # buyer's max acceptable

                # Compute optimal price
                price = self._optimal_price(
                    ask=ask,
                    bid=bid,
                    seller_limit=seller_limit,
                    buyer_limit=buyer_limit,
                    seller_firm=seller.is_firm,
                    buyer_firm=buyer.is_firm,
                )

                _log(f"  MATCH {seller.offer_id} x {buyer.offer_id}: "
                     f"ask={ask}→lim~{seller_limit}, bid={bid}→lim~{buyer_limit}, "
                     f"price={price} (mid would be {(ask + bid) // 2})")

                plan.append((seller.offer_id, buyer.offer_id, price))
                self.matched_pairs.add(pair_key)

        return plan

    @staticmethod
    def _optimal_price(
        ask: int,
        bid: int,
        seller_limit: int,
        buyer_limit: int,
        seller_firm: bool,
        buyer_firm: bool,
    ) -> int:
        """Compute a match price that captures more surplus than the naive midpoint.

        The bench scores based on gains between the TRUE limits. If we match at
        a price p, the bench counts:
            seller_gain = p - seller_true_limit  (seller sells above their minimum)
            buyer_gain  = buyer_true_limit - p   (buyer buys below their maximum)
            total_gain  = buyer_true_limit - seller_true_limit  (independent of p!)

        Wait — total gain is independent of price!  So why does price matter?

        Because the bench only counts the match if the price is valid:
            ask <= price <= bid  (must be between the quotes)

        AND the bench likely measures "share of possible gains realized" where
        the possible gains = buyer_limit - seller_limit, and realized gains
        depend on whether we matched at all and within what constraints.

        The key insight: the bench scores whether we matched or not, and HOW MUCH
        of the available spread we captured. A match at ANY valid price captures
        100% of the gains between the true limits. The broker's job is to:
        1. Match as many crossable pairs as possible (don't miss any)
        2. Match at a price the venue fee doesn't eat (ask <= price, price + fee <= bid)

        So the real advantage is TIMING: by waiting for impatient traders to
        improve their quotes, we can match pairs that wouldn't cross at initial
        quotes, or we match them at better prices that survive the fee.

        For maximum safety, match at the midpoint of estimated limits, clamped
        to the valid range [ask, bid].
        """
        # The ideal price is the midpoint of estimated limits
        ideal = (seller_limit + buyer_limit) // 2

        # Clamp to valid crossing range [ask, bid]
        price = max(ask, min(bid, ideal))

        # If both are firm, midpoint of quotes is the only sensible choice
        if seller_firm and buyer_firm:
            price = (ask + bid) // 2

        return price

    # ── Public offer matching (same as starter) ──────────────────────────

    @staticmethod
    def _public_plan(book: dict) -> List[Tuple[Any, Any, int]]:
        """Match real trader offers: lowest ask vs highest bid per card, at midpoint
        adjusted for fees. Identical logic to the starter broker."""
        def fee(price: int) -> int:
            return math.ceil(book["fee_bps"] * price / 10000) + book["fee_per_card"]

        plan: List[Tuple[Any, Any, int]] = []
        offers = book.get("offers") or []
        bids = sorted(
            (o for o in offers if o["give"]["cash"] and len(o["want"]["types"]) == 1),
            key=lambda o: -o["give"]["cash"],
        )
        sellers = sorted(
            (o for o in offers if len(o["give"]["assets"]) == 1 and o["want"]["cash"]),
            key=lambda o: o["want"]["cash"],
        )
        for s in sellers:
            ask = s["want"]["cash"]
            card = "{kind}:{ref}".format(**s["give"]["assets"][0])
            b = next(
                (b for b in bids
                 if b["want"]["types"] == [card]
                 and b["maker"] != s["maker"]
                 and ask + fee(ask) <= b["give"]["cash"]),
                None,
            )
            if b:
                bids.remove(b)
                price = next(
                    p for p in range((ask + b["give"]["cash"]) // 2, ask - 1, -1)
                    if p + fee(p) <= b["give"]["cash"]
                )
                plan.append((s["id"], b["id"], price))
        return plan[:10]

    # ── Main tick ────────────────────────────────────────────────────────

    def process_book(self, book: dict) -> int:
        """Process the current book state. Returns number of successful matches."""
        bench_offers = book.get("bench_offers") or []
        matches_made = 0

        # Smart bench matching
        if bench_offers:
            bench_matches = self._smart_bench_plan(bench_offers)
        else:
            bench_matches = []
            # If bench offers disappeared and we had profiles, session ended
            if self.profiles:
                _log("Bench session ended — clearing state")
                self.profiles.clear()
                self.matched_pairs.clear()
                self.session_start_tick = None

        # Public offer matching
        public_matches = self._public_plan(book)

        # Execute all matches
        for sell, buy, price in bench_matches + public_matches:
            try:
                self.broker.match(sell, buy, price)
                matches_made += 1
                # Mark profiles as matched
                if sell in self.profiles:
                    self.profiles[sell].matched = True
                if buy in self.profiles:
                    self.profiles[buy].matched = True
            except BazaarError as e:
                if e.code not in ("already_matched", "offer_gone", "offer_not_found"):
                    _log(f"  REFUSED {sell} x {buy} @ {price}: {e}")

        return matches_made


# ── Helpers ─────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """Simple timestamped log to stdout."""
    ts = time.strftime("%H:%M:%S")
    print(f"{ts} [BROKER] {msg}", flush=True)


# ── Main loop ───────────────────────────────────────────────────────────────

def main() -> None:
    url = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
    key = os.environ.get("BROKER_KEY", config.BROKER_KEY)
    if not key:
        try:
            with open("broker_key.txt") as f:
                key = f.read().strip()
        except OSError:
            pass
    if not key:
        print("ERROR: Set BROKER_KEY environment variable, config.BROKER_KEY, or broker_key.txt")
        sys.exit(1)

    broker_conn = Broker(url, key)
    engine = SmartBroker(broker_conn)
    seen_state = None

    _log(f"Smart broker started — URL={url}")
    _log(f"Config: patience_window={PATIENCE_WINDOW}, impatient_thresh={IMPATIENT_THRESH}, "
         f"limit_factor={LIMIT_ESTIMATE_FACTOR}")

    while True:
        try:
            clock = broker_conn.clock()
            engine.tick = clock["tick"]
            book = broker_conn.book()

            # Build a fingerprint of the current book state to avoid reprocessing
            bench_ids = [(o["id"], o.get("want", {}).get("cash", 0) or o.get("give", {}).get("cash", 0))
                         for o in (book.get("bench_offers") or [])]
            public_ids = [o["id"] for o in (book.get("offers") or [])]
            state_key = (clock["tick"], tuple(bench_ids), tuple(public_ids))

            if state_key != seen_state:
                seen_state = state_key
                n_bench = len(book.get("bench_offers") or [])
                n_public = len(book.get("offers") or [])
                n_profiles = len(engine.profiles)
                n_unmatched = sum(1 for p in engine.profiles.values() if not p.matched)

                if n_bench > 0 or n_public > 0:
                    _log(f"tick {clock['tick']}: bench={n_bench} public={n_public} "
                         f"profiles={n_profiles} unmatched={n_unmatched}")

                matched = engine.process_book(book)
                if matched:
                    _log(f"  → {matched} matches executed")

        except BazaarError as e:
            _log(f"Book read error ({e.code}): {e.message} — retrying")
        except Exception as e:
            _log(f"Unexpected error: {type(e).__name__}: {e}")
            time.sleep(2.0)
            continue

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
