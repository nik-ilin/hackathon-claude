"""DuelEngine: handles all 1v1 scheduled duel negotiations.

Duels are timed sessions where every team faces every other team once as buyer and
once as seller. The value of the deal shrinks with each round of talk, so the engine
tries to close in 3-5 rounds. Later sessions add a delivery-day dimension.
"""
from __future__ import annotations

import random
from typing import Any, Optional

import config
import logger as log
from bazaar_sdk import BazaarError

# ── Message variety (avoids looking robotic) ─────────────────────────────────

_BUYER_OPENERS = [
    "Let's make a deal! I'll start at {price}.",
    "I'm interested. How about {price}?",
    "Good to meet you — I'm thinking {price}.",
    "I'd like to buy. {price} to start?",
    "Hello! I'll offer {price} for this.",
]

_BUYER_CONCESSIONS = [
    "I can come up to {price}.",
    "Alright, how about {price}?",
    "Let me raise my offer to {price}.",
    "I'll go to {price} — that's fair.",
    "Fine, {price} works for me.",
    "I want this to work — {price}.",
    "{price} is my best so far.",
]

_SELLER_OPENERS = [
    "I'd sell for {price}.",
    "My starting price is {price}.",
    "Let's begin at {price}.",
    "I'm looking for {price}.",
    "This is worth at least {price} to me.",
]

_SELLER_CONCESSIONS = [
    "I could come down to {price}.",
    "How about {price} then?",
    "I'll lower to {price}.",
    "Let me offer {price}.",
    "Alright, {price} is reasonable.",
    "I can accept {price}.",
    "{price} — final-ish from me.",
]

_DAYS_PHRASES = [
    " Delivery on day {days}.",
    " I'd prefer day {days} for delivery.",
    " Day {days} works best for me.",
    " Let's say delivery day {days}.",
]


class _DuelState:
    """Tracks negotiation state for one active duel."""

    __slots__ = (
        "duel_id", "role", "your_limit", "issues", "your_days_weight",
        "round_num", "our_last_price", "our_last_days", "rival_last_price",
        "rival_last_days", "prices_sent", "accepted", "best_day_for_us",
        "worst_day_for_us", "deadline",
    )

    def __init__(self, duel_id: int, role: str, your_limit: float,
                 issues: list[str], your_days_weight: Optional[dict] = None,
                 deadline: Optional[int] = None):
        self.duel_id = duel_id
        self.role = role                 # "buyer" or "seller"
        self.your_limit = your_limit
        self.issues = issues
        self.your_days_weight = your_days_weight or {}
        self.deadline = deadline
        self.round_num = 0
        self.our_last_price: Optional[int] = None
        self.our_last_days: Optional[int] = None
        self.rival_last_price: Optional[int] = None
        self.rival_last_days: Optional[int] = None
        self.prices_sent: set[int] = set()
        self.accepted = False

        # Pre-compute day preferences
        if self.your_days_weight:
            sorted_days = sorted(
                self.your_days_weight.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )
            self.best_day_for_us = int(sorted_days[0][0]) if sorted_days else 5
            self.worst_day_for_us = int(sorted_days[-1][0]) if sorted_days else 5
        else:
            self.best_day_for_us = 5
            self.worst_day_for_us = 5


