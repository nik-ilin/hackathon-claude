"""Dealers (the ladder): haggle to the dealer's `final:true`, only on value-positive topics.

Ladder = share of each dealer's price range we capture, best 3 deals per level per round.
So for each dealer and round we want 3 negotiated deals that reach the dealer's final
offer (= its limit), and each deal must also be ≥ 0 in private value (guard).

Topics (first that applies):
  sell  a spare copy / low-value card the dealer buys (loss small)          → cash in
  buy   a missing page card (RET first, then MAL…) when our gain ≫ list price → page bonus
Never buy packs unless their private EV beats the price.
Haggle: Boulware — start far, move 1–2 P per tick (never repeat a price), accept only
the dealer's final offer or an offer that already crosses our next price (AC_next).

Leaders' audit (Saturday, threads of t18/t12/t02/t13):
  * El Chato mirrors our step ("cuatro tuyos, cuatro míos"): with +1 steps he holds 33 for
    three rounds; with +4 he moves 3–4. He accepts our price himself when it is 1–2 below his.
  * After Chato's `final`, a counter at final − 1 was accepted 2/2 (t13 LAV-06, LAV-07).
  * La Abuela concedes ~1 per round and her final drops with patience: packs 19–21 with
    +1 steps (t13) vs 22–24 with +2 (t12). She sells uncommons at 21–22, Chato at 27–31.
  * Ladder counts share of range, not price: cheap commons (Abuela 12 → 9) score like rares.
"""
from __future__ import annotations

import random
from typing import Optional

from ..executor import Intent, PRIO_DEALER, PRIO_DEALER_FINAL

PARAMS = {
    "SELL_ANCHOR": 2.2,       # first ask = dealer opening bid × 2.2 (Abuela common 5 → 11)
    "BUY_ANCHOR": 0.55,       # first bid = dealer opening ask × 0.55 (Abuela pack 30 → 16)
    "STEP": 2,                # primas per tick toward the dealer (t12: +2 got the pack to 21)
    # per dealer and rarity, from the best observed haggles (audit round 2)
    "STEP_BY": {("chato", "rare"): 4, ("chato", "uncommon"): 3, ("chato", "sell"): 3,
                ("abuela", "common"): 1, ("abuela", "uncommon"): 1, ("abuela", "sell"): 1,
                ("abuela", None): 1},          # None = pack topic (no rarity)
    "ANCHOR_BY": {("chato", "rare"): 0.70},   # t18: 70 → 86, best rare deal observed
    "UNDERCUT_DEALERS": {"chato"},            # counter final − 1 once, then take the final
    "CHEAPER_BY": 3,          # skip a buy if another dealer's learned final is ≥ 3 P cheaper
    "DEALS_PER_ROUND": 3,     # ladder counts best 3 per dealer per round
    "MAX_BUY_SHARE": 0.75,    # never pay more than 75 % of our gain for a card
    "CASH_RESERVE": 0,        # set by the agent (venue bond, etc.)
    "FINALS": {},             # learned by the tuner: "dealer|mode|kind" -> {n, median}
}


def expected_final(dealer_id: str, mode: str, kind: str):
    f = PARAMS["FINALS"].get(f"{dealer_id}|{mode}|{kind}")
    return f["median"] if f and f.get("n", 0) >= 2 else None

_SELL_TXT = ["¿{p} P, Abuela? Es una buena carta.", "Le dejo esta por {p} P.", "{p} P y es suya.",
             "Venga, {p} P.", "Por {p} P se la queda.", "¿Qué le parece {p} P?", "{p} P, último esfuerzo."]
_BUY_TXT = ["¿Me la deja en {p} P?", "Le ofrezco {p} P.", "¿{p} P le parece bien?", "Subo a {p} P.",
            "{p} P, de corazón.", "¿Cerramos en {p} P?", "Puedo llegar a {p} P."]


def cheaper_elsewhere(dealer_id: str, rarity: str, ef: Optional[float]) -> bool:
    """Another dealer's learned final for buying this rarity beats this one by CHEAPER_BY."""
    if ef is None:
        return False
    for key in PARAMS["FINALS"]:
        d, mode, kind = (key.split("|") + ["", ""])[:3]
        if d != dealer_id and mode == "buy" and kind == rarity:
            other = expected_final(d, "buy", rarity)
            if other is not None and other <= ef - PARAMS["CHEAPER_BY"]:
                return True
    return False


def dealer_price(offer: dict, mode: str) -> Optional[int]:
    """Cash side of a dealer's standing offer: it wants cash when we buy, gives cash when we sell."""
    side = "want" if mode == "buy" else "give"
    c = (offer.get(side) or {}).get("cash")
    return int(c) if c else None


def thread_mode(topic: dict) -> str:
    return "sell" if "sell" in (topic or {}) else "buy"


