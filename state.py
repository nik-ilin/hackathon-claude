"""GameState: the single source of truth for the agent. Refreshed every tick."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Optional

from bazaar_sdk import Bazaar
import logger as log


class GameState:
    """Tracks everything the agent needs to make decisions.

    Call refresh() once per tick before any manager runs.
    """

    def __init__(self, b: Bazaar):
        self.b = b
        self.team_id = ""
        self.tick = 0
        self.tick_seconds = 60
        self.paused = False
        self.cash = 0
        self.level = 0
        self.assets: list[dict] = []
        self.cards: list[dict] = []
        self.packs: list[dict] = []
        self.album: dict = {}
        self.score: dict = {}
        self.limits: dict = {}

        # Catalog (static — loaded once)
        self.catalog: dict = {}
        self.card_catalog: dict[str, dict] = {}   # ref -> card info
        self.set_catalog: dict[str, dict] = {}     # set_id -> set info
        self.pack_catalog: list[dict] = []
        self.currency = "P"

        # Derived state (computed on refresh)
        self.my_values: dict[str, float] = {}       # ref -> my value of one more copy
        self.held_refs: dict[str, list[dict]] = {}  # ref -> list of assets I hold of that ref
        self.duplicates: list[dict] = []            # assets that are extra copies
        self.missing: dict[str, list[str]] = {}     # set_id -> list of card refs I'm missing
        self.page_progress: dict[str, dict] = {}    # set_id -> {have, need, complete}

        # Inferred set priorities (which sets I value most)
        self.set_priority: list[str] = []           # set_ids sorted best->worst

        # Rival intelligence
        self.rival_preferences: dict[str, dict] = defaultdict(lambda: defaultdict(float))
        self.rival_card_interest: dict[str, dict] = defaultdict(lambda: defaultdict(float))
        self.market_prices: dict[str, list] = defaultdict(list)

        # Threading state
        self.open_threads: list[dict] = []
        self.open_offers: list[dict] = []

        # Dealer state
        self.dealers: list[dict] = []
        self.unlocked_dealers: list[str] = []

        # Clock / schedule
        self.next_tick_in: float = 1.0
        self.schedule_data: dict = {}

        self._last_catalog_load = 0.0

    def load_catalog(self) -> None:
        """Load the catalog (card definitions, packs, value rules). Only needed once."""
        self.catalog = self.b.catalog()
        self.currency = self.catalog.get("currency_symbol", "P")
        self.pack_catalog = self.catalog.get("packs", [])
        for s in self.catalog.get("sets", []):
            self.set_catalog[s["id"]] = s
            for c in s.get("cards", []):
                self.card_catalog[c["id"]] = {**c, "set": s["id"], "set_name": s["name"]}
        self._last_catalog_load = time.time()
        log.info(f"Catalog: {len(self.card_catalog)} cards in {len(self.set_catalog)} sets, "
                 f"{len(self.pack_catalog)} pack types")

    def refresh(self) -> None:
        """Pull fresh state from the server. Call once at the start of each tick."""
        # Clock
        clock = self.b.clock()
        self.tick = clock.get("tick", 0)
        self.tick_seconds = clock.get("tick_seconds", 60)
        self.paused = clock.get("paused", False)
        self.next_tick_in = clock.get("next_tick_in", 1.0)
        self.limits = clock.get("limits", {})

        if self.paused:
            return

        # Me
        me = self.b.me()
        self.team_id = me.get("id", "")
        self.cash = me.get("cash", 0)
        self.level = me.get("level", 0)
        self.assets = me.get("assets", [])
        self.album = me.get("album", {})
        self.score = me.get("score", {})
        self.unlocked_dealers = me.get("unlocked", [])

        # Separate cards from packs
        self.cards = [a for a in self.assets if a.get("kind") == "card"]
        self.packs = [a for a in self.assets if a.get("kind") == "pack"]

        # Reload catalog if new sets might have appeared (every 30 min)
        if time.time() - self._last_catalog_load > 1800:
            self.load_catalog()

        # Build derived state
        self._compute_holdings()
        self._compute_set_priority()
        self._load_threads_and_offers()
        self._load_dealers()

    def _compute_holdings(self) -> None:
        """Compute held refs, duplicates, missing cards, and page progress."""
        self.held_refs = defaultdict(list)
        for c in self.cards:
            self.held_refs[c["ref"]].append(c)

        # Duplicates: cards beyond the first copy
        self.duplicates = []
        for ref, copies in self.held_refs.items():
            if len(copies) > 1:
                # Keep the lowest serial, sell the rest
                by_serial = sorted(copies, key=lambda c: c.get("serial", 9999))
                self.duplicates.extend(by_serial[1:])

        # Missing cards & page progress
        self.missing = defaultdict(list)
        self.page_progress = {}
        for set_id, set_info in self.set_catalog.items():
            page_cards = [c for c in set_info.get("cards", [])
                          if c.get("rarity") in ("common", "uncommon", "rare")]
            have = sum(1 for c in page_cards if c["id"] in self.held_refs)
            need = len(page_cards)
            self.page_progress[set_id] = {
                "have": have, "need": need,
                "complete": have >= need,
                "fraction": have / need if need else 1.0,
            }
            for c in set_info.get("cards", []):
                if c["id"] not in self.held_refs:
                    self.missing[set_id].append(c["id"])

    def _compute_set_priority(self) -> None:
        """Infer which sets I value most by averaging my_value of held cards per set."""
        set_values: dict[str, list[float]] = defaultdict(list)
        for c in self.cards:
            card_info = self.card_catalog.get(c.get("ref", ""), {})
            if card_info:
                val = c.get("your_value", 0)
                book = card_info.get("book", 1)
                if book > 0:
                    set_values[card_info["set"]].append(val / book)
        # Sort by average value/book ratio (higher = I value this set more)
        self.set_priority = sorted(
            set_values.keys(),
            key=lambda s: sum(set_values[s]) / len(set_values[s]) if set_values[s] else 0,
            reverse=True,
        )

    def _load_threads_and_offers(self) -> None:
        """Load open threads and offers."""
        try:
            threads_data = self.b.my_threads(status="open")
            self.open_threads = threads_data.get("threads", []) if isinstance(threads_data, dict) else threads_data
        except Exception:
            self.open_threads = []
        try:
            offers_data = self.b.my_offers()
            self.open_offers = offers_data.get("offers", []) if isinstance(offers_data, dict) else offers_data
        except Exception:
            self.open_offers = []

    def _load_dealers(self) -> None:
        """Load available dealers."""
        try:
            dealers_data = self.b.dealers()
            self.dealers = dealers_data.get("personas", []) if isinstance(dealers_data, dict) else dealers_data
        except Exception:
            self.dealers = []

    # ── Value lookups ───────────────────────────────────────────────────

    def get_my_value(self, card_ref: str) -> float:
        """Get my private value for one more copy of card_ref (cached per tick)."""
        if card_ref not in self.my_values:
            try:
                v = self.b.value(card_ref)
                self.my_values[card_ref] = v.get("your_value", 0)
            except Exception:
                self.my_values[card_ref] = 0
        return self.my_values[card_ref]

    def get_book_value(self, card_ref: str) -> float:
        """Book value from catalog (public, same for all teams)."""
        return self.card_catalog.get(card_ref, {}).get("book", 0)

    def value_ratio(self, card_ref: str) -> float:
        """my_value / book_value: >1 means I value it above average."""
        book = self.get_book_value(card_ref)
        return self.get_my_value(card_ref) / book if book > 0 else 0

    # ── Card counting ───────────────────────────────────────────────────

    def count_held(self, card_ref: str) -> int:
        return len(self.held_refs.get(card_ref, []))

    def is_duplicate(self, card_ref: str) -> bool:
        return self.count_held(card_ref) > 1

    def needs_for_page(self, set_id: str) -> list[str]:
        """Card refs I still need to complete this set's page."""
        return self.missing.get(set_id, [])

    # ── Rival intelligence ──────────────────────────────────────────────

    def record_rival_interest(self, team_id: str, card_ref: str, strength: float = 1.0) -> None:
        """Record that a rival showed interest in a card (from their offers/messages)."""
        card_info = self.card_catalog.get(card_ref, {})
        if card_info:
            self.rival_preferences[team_id][card_info["set"]] += strength
        self.rival_card_interest[card_ref][team_id] += strength

    def infer_rival_top_set(self, team_id: str) -> Optional[str]:
        """Guess which set a rival values most."""
        prefs = self.rival_preferences.get(team_id, {})
        if prefs:
            return max(prefs, key=prefs.get)
        return None

    def interested_buyers_for(self, card_ref: str, min_strength: float = 0.5) -> list[str]:
        """Teams that have shown positive interest in this exact card ref,
        best lead first — used to target sell offers instead of listing blind."""
        buyers = self.rival_card_interest.get(card_ref, {})
        ranked = sorted(
            ((team, s) for team, s in buyers.items() if s >= min_strength),
            key=lambda kv: -kv[1],
        )
        return [team for team, _ in ranked]

    def record_market_price(self, card_ref: str, price: float) -> None:
        """Track observed P2P settlement prices per card ref (last 20)."""
        if price <= 0:
            return
        history = self.market_prices[card_ref]
        history.append(price)
        if len(history) > 20:
            del history[0]

    def market_price_estimate(self, card_ref: str) -> Optional[float]:
        """Recent average real trade price for a card, if we've observed any."""
        history = self.market_prices.get(card_ref)
        if not history:
            return None
        return sum(history) / len(history)

    # ── Convenience ─────────────────────────────────────────────────────

    def thread_count(self) -> int:
        """Number of currently open threads (conversations)."""
        return len([t for t in self.open_threads if isinstance(t, dict) and t.get("status") == "open"])

    def offer_count(self) -> int:
        """Number of currently open offers."""
        return len([o for o in self.open_offers if isinstance(o, dict) and o.get("status") == "open"])

    def can_open_thread(self) -> bool:
        max_threads = self.limits.get("max_threads", 6)
        return self.thread_count() < max_threads

    def can_list_offer(self) -> bool:
        max_offers = self.limits.get("max_offers", 30)
        return self.offer_count() < max_offers

    def summary(self) -> str:
        score_val = self.score.get("score", 0) if self.score else 0
        rank = self.score.get("rank", "?") if self.score else "?"
        top_sets = ", ".join(self.set_priority[:2]) if self.set_priority else "?"
        return (
            f"Tick {self.tick} ({self.tick_seconds}s) | "
            f"Cash {self.cash} {self.currency} | "
            f"Cards {len(self.cards)} | Dupes {len(self.duplicates)} | "
            f"Level {self.level} | Score {score_val} (#{rank}) | "
            f"Top sets: {top_sets}"
        )
