"""TradeManager: P2P trading with other teams on El Rastro and your own venue.

All team-to-team trades happen on a *venue*.  El Rastro is the house venue
(5% + 1P/card fee).  Your own venue (if open) has lower fees and is preferred.

Per-tick constraints:
  - Max 1 accept per tick per counterparty
  - Max 12 new listings per tick
  - Max 30 total open offers
  - Max 6 open conversations
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from bazaar_sdk import BazaarError
import config
import logger as log

if TYPE_CHECKING:
    from state import GameState
    from portfolio import PortfolioManager


# ── Internal limits ─────────────────────────────────────────────────────────
_MAX_NEW_LISTINGS_PER_TICK = 12
_RASTRO_VENUE = "rastro"
_OFFER_EXPIRY_TICKS = 40       # default TTL for offers we post
_STALE_OFFER_AGE = 30          # cancel our own offers older than this many ticks


class TradeManager:
    """Manages all P2P trading activity.

    Instantiated once with refs to GameState and PortfolioManager.
    ``tick()`` is called each game tick after state.refresh() and portfolio.tick().
    """

    def __init__(self, state: GameState, portfolio: PortfolioManager):
        self.s = state
        self.pf = portfolio

        # Track how many listings we post this tick (reset each tick)
        self._listings_this_tick = 0
        # Track which counterparties we've already accepted from this tick
        self._accepted_this_tick: set[str] = set()
        # Set of asset IDs currently listed for sale (to avoid double-listing)
        self._listed_asset_ids: set[int] = set()
        # Our own venue slug (set when detected)
        self._my_venue: Optional[str] = None

    # ── Main loop ───────────────────────────────────────────────────────

    def tick(self) -> None:
        """Run all P2P trading logic for this tick."""
        try:
            self._listings_this_tick = 0
            self._accepted_this_tick = set()
            self._refresh_listed_assets()
            self._detect_my_venue()

            # 1. Cancel stale or bad offers first (frees up slots)
            self._cancel_stale_offers()

            # 2. Process incoming offers addressed to us
            self.process_incoming()

            # 3. List surplus cards for sale
            self.list_surplus()

            # 4. Scan the board for bargains
            self.scan_and_buy()

            # 5. Monitor feed for rival intelligence
            self._monitor_feed()

        except Exception as exc:
            log.warn(f"Trade tick error: {exc}")

    # ── 2. list_surplus ─────────────────────────────────────────────────

    def list_surplus(self) -> None:
        """Post sell offers for duplicate cards on El Rastro (or own venue)."""
        if not self.s.can_list_offer():
            return

        venue = self._preferred_venue()
        for item in self.pf.surplus_list:
            if self._listings_this_tick >= _MAX_NEW_LISTINGS_PER_TICK:
                break
            if not self.s.can_list_offer():
                break

            asset = item["asset"]
            asset_id = asset.get("id")
            if asset_id is None:
                continue
            if asset_id in self._listed_asset_ids:
                continue  # already listed

            price = item["sell_price"]
            if price <= 0:
                continue

            # If we have market intelligence that a specific rival wants this
            # exact card, target them directly instead of listing blind — they
            # get first look, and we can ask a touch more since we know they need it.
            target_team = None
            interested = self.s.interested_buyers_for(item["ref"])
            if interested:
                target_team = interested[0]
                price = max(price, int(price * 1.05))

            try:
                self.s.b.list_offer(
                    give={"assets": [asset_id]},
                    want={"cash": price},
                    venue=venue,
                    to=target_team,
                    expires_in_ticks=_OFFER_EXPIRY_TICKS,
                )
                self._listings_this_tick += 1
                self._listed_asset_ids.add(asset_id)
                dest = f"targeted at {target_team}" if target_team else f"on {venue}"
                log.trade(f"Listed {item['ref']} for {price}{self.s.currency} {dest}")
            except BazaarError as exc:
                if exc.code == "rate_limited":
                    break  # back off, try again next tick
                log.warn(f"Failed to list {item['ref']}: {exc}")
            except Exception as exc:
                log.warn(f"Failed to list {item['ref']}: {exc}")

    # ── 3. scan_and_buy ─────────────────────────────────────────────────

    def scan_and_buy(self) -> None:
        """Check the board for cards that are worth more to us than their price."""
        venues_to_scan = [_RASTRO_VENUE]
        if self._my_venue and self._my_venue != _RASTRO_VENUE:
            venues_to_scan.append(self._my_venue)

        for venue in venues_to_scan:
            try:
                self._scan_venue(venue)
            except BazaarError as exc:
                log.warn(f"Board scan error ({venue}): {exc}")
            except Exception as exc:
                log.warn(f"Board scan error ({venue}): {exc}")

    def _scan_venue(self, venue: str) -> None:
        """Scan a single venue's board for profitable buys."""
        try:
            board_data = self.s.b.board(venue)
        except Exception as exc:
            log.warn(f"Cannot read board({venue}): {exc}")
            return

        offers = board_data.get("offers", []) if isinstance(board_data, dict) else board_data
        if not isinstance(offers, list):
            return

        # Score and sort opportunities
        opportunities: list[tuple[float, dict]] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            # Skip our own offers
            if offer.get("maker") == self.s.team_id:
                continue
            give = offer.get("give", {})
            want = offer.get("want", {})
            if not isinstance(give, dict) or not isinstance(want, dict):
                continue

            # Someone is selling card(s)/asset(s) for cash — we are the buyer
            asked_cash = want.get("cash", 0)
            given_assets = give.get("assets", [])
            card_refs = [a["ref"] for a in given_assets if isinstance(a, dict) and a.get("ref")]

            if asked_cash > 0 and card_refs:
                for ref in card_refs:
                    if self.pf.should_buy(ref, asked_cash) and asked_cash <= self.s.cash:
                        score = self.s.get_my_value(ref) / max(1, asked_cash)
                        opportunities.append((score, offer))
                        # Record rival interest (they are selling → they don't value it)
                        seller = offer.get("maker", "")
                        if seller:
                            self.s.record_rival_interest(seller, ref, strength=-0.5)

        # Accept best opportunity (max 1 accept per tick per counterparty)
        opportunities.sort(key=lambda x: x[0], reverse=True)
        for _score, offer in opportunities:
            offer_id = offer.get("id")
            counterparty = offer.get("maker", "unknown")
            if offer_id is None:
                continue
            if counterparty in self._accepted_this_tick:
                continue

            try:
                self.s.b.accept(offer_id)
                self._accepted_this_tick.add(counterparty)
                want = offer.get("want", {})
                give = offer.get("give", {})
                log.trade(f"Accepted offer #{offer_id} from {counterparty}: "
                          f"get {give} for {want.get('cash', '?')}{self.s.currency}")
                # Only 1 accept per tick overall is safest
                break
            except BazaarError as exc:
                if exc.code == "wait_for_tick":
                    break  # already accepted something this tick
                log.warn(f"Accept failed for #{offer_id}: {exc}")
            except Exception as exc:
                log.warn(f"Accept failed for #{offer_id}: {exc}")

    # ── 4. process_incoming ─────────────────────────────────────────────

    def process_incoming(self) -> None:
        """Check offers addressed to us and accept the good ones."""
        try:
            offers_data = self.s.b.my_offers()
            all_offers = (offers_data.get("offers", [])
                          if isinstance(offers_data, dict) else offers_data)
        except Exception as exc:
            log.warn(f"Cannot read my_offers: {exc}")
            return

        if not isinstance(all_offers, list):
            return

        for offer in all_offers:
            if not isinstance(offer, dict):
                continue
            # Only look at offers *to* us that are open and not from us
            if offer.get("maker") == self.s.team_id:
                continue
            if offer.get("status") != "open":
                continue
            # Determine if this is addressed to us (has "to" field matching us,
            # or is a general board offer we already handle in scan_and_buy)
            if not offer.get("to"):
                continue  # board offers handled by scan_and_buy

            give = offer.get("give", {})
            want = offer.get("want", {})
            if not isinstance(give, dict) or not isinstance(want, dict):
                continue

            if self._evaluate_incoming_offer(offer, give, want):
                offer_id = offer.get("id")
                counterparty = offer.get("maker", "unknown")
                if offer_id is None:
                    continue
                if counterparty in self._accepted_this_tick:
                    continue

                try:
                    self.s.b.accept(offer_id)
                    self._accepted_this_tick.add(counterparty)
                    log.trade(f"Accepted incoming offer #{offer_id} from {counterparty}")
                    break  # one accept per tick
                except BazaarError as exc:
                    if exc.code == "wait_for_tick":
                        break
                    log.warn(f"Accept incoming failed #{offer_id}: {exc}")
                except Exception as exc:
                    log.warn(f"Accept incoming failed #{offer_id}: {exc}")

    @staticmethod
    def _type_refs(types: list, kind: str) -> list[str]:
        """Extract refs of a given kind ("card"/"pack") from a types list
        like ["card:LAV-06", "pack:sobre_barrio"]."""
        refs = []
        for t in types or []:
            if isinstance(t, str) and ":" in t:
                k, ref = t.split(":", 1)
                if k == kind and ref:
                    refs.append(ref)
        return refs

    def _evaluate_incoming_offer(self, offer: dict, give: dict, want: dict) -> bool:
        """Return True if this incoming offer is profitable for us."""
        try:
            given_assets = give.get("assets", [])
            given_refs = [a["ref"] for a in given_assets if isinstance(a, dict) and a.get("ref")]
            asked_cash = want.get("cash", 0)

            # Case A: They give specific card assets, want cash from us
            if given_refs and asked_cash > 0:
                total_value = 0.0
                for ref in given_refs:
                    if not self.pf.should_buy(ref, asked_cash // max(1, len(given_refs))):
                        return False
                    total_value += self.s.get_my_value(ref)
                return total_value > asked_cash and asked_cash <= self.s.cash

            # Case B: They want specific cards from us, give cash
            wanted_refs = self._type_refs(want.get("types", []), "card")
            given_cash = give.get("cash", 0)
            if wanted_refs and given_cash > 0:
                for ref in wanted_refs:
                    min_price = self._min_sell_price_for_ref(ref)
                    per_card = given_cash // max(1, len(wanted_refs))
                    if per_card < min_price:
                        return False
                return True

            return False
        except Exception as exc:
            log.warn(f"Offer evaluation error: {exc}")
            return False

    # ── 6. Cancel stale / bad offers ────────────────────────────────────

    def _cancel_stale_offers(self) -> None:
        """Cancel our own offers that are too old or no longer make sense."""
        for offer in self.s.open_offers:
            if not isinstance(offer, dict):
                continue
            if offer.get("maker") != self.s.team_id:
                continue
            if offer.get("status") != "open":
                continue

            offer_id = offer.get("id")
            if offer_id is None:
                continue

            # Check age: cancel if the offer has been sitting too long
            created_tick = offer.get("created_tick", 0)
            age = self.s.tick - created_tick
            if age > _STALE_OFFER_AGE:
                try:
                    self.s.b.cancel(offer_id)
                    log.trade(f"Cancelled stale offer #{offer_id} (age {age} ticks)")
                except Exception as exc:
                    log.warn(f"Cancel failed #{offer_id}: {exc}")
                continue

            # Check if we're selling a card that we now need (no longer a duplicate)
            give = offer.get("give", {})
            for asset in give.get("assets", []):
                ref = asset.get("ref", "") if isinstance(asset, dict) else ""
                if ref and not self.s.is_duplicate(ref):
                    try:
                        self.s.b.cancel(offer_id)
                        log.trade(f"Cancelled offer #{offer_id}: {ref} no longer surplus")
                    except Exception as exc:
                        log.warn(f"Cancel failed #{offer_id}: {exc}")
                    break

    # ── 7. Prefer own venue ─────────────────────────────────────────────

    def _preferred_venue(self) -> str:
        """Return our own venue if available, else El Rastro."""
        if self._my_venue:
            return self._my_venue
        return _RASTRO_VENUE

    def _detect_my_venue(self) -> None:
        """Detect if we have our own venue open."""
        if self._my_venue:
            return  # already detected
        try:
            venues_data = self.s.b.venues()
            venues = venues_data.get("venues", []) if isinstance(venues_data, dict) else venues_data
            if not isinstance(venues, list):
                return
            for v in venues:
                if isinstance(v, dict) and v.get("owner") == self.s.team_id:
                    self._my_venue = v.get("slug", v.get("id", ""))
                    if self._my_venue:
                        log.trade(f"Using own venue: {self._my_venue}")
                    break
        except Exception:
            pass  # no venue yet — that's fine

    # ── Feed monitoring (rival intelligence) ────────────────────────────

    def _monitor_feed(self) -> None:
        """Watch the public feed for settlements and open offers to infer rival
        preferences and real market prices (real schema: settlement payloads
        carry "items": [{"ref", "frm", "to", ...}] and "price"; offer.listed
        payloads carry a "maker" and give/want)."""
        try:
            feed_data = self.s.b.feed(limit=100)
            events = feed_data.get("events", []) if isinstance(feed_data, dict) else feed_data
            if not isinstance(events, list):
                return

            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type", "")
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    continue

                if event_type == "settlement":
                    price = payload.get("price", 0)
                    for item in payload.get("items", []):
                        if not isinstance(item, dict):
                            continue
                        ref = item.get("ref", "")
                        if not ref:
                            continue
                        buyer = item.get("to", "")
                        seller = item.get("frm", "")
                        if buyer and buyer != self.s.team_id and buyer not in ("abuela",):
                            self.s.record_rival_interest(buyer, ref, strength=1.0)
                        if seller and seller != self.s.team_id and seller not in ("abuela",):
                            self.s.record_rival_interest(seller, ref, strength=-0.3)
                        self.s.record_market_price(ref, price)

                elif event_type == "offer.listed":
                    offer = payload.get("offer", payload)
                    maker = offer.get("maker", "")
                    if not maker or maker == self.s.team_id:
                        continue
                    want = offer.get("want", {}) or {}
                    give = offer.get("give", {}) or {}
                    # Listing cash for specific cards: maker wants those cards
                    for ref in self._type_refs(want.get("types", []), "card"):
                        self.s.record_rival_interest(maker, ref, strength=0.7)
                    # Listing cards for cash: maker has surplus of those cards
                    for asset in give.get("assets", []):
                        ref = asset.get("ref", "") if isinstance(asset, dict) else ""
                        if ref:
                            self.s.record_rival_interest(maker, ref, strength=-0.5)

        except Exception as exc:
            log.warn(f"Feed monitor error: {exc}")

    # ── Internal helpers ────────────────────────────────────────────────

    def _refresh_listed_assets(self) -> None:
        """Rebuild set of asset IDs that are currently listed for sale."""
        self._listed_asset_ids = set()
        for offer in self.s.open_offers:
            if not isinstance(offer, dict):
                continue
            if offer.get("from") != self.s.b.key:
                continue
            if offer.get("status") != "open":
                continue
            give = offer.get("give", {})
            for aid in give.get("assets", []):
                self._listed_asset_ids.add(aid)

    def _min_sell_price_for_ref(self, card_ref: str) -> int:
        """Minimum price to accept for selling a card by ref."""
        book = self.s.get_book_value(card_ref)
        my_val = self.s.get_my_value(card_ref)

        if self.s.is_duplicate(card_ref):
            return max(1, int(book * config.TRADE_DUPLICATE_DISCOUNT))
        else:
            return max(1, int(max(book, my_val)))
