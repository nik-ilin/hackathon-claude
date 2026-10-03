"""Team-to-team trading at OUR private values (this part of Negotiating has no ceiling).

  take   board offers whose Δ private value ≥ guard (sell a spare into a bid, buy a page card)
  list   spare copies at market prices (dups are worth 25 % / 10 % to us, 100 % to a collector)
  bid    for missing page cards (RET first) at prices well under our gain
Never trade on venues owned by the leaders (it would feed their market-making score).
"""
from __future__ import annotations

import math

from ..executor import Intent, PRIO_P2P

PARAMS = {
    "LIST_PRICE": {"common": 9, "uncommon": 24, "rare": 75, "epic": 170, "legendary": 420},
    "BID_PRICE": {"common": 10, "uncommon": 22, "rare": 70},
    "MAX_LISTINGS": 8,
    "MAX_BIDS": 6,
    "EXPIRY": 30,
    "BID_BUDGET": 120,         # primas we may commit to open bids at once
}


def venue_fee(venue: dict, price: int, n_cards: int) -> int:
    return math.ceil(venue.get("fee_bps", 0) * price / 10000) + venue.get("fee_per_card", 0) * n_cards


def _card_type(o_side: dict) -> list:
    return [t.split(":", 1)[1] for t in (o_side.get("types") or []) if t.startswith("card:")]


def take_from_board(board_offers: list, venue: dict, vb, held_assets: list, mine: set,
                    cash_avail: int = 10**9) -> list:
    out = []
    by_ref: dict = {}
    for a in held_assets:
        if a.get("kind") == "card":
            by_ref.setdefault(a["ref"], []).append(a)
    for o in board_offers:
        if o.get("id") in mine or o.get("status") != "open":
            continue
        g, w = o.get("give") or {}, o.get("want") or {}
        # someone buys a card type for cash → we may sell our cheapest-to-lose copy
        wanted = _card_type(w)
        if g.get("cash") and len(wanted) == 1 and not g.get("assets") and not w.get("cash"):
            ref = wanted[0]
            copies = sorted(by_ref.get(ref, []), key=vb.loss)
            if copies:
                a = copies[0]
                fee = venue_fee(venue, g["cash"], 1)
                d = g["cash"] - fee - vb.loss(a)
                out.append(Intent("accept", "p2p", {"offer_id": o["id"], "assets": [a["id"]]},
                                  priority=PRIO_P2P, delta=d,
                                  why=f"sell {ref} into bid {g['cash']} on {venue['venue']} (fee {fee})"))
        # someone sells cards for cash → buy if they complete pages / are worth more to us
        if g.get("assets") and w.get("cash") and not w.get("assets") and not w.get("types"):
            refs = [x["ref"] for x in g["assets"] if isinstance(x, dict) and x.get("kind") == "card"]
            if len(refs) != len(g["assets"]):
                continue
            fee = venue_fee(venue, w["cash"], len(refs))
            if w["cash"] + fee > cash_avail:
                continue
            gain = vb.gain(refs)
            if gain is None:
                continue
            d = gain - w["cash"] - fee
            out.append(Intent("accept", "p2p", {"offer_id": o["id"]}, priority=PRIO_P2P, delta=d,
                              why=f"buy {refs} for {w['cash']} on {venue['venue']} (fee {fee}, gain {gain:.0f})"))
    return [i for i in out if i.delta is not None and i.delta >= 2]


def list_spares(held_assets: list, vb, listed_asset_ids: set, venue: dict, n_open: int) -> list:
    out = []
    room = PARAMS["MAX_LISTINGS"] - n_open
    spares = []
    for a in held_assets:
        if a.get("kind") != "card" or a["id"] in listed_asset_ids:
            continue
        loss = vb.loss(a)
        price = PARAMS["LIST_PRICE"].get(a.get("rarity"), 0)
        if not price or loss == float("inf"):
            continue
        fee = venue_fee(venue, price, 1)
        d = price - fee - loss
        if d >= 3:
            spares.append((d, a, price))
    spares.sort(key=lambda x: -x[0])
    for d, a, price in spares[:max(0, room)]:
        out.append(Intent("list", "p2p", {"give": {"assets": [a["id"]]}, "want": {"cash": price},
                                          "venue": venue["venue"], "expires_in_ticks": PARAMS["EXPIRY"]},
                          priority=PRIO_P2P, delta=d, why=f"list spare {a['ref']} at {price} (loss {vb.loss(a):.1f})"))
    return out


def bid_for_pages(want_refs: list, vb, open_bid_refs: set, venue: dict, cash: int, n_open: int) -> list:
    out = []
    budget = min(PARAMS["BID_BUDGET"], cash)
    room = PARAMS["MAX_BIDS"] - n_open
    for ref in want_refs:
        if room <= 0 or ref in open_bid_refs:
            continue
        c = vb.cards.get(ref)
        if not c:
            continue
        price = PARAMS["BID_PRICE"].get(c["rarity"])
        gain = vb.gain([ref])
        if not price or gain is None or price > budget:
            continue
        fee = venue_fee(venue, price, 1)
        d = gain - price - fee
        if d < 5:
            continue
        out.append(Intent("list", "p2p", {"give": {"cash": price}, "want": {"cards": [ref]},
                                          "venue": venue["venue"], "expires_in_ticks": PARAMS["EXPIRY"]},
                          priority=PRIO_P2P, delta=d, why=f"bid {price} for {ref} (gain {gain:.0f})"))
        budget -= price
        room -= 1
    return out