def haggle(thread: dict, conv: dict, vb, tick: int) -> list:
    """One thread, one tick. `conv` is our persisted memory for the thread
    (prices sent, reservation). Returns intents."""
    tid = thread["id"]
    mode = thread_mode(thread.get("topic") or {})
    dealer = thread["with"]
    standing = [o for o in thread.get("standing_offers", [])
                if o.get("maker") == dealer and o.get("status") == "open"]
    msgs = [m for m in thread.get("messages", []) if m.get("sender") == dealer and m.get("offer")]
    last = standing[-1] if standing else (msgs[-1]["offer"] if msgs else None)
    their = dealer_price(last, mode) if last else None
    final = bool(last and last.get("final"))
    res = conv.get("reservation")            # most we pay / least we take
    if their is not None:
        conv.setdefault("first_dealer_price", their)
    rounds = len(conv.get("sent", []))
    out = []

    def delta_at(p: int) -> Optional[float]:
        if res is None:
            return None
        return (res - p) if mode == "buy" else (p - res)

    # 1. accept: dealer's final inside our reservation, or their price already crosses ours
    if last and their is not None and standing:
        ours = conv.get("sent", [None])[-1] if conv.get("sent") else None
        crosses = ours is not None and ((mode == "buy" and their <= ours) or (mode == "sell" and their >= ours))
        d = delta_at(their)
        if (final and dealer in PARAMS["UNDERCUT_DEALERS"] and mode == "buy" and d is not None and d >= 0
                and conv.get("undercut") is None and their - 1 > (ours or 0)):
            conv["undercut"] = their                # one counter at final − 1; next tick take the final
            conv.setdefault("sent", []).append(their - 1)
            out.append(Intent("say", "dealer", {"thread_id": tid, "text": f"{their - 1} P y cerramos ya.",
                                                "price": their - 1},
                              priority=PRIO_DEALER, why=f"{dealer} t{tid} final {their} → counter {their - 1}"))
            return out
        if (final or crosses) and d is not None and d >= 0 and (rounds >= 1 or final):
            out.append(Intent("accept", "dealer", {"offer_id": last["id"]},
                              priority=PRIO_DEALER_FINAL if final else PRIO_DEALER, delta=d,
                              why=f"{dealer} t{tid} {mode} {'FINAL' if final else 'cross'} {their} res {res}"))
            conv["accepting"] = tick
            return out
        if final:                                   # final outside our reservation: walk
            out.append(Intent("close_thread", "dealer", {"thread_id": tid}, why=f"{dealer} final {their} > res {res}"))
            return out

    # 2. next price (Boulware: steady small steps, never repeat, never cross reservation)
    if their is None or res is None:
        return out
    sent = conv.setdefault("sent", [])
    rarity = conv.get("rarity")
    if not sent:
        anchor = PARAMS["ANCHOR_BY"].get((dealer, rarity), PARAMS["BUY_ANCHOR"])
        p = int(round(their * PARAMS["SELL_ANCHOR"])) if mode == "sell" else max(1, int(round(their * anchor)))
    else:
        step = PARAMS["STEP_BY"].get((dealer, "sell" if mode == "sell" else rarity), PARAMS["STEP"])
        p = sent[-1] - step if mode == "sell" else sent[-1] + step
    if mode == "sell":
        p = max(p, int(-(-res // 1)), their + 1)        # never below reservation, stay above their bid
    else:
        p = min(p, int(res), their - 1)                 # never above reservation, stay below their ask
    if p in sent or p <= 0:
        return out                                      # nowhere left to move: wait for their final
    sent.append(p)
    pool = _SELL_TXT if mode == "sell" else _BUY_TXT
    out.append(Intent("say", "dealer", {"thread_id": tid, "text": random.choice(pool).format(p=p), "price": p},
                      priority=PRIO_DEALER, why=f"{dealer} t{tid} {mode} ours {p} theirs {their} res {res}"))
    return out


def choose_topic(dealer: dict, vb, held_assets: list, deals_done: int, cash: int,
                 want_refs: list) -> Optional[tuple]:
    """(topic, reservation, why) for a new thread, or None."""
    menu = dealer.get("menu") or {}
    buys = {b["rarity"] for b in menu.get("buys", []) if "rarity" in b}
    sells = {s["rarity"]: s for s in menu.get("sells", []) if "rarity" in s}

    # sell: cheapest-to-lose card the dealer buys
    cands = []
    for a in held_assets:
        if a.get("kind") != "card" or a.get("rarity") not in buys:
            continue
        loss = vb.loss(a)
        if loss == float("inf"):
            continue
        cands.append((loss, a))
    cands.sort(key=lambda x: x[0])
    if cands and deals_done < PARAMS["DEALS_PER_ROUND"] + 3:
        loss, a = cands[0]
        list_price = (sells.get(a["rarity"]) or {}).get("list_price") or vb.book[a["rarity"]]
        ef = expected_final(dealer.get("id", ""), "sell", a["rarity"])
        if ef is not None and ef < loss:                # learned: this dealer's final can't beat our loss
            cands = []
        elif loss < 0.6 * list_price:                   # dealers pay ~50–60 % of list
            return ({"sell": {"assets": [a["id"]]}}, max(1.0, loss), f"sell {a['ref']} loss {loss:.1f}")

    # buy: a missing page card the dealer sells, if our gain dwarfs the price
    for ref in want_refs:
        c = vb.cards.get(ref)
        if not c or c["rarity"] not in sells:
            continue
        gain = vb.gain([ref])
        lp = sells[c["rarity"]].get("list_price", 0)
        if gain is None or not lp:
            continue
        res = min(gain * PARAMS["MAX_BUY_SHARE"], cash - PARAMS["CASH_RESERVE"])
        ef = expected_final(dealer.get("id", ""), "buy", c["rarity"])
        if ef is not None and ef > res:                 # learned: their final is above what we'd pay
            continue
        if cheaper_elsewhere(dealer.get("id", ""), c["rarity"], ef):
            continue                                    # e.g. uncommons: Abuela 22 vs Chato 27
        if res >= 0.8 * lp:
            return ({"buy": {"card": ref}}, float(int(res)), f"buy {ref} gain {gain:.0f} list {lp}")
    return None
