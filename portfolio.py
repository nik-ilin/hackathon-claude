"""PortfolioManager: valuation decisions — what to acquire, what to sell, and at what price.

The key insight is that each team has private set multipliers (shuffled), so some
neighbourhoods are worth much more to *us* than their public book value.  We exploit
this by focusing acquisitions on high-multiplier sets and dumping duplicates that
are worth little to us but valuable to rivals who might be chasing those sets.

A 'page' = all commons + uncommons + rares of a set.  Completing it gives a bonus,
so cards that fill a gap are worth more than their face value.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import config
import logger as log

if TYPE_CHECKING:
    from state import GameState


# ── Page-completion bonus scaling ───────────────────────────────────────────
# How much extra to value a card that brings us closer to completing a page.
# The closer we are, the more each missing card is worth (exponential urgency).
_PAGE_BONUS_BASE = 1.15       # minimum multiplier for any missing-page card
_PAGE_BONUS_MAX = 2.00        # multiplier when only 1 card is missing
_PAGE_BONUS_THRESHOLD = 0.50  # only kick in when page is ≥50% complete


class PortfolioManager:
    """Decides which cards to acquire and which to sell, and at what price.

    Instantiated once; ``tick()`` is called each game tick after ``state.refresh()``.
    """

    def __init__(self, state: GameState):
        self.s = state

        # Recalculated every tick
        self._want_list: list[dict] = []       # [{ref, set_id, my_value, book, ratio, page_bonus}]
        self._surplus_list: list[dict] = []    # [{asset, ref, sell_price}]

    # ── Public API ──────────────────────────────────────────────────────

    def tick(self) -> None:
        """Recompute want/surplus lists.  Call once per tick, after state.refresh()."""
        try:
            self._build_want_list()
            self._build_surplus_list()
            if self._want_list:
                top = self._want_list[:3]
                names = ", ".join(f"{w['ref']}(×{w['ratio']:.1f})" for w in top)
                log.info(f"Portfolio: want {len(self._want_list)} cards, top: {names}")
            if self._surplus_list:
                log.info(f"Portfolio: {len(self._surplus_list)} duplicates to sell")
        except Exception as exc:
            log.warn(f"Portfolio tick error: {exc}")

    # ── 1. Want / surplus lists ─────────────────────────────────────────

    @property
    def want_list(self) -> list[dict]:
        """Cards we want to acquire, sorted by effective value-to-cost ratio."""
        return list(self._want_list)

    @property
    def surplus_list(self) -> list[dict]:
        """Assets (duplicate cards) we are willing to sell."""
        return list(self._surplus_list)

    # ── 2. should_buy ───────────────────────────────────────────────────

    def should_buy(self, card_ref: str, price: int) -> bool:
        """Return True if buying *card_ref* at *price* creates positive value.

        The card is worth buying if:
        - price ≤ our private value (we never overpay)
        - we factor in page-completion bonus (a card that fills a page gap is
          worth more than its raw ``your_value``)
        """
        try:
            effective_value = self._effective_value(card_ref)
            if effective_value <= 0:
                return False
            return price <= effective_value
        except Exception as exc:
            log.warn(f"should_buy error for {card_ref}: {exc}")
            return False

    # ── 3. sell_price ───────────────────────────────────────────────────

    def sell_price(self, asset: dict) -> int:
        """Price to ask when selling *asset*, demand-aware.

        Baseline is TRADE_DUPLICATE_DISCOUNT of book for a duplicate (our
        marginal value is low, so even a discount is pure profit). But if we've
        seen real rivals wanting this exact card (via dealer/P2P chatter, feed
        settlements, or listed offers), or the observed P2P market price for it
        runs above book, price it up toward that demand instead of leaving
        money on the table — someone short a card will pay more than a
        stranger-priced discount.
        """
        try:
            ref = asset.get("ref", "")
            book = self.s.get_book_value(ref)
            my_val = asset.get("your_value", self.s.get_my_value(ref))

            if self.s.is_duplicate(ref):
                price = book * config.TRADE_DUPLICATE_DISCOUNT
                interested = self.s.interested_buyers_for(ref)
                if interested:
                    # Each extra interested team pushes the price up, capped
                    # at TRADE_BUY_PREMIUM so it's still a fair-looking offer.
                    demand_ratio = min(config.TRADE_BUY_PREMIUM, 1.0 + 0.1 * len(interested))
                    price = max(price, book * demand_ratio)
                market = self.s.market_price_estimate(ref)
                if market:
                    price = max(price, market * 0.95)
                price = max(1, int(price))
            else:
                # Not a duplicate — only sell if the price is at least our
                # private value (we'd be giving up real portfolio value).
                price = max(1, int(max(book, my_val)))

            return price
        except Exception as exc:
            log.warn(f"sell_price error: {exc}")
            # Fallback: ask for book value or a minimum of 1
            return max(1, int(self.s.get_book_value(asset.get("ref", ""))))

    # ── 4. priority_acquisitions ────────────────────────────────────────

    def priority_acquisitions(self) -> list[dict]:
        """Cards sorted by value-to-cost ratio (best deals first).

        Each entry: {ref, set_id, my_value, book, ratio, page_bonus, effective_value}.
        """
        return list(self._want_list)

    # ── 5. page_completion_value ────────────────────────────────────────

    def page_completion_value(self, set_id: str) -> float:
        """Estimate the bonus value of completing *set_id*'s page.
        
        Returns a multiplier of 1.25 representing the page completion bonus.
        """
        try:
            progress = self.s.page_progress.get(set_id, {})
            have = progress.get("have", 0)
            need = progress.get("need", 1)

            if need == 0 or progress.get("complete", False):
                return 1.0  # already complete — no bonus

            return 1.25
        except Exception as exc:
            log.warn(f"page_completion_value error for {set_id}: {exc}")
            return 1.0

    # ── Private helpers ─────────────────────────────────────────────────

    def _effective_value(self, card_ref: str) -> float:
        """Our effective value for one more copy of *card_ref*, including page bonus."""
        my_val = self.s.get_my_value(card_ref)
        if my_val <= 0:
            return 0.0

        # Apply page-completion bonus if this card is missing from a set
        card_info = self.s.card_catalog.get(card_ref, {})
        set_id = card_info.get("set", "")
        if set_id and card_ref in self.s.missing.get(set_id, []):
            bonus = self.page_completion_value(set_id)
            my_val *= bonus

        return my_val

    def _build_want_list(self) -> None:
        """Identify cards we want to acquire, scored by effective value / book cost."""
        wants: list[dict] = []

        for set_id in self.s.set_priority:
            missing_refs = self.s.needs_for_page(set_id)
            for ref in missing_refs:
                book = self.s.get_book_value(ref)
                if book <= 0:
                    continue
                my_val = self.s.get_my_value(ref)
                if my_val <= 0:
                    continue

                page_bonus = self.page_completion_value(set_id)
                effective = my_val * page_bonus
                ratio = effective / book

                wants.append({
                    "ref": ref,
                    "set_id": set_id,
                    "my_value": my_val,
                    "book": book,
                    "ratio": ratio,
                    "page_bonus": page_bonus,
                    "effective_value": effective,
                })

        # Also add cards from any set not yet prioritised (we might have no
        # cards from them yet, so they don't appear in set_priority)
        prioritised = set(self.s.set_priority)
        for set_id, set_info in self.s.set_catalog.items():
            if set_id in prioritised:
                continue
            for c in set_info.get("cards", []):
                ref = c["id"]
                if self.s.count_held(ref) > 0:
                    continue
                book = self.s.get_book_value(ref)
                if book <= 0:
                    continue
                my_val = self.s.get_my_value(ref)
                if my_val <= 0:
                    continue
                ratio = my_val / book
                wants.append({
                    "ref": ref,
                    "set_id": set_id,
                    "my_value": my_val,
                    "book": book,
                    "ratio": ratio,
                    "page_bonus": 1.0,
                    "effective_value": my_val,
                })

        # Sort by ratio (best value-to-cost first)
        wants.sort(key=lambda w: w["ratio"], reverse=True)
        self._want_list = wants

    def _build_surplus_list(self) -> None:
        """Build the list of duplicate assets we're willing to sell."""
        surplus: list[dict] = []
        for asset in self.s.duplicates:
            ref = asset.get("ref", "")
            price = self.sell_price(asset)
            surplus.append({
                "asset": asset,
                "ref": ref,
                "sell_price": price,
            })
        # Sort by sell price descending (sell the most valuable duplicates first
        # so they generate the most cash for acquisitions)
        surplus.sort(key=lambda s: s["sell_price"], reverse=True)
        self._surplus_list = surplus