class DuelEngine:
    """Manages all 1v1 duel negotiations.

    Call tick() once per game tick. It will:
      1. Fetch active duels.
      2. For each duel, determine role and continue negotiation.
      3. Accept if the rival's offer is within threshold.
      4. Try to close in 3-5 rounds to minimize pie shrinkage.
      5. Handle multi-issue duels (price + delivery days).
      6. Log results.
    """

    def __init__(self, state: Any):
        self.state = state
        # duel_id -> _DuelState
        self._active: dict[int, _DuelState] = {}
        # Completed duel results
        self._results: list[dict] = []
        # Phrase rotation indices
        self._phrase_idx: int = 0

    # ── Public API ───────────────────────────────────────────────────────

    def tick(self) -> None:
        """Main entry point — called once per game tick."""
        try:
            duels_data = self.state.b.duels()
            duels = duels_data.get("duels", []) if isinstance(duels_data, dict) else duels_data
        except Exception as exc:
            log.warn(f"DuelEngine: failed to fetch duels: {exc}")
            return

        # Track which duel IDs are still active
        active_ids = set()

        for duel in duels:
            try:
                duel_id = duel.get("id")
                if duel_id is None:
                    continue
                status = duel.get("status", "active")
                if status not in ("active", "open", "negotiating", ""):
                    # Duel is finished — clean up and record
                    self._finish_duel(duel_id, duel)
                    continue

                active_ids.add(duel_id)
                self._process_duel(duel)
            except BazaarError as exc:
                if exc.code == "wait_for_tick":
                    continue
                log.warn(f"Duel {duel.get('id', '?')} error: {exc}")
            except Exception as exc:
                log.warn(f"Duel {duel.get('id', '?')} unexpected error: {exc}")

        # Clean up stale entries
        stale = [did for did in self._active if did not in active_ids]
        for did in stale:
            self._finish_duel(did)

    # ── Duel processing ──────────────────────────────────────────────────

    def _process_duel(self, duel: dict) -> None:
        """Process a single active duel: read rival's offer, decide response."""
        duel_id = duel["id"]
        role = duel.get("role", "buyer")
        your_limit = float(duel.get("your_limit", 0))
        issues = duel.get("issues", ["price"])
        your_days_weight = duel.get("your_days_weight", {})
        rival_offer = duel.get("rival_offer")
        messages = duel.get("messages", [])

        # Get or create state
        if duel_id not in self._active:
            ds = _DuelState(
                duel_id=duel_id,
                role=role,
                your_limit=your_limit,
                issues=issues,
                your_days_weight=your_days_weight,
                deadline=duel.get("deadline"),
            )
            self._active[duel_id] = ds
            log.duel(
                f"New duel {duel_id}: role={role}, limit={your_limit}, "
                f"issues={issues}, deadline={ds.deadline}"
            )
        ds = self._active[duel_id]
        if ds.deadline is None and duel.get("deadline") is not None:
            ds.deadline = duel.get("deadline")

        if ds.accepted:
            return  # already accepted, waiting for settlement

        # ── Parse rival's latest offer ──────────────────────────────────
        if rival_offer:
            rival_price = rival_offer.get("price")
            rival_days = rival_offer.get("days")
            if rival_price is not None:
                try:
                    ds.rival_last_price = int(rival_price)
                except (ValueError, TypeError):
                    pass
            if rival_days is not None:
                try:
                    ds.rival_last_days = int(rival_days)
                except (ValueError, TypeError):
                    pass

        # Also scan messages for rival price info
        rival_msgs = [m for m in messages if m.get("from") != "me"]
        if rival_msgs:
            latest_rival = rival_msgs[-1]
            rp = latest_rival.get("price")
            rd = latest_rival.get("days")
            if rp is not None:
                try:
                    ds.rival_last_price = int(rp)
                except (ValueError, TypeError):
                    pass
            if rd is not None:
                try:
                    ds.rival_last_days = int(rd)
                except (ValueError, TypeError):
                    pass

        # ── Check if we should accept rival's offer ─────────────────────
        if ds.rival_last_price is not None:
            if self._should_accept(ds):
                try:
                    self.state.b.duel_accept(duel_id)
                    ds.accepted = True
                    log.duel(
                        f"Accepted duel {duel_id} at rival price "
                        f"{ds.rival_last_price} (limit {ds.your_limit})"
                    )
                    return
                except BazaarError as exc:
                    if exc.code == "wait_for_tick":
                        return
                    log.warn(f"Failed to accept duel {duel_id}: {exc}")

        # ── Compute and send our next offer ─────────────────────────────
        price = self._compute_price(ds)
        days = self._compute_days(ds) if "days" in ds.issues else None

        # Avoid repeating exact same price
        attempts = 0
        while price in ds.prices_sent and attempts < 10:
            if ds.role == "buyer":
                price += 1
            else:
                price -= 1
            attempts += 1

        text = self._make_phrase(ds, price, days)

        try:
            self.state.b.duel_say(duel_id, text=text, price=price, days=days)
            ds.prices_sent.add(price)
            ds.our_last_price = price
            ds.our_last_days = days
            ds.round_num += 1
            log.duel(
                f"[Duel {duel_id}] Round {ds.round_num}: "
                f"{'offered' if ds.role == 'buyer' else 'asked'} {price}"
                f"{f', day {days}' if days is not None else ''}"
                f" (rival: {ds.rival_last_price})"
            )
        except BazaarError as exc:
            if exc.code == "wait_for_tick":
                pass  # try next tick
            elif exc.code == "missing_days" and "days" in ds.issues:
                # Retry with days
                days = days if days is not None else ds.best_day_for_us
                try:
                    self.state.b.duel_say(duel_id, text=text, price=price, days=days)
                    ds.prices_sent.add(price)
                    ds.our_last_price = price
                    ds.our_last_days = days
                    ds.round_num += 1
                except BazaarError:
                    pass
            else:
                log.warn(f"Duel {duel_id} say error: {exc}")

    # ── Price strategy ───────────────────────────────────────────────────

    def _compute_price(self, ds: _DuelState) -> int:
        """Compute next price offer using concession strategy.

        Buyer: start at 55% of limit, escalate by 15% of gap each round.
        Seller: start at 150% of limit, decrease by 15% of gap each round.
        """
        limit = ds.your_limit

        if ds.role == "buyer":
            if ds.round_num == 0:
                return max(1, int(limit * config.DUEL_BUYER_START))
            # Escalate toward limit
            current = ds.our_last_price or int(limit * config.DUEL_BUYER_START)
            gap = limit - current
            step = max(1, int(gap * config.DUEL_STEP_RATIO))
            # Accelerate if we've been going too many rounds
            if ds.round_num >= 4:
                step = max(step, int(gap * 0.30))
            return min(int(limit * 0.98), current + step)  # never exceed 98% of limit
        else:
            # Seller
            if ds.round_num == 0:
                return max(1, int(limit * config.DUEL_SELLER_START))
            current = ds.our_last_price or int(limit * config.DUEL_SELLER_START)
            gap = current - limit
            step = max(1, int(gap * config.DUEL_STEP_RATIO))
            if ds.round_num >= 4:
                step = max(step, int(gap * 0.30))
            return max(int(limit * 1.02), current - step)  # never go below 102% of limit

    def _compute_days(self, ds: _DuelState) -> int:
        """Compute delivery day offer.

        Strategy: offer the day that's worst for us (cheap to give away) in
        exchange for better price. If we have low weight on late days, offer
        late delivery. Adjust toward rival's preference if they reveal one.
        """
        if not ds.your_days_weight:
            return 5  # neutral default

        if ds.round_num == 0:
            # First round: offer our worst day (cheapest concession)
            return ds.worst_day_for_us

        # If rival expressed a day preference, consider meeting them partway
        if ds.rival_last_days is not None:
            rival_day = ds.rival_last_days
            our_worst = ds.worst_day_for_us

            # If rival wants a day that's cheap for us, give it to them
            rival_day_weight = ds.your_days_weight.get(str(rival_day), 0.5)
            our_worst_weight = ds.your_days_weight.get(str(our_worst), 0.5)

            if rival_day_weight <= our_worst_weight * 1.2:
                # Rival's preferred day is cheap enough for us — concede it
                return rival_day

            # Otherwise, move slightly toward rival's day
            if ds.round_num >= 2:
                # Offer a day between our worst and rival's preference
                mid = (our_worst + rival_day) // 2
                return max(0, min(10, mid))

            return our_worst

        return ds.worst_day_for_us

    # ── Acceptance logic ─────────────────────────────────────────────────

    def _should_accept(self, ds: _DuelState) -> bool:
        """Should we accept the rival's current offer?

        Accept if the offer is within 2% of our limit. Also accept if we've
        been negotiating for too many rounds (value is shrinking) and the offer
        is still profitable.
        """
        if ds.rival_last_price is None:
            return False

        limit = ds.your_limit
        rival_price = ds.rival_last_price
        threshold = config.DUEL_ACCEPT_THRESHOLD

        # Close to the deadline: any still-profitable offer beats the pie
        # shrinking to zero. Widen acceptance in the last couple of ticks.
        if ds.deadline is not None:
            ticks_left = ds.deadline - self.state.tick
            if ticks_left <= 1:
                if ds.role == "buyer" and rival_price <= limit:
                    return True
                if ds.role == "seller" and rival_price >= limit:
                    return True

        if ds.role == "buyer":
            # Buying: rival_price should be ≤ our limit
            # Accept if price is at or below limit (even slightly above is ok)
            if rival_price <= limit * (1.0 + threshold):
                return True
            # After many rounds, widen acceptance to avoid no-deal
            if ds.round_num >= 4 and rival_price <= limit * 1.05:
                return True
            if ds.round_num >= 6 and rival_price <= limit * 1.10:
                return True
        else:
            # Selling: rival_price should be ≥ our limit (cost)
            # Accept if price is at or above limit (even slightly below is ok)
            if rival_price >= limit * (1.0 - threshold):
                return True
            # After many rounds, widen acceptance
            if ds.round_num >= 4 and rival_price >= limit * 0.95:
                return True
            if ds.round_num >= 6 and rival_price >= limit * 0.90:
                return True

        return False

    # ── Phrase generation ────────────────────────────────────────────────

    def _make_phrase(self, ds: _DuelState, price: int,
                     days: Optional[int] = None) -> str:
        """Build a varied message for the duel."""
        if ds.role == "buyer":
            pool = _BUYER_OPENERS if ds.round_num == 0 else _BUYER_CONCESSIONS
        else:
            pool = _SELLER_OPENERS if ds.round_num == 0 else _SELLER_CONCESSIONS

        idx = (self._phrase_idx + ds.round_num) % len(pool)
        phrase = pool[idx].format(price=price)
        self._phrase_idx += 1

        if days is not None:
            day_idx = ds.round_num % len(_DAYS_PHRASES)
            phrase += _DAYS_PHRASES[day_idx].format(days=days)

        return phrase

    # ── Cleanup ──────────────────────────────────────────────────────────

    def _finish_duel(self, duel_id: int, duel: Optional[dict] = None) -> None:
        """Record and clean up a finished duel."""
        ds = self._active.pop(duel_id, None)
        if ds is None:
            return

        result = {
            "duel_id": duel_id,
            "role": ds.role,
            "your_limit": ds.your_limit,
            "our_last_price": ds.our_last_price,
            "rival_last_price": ds.rival_last_price,
            "rounds": ds.round_num,
            "accepted": ds.accepted,
        }

        if duel:
            result["status"] = duel.get("status", "unknown")
            result["settlement_price"] = duel.get("settlement", {}).get("price")

        self._results.append(result)
        status = result.get("status", "finished")
        log.duel(
            f"Duel {duel_id} finished: role={ds.role}, "
            f"limit={ds.your_limit}, "
            f"rival_price={ds.rival_last_price}, "
            f"rounds={ds.round_num}, status={status}"
        )

    # ── Stats ────────────────────────────────────────────────────────────

    def duel_summary(self) -> str:
        """Return a summary string of duel results so far."""
        if not self._results:
            return "No duel results yet."
        deals = [r for r in self._results if r.get("accepted")]
        no_deals = len(self._results) - len(deals)
        return (
            f"Duels: {len(self._results)} total, "
            f"{len(deals)} deals, {no_deals} no-deals"
        )
