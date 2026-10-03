"""Main agent loop: orchestrates all managers tick by tick.

    BAZAAR_URL=https://bazaar.causaprima.ai BAZAAR_KEY=tk-xxxx python3 run.py

The agent runs one iteration per game tick:
  1. Refresh game state
  2. Dealer haggling (unlock levels, buy packs)
  3. Duel negotiations (1v1 scheduled sessions)
  4. P2P trading (sell duplicates, buy wanted cards)
  5. Portfolio maintenance (open packs, manage listings)
  6. Open own venue when level 2 is reached
"""
from __future__ import annotations

import time
import traceback
from typing import Optional

from bazaar_sdk import Bazaar, BazaarError
import config
import logger as log
from state import GameState
from dealer import DealerManager
from duels import DuelEngine
from trading import TradeManager
from portfolio import PortfolioManager


class Agent:
    """The brain: one tick() call per heartbeat, delegates to specialised managers."""

    def __init__(self):
        self.b = Bazaar(config.BAZAAR_URL, config.BAZAAR_KEY, wait_on_tick=False)
        self.state = GameState(self.b)

        # Load catalog once (card definitions, pack types, etc.)
        self.state.load_catalog()

        # Sub-managers (each has a .tick(state) method)
        self.dealer = DealerManager(self.state)
        self.duels = DuelEngine(self.state)
        self.portfolio = PortfolioManager(self.state)
        self.trading = TradeManager(self.state, self.portfolio)

        # Venue state
        self.venue_opened = False
        self.venue_id: Optional[str] = None
        self.broker_key: Optional[str] = None

        # Timing
        self._last_leaderboard = 0.0
        self._last_tick = -1
        self._ticks_played = 0

    def run(self) -> None:
        """Run forever: wait for ticks, act on each one."""
        log.info("=" * 60)
        log.info("🏪  The Bazaar Agent — starting up")
        log.info("=" * 60)

        # Initial state
        self.state.refresh()
        log.info(self.state.summary())
        self._print_portfolio_overview()

        while True:
            try:
                self._wait_for_next_tick()
                self._tick()
            except KeyboardInterrupt:
                log.info("Agent stopped by user.")
                break
            except Exception as e:
                log.warn(f"Tick error (continuing): {e}")
                traceback.print_exc()
                time.sleep(1)

    def _wait_for_next_tick(self) -> None:
        """Sleep until the next tick."""
        clock = self.b.clock()
        if clock.get("paused"):
            log.info("Game paused. Waiting for next session...")
            while clock.get("paused"):
                time.sleep(5)
                clock = self.b.clock()
        wait = max(0.05, float(clock.get("next_tick_in", 1.0)))
        time.sleep(wait + 0.15)  # small buffer to ensure tick has happened

    def _tick(self) -> None:
        """One game tick: refresh state, run all managers."""
        # Refresh game state
        self.state.refresh()

        # Skip if same tick (shouldn't happen with proper waiting)
        if self.state.tick <= self._last_tick:
            return
        self._last_tick = self.state.tick
        self._ticks_played += 1

        if self.state.paused:
            return

        # Log status every 10 ticks
        if self._ticks_played % 10 == 1:
            log.info(self.state.summary())

        # ── 1. Dealer haggling (priority: unlock levels, opens packs) ───
        try:
            self.dealer.tick()
        except Exception as e:
            log.warn(f"Dealer tick error: {e}")

        # ── 2. Duels (time-sensitive: pie shrinks each round) ───────────
        try:
            self.duels.tick()
        except Exception as e:
            log.warn(f"Duel tick error: {e}")

        # ── 3. P2P Trading ──────────────────────────────────────────────
        try:
            self.trading.tick()
        except Exception as e:
            log.warn(f"Trading tick error: {e}")

        # ── 4. Venue management ─────────────────────────────────────────
        if not self.venue_opened and self.state.level >= 2:
            self._try_open_venue()

        # ── 5. Periodic leaderboard check ───────────────────────────────
        now = time.time()
        if now - self._last_leaderboard > config.LEADERBOARD_INTERVAL:
            self._check_leaderboard()
            self._last_leaderboard = now

    def _try_open_venue(self) -> None:
        """Open our own venue with low fees to attract traders."""
        if self.state.cash < config.VENUE_BOND + 50:
            return  # not enough cash yet, keep some buffer

        try:
            result = self.b.open_venue(
                name="La Bolsa",
                fee_bps=config.VENUE_FEE_BPS,
                fee_per_card=config.VENUE_FEE_PER_CARD,
                rules={"mechanism": config.VENUE_MECHANISM},
                description="Low fees, fast matches. Trade here!",
            )
            self.venue_id = result.get("venue_id") or result.get("id")
            self.broker_key = result.get("broker_key")
            self.venue_opened = True
            log.venue(f"🏪 Venue opened! broker_key={self.broker_key}")
            log.venue(f"   Run: BROKER_KEY={self.broker_key} python3 smart_broker.py")
            if self.broker_key:
                try:
                    with open("broker_key.txt", "w") as f:
                        f.write(self.broker_key)
                except OSError as e:
                    log.warn(f"Could not persist broker_key to file: {e}")
        except BazaarError as e:
            if e.code == "venue_not_live":
                pass  # venues not available yet
            else:
                log.warn(f"Cannot open venue: {e}")

    def _check_leaderboard(self) -> None:
        """Check leaderboard and log our position."""
        try:
            lb = self.b.leaderboard()
            teams = lb.get("teams", lb.get("leaderboard", []))
            if isinstance(teams, list):
                for i, t in enumerate(teams[:5], 1):
                    name = t.get("name", t.get("team", "?"))
                    score = t.get("score", 0)
                    log.info(f"  #{i} {name}: {score}")
        except Exception:
            pass  # leaderboard is non-critical

    def _print_portfolio_overview(self) -> None:
        """Print initial portfolio analysis."""
        log.info("── Portfolio Overview ──")
        log.info(f"  Cash: {self.state.cash} {self.state.currency}")
        log.info(f"  Cards: {len(self.state.cards)}, Packs: {len(self.state.packs)}")
        log.info(f"  Duplicates: {len(self.state.duplicates)}")
        if self.state.set_priority:
            log.info(f"  Set priority (best→worst): {' > '.join(self.state.set_priority)}")
        for set_id, prog in self.state.page_progress.items():
            status = "✅" if prog["complete"] else f"{prog['have']}/{prog['need']}"
            log.info(f"  Page {set_id}: {status}")
