"""Duels: hybrid 'strategic silence' policy, deterministic, never outside our limit.

Evidence (Friday practice, 24 duels): most rivals concede every tick without needing a
reply; a few make an 'exploding' first offer that later worsens; ~25 % never speak.
Policy per live duel, each tick:
  1. Rival offer inside our limit and good enough now (≥ GOOD_SHARE of our limit as margin),
     or worsening / stalled for STALL_TICKS ⇒ accept (arbitrated: one accept per tick).
  2. Rival still conceding ⇒ stay silent (talk costs decay).
  3. No rival offer at deadline − SPEAK_AT ⇒ one anchored offer of ours.
  4. Last ticks ⇒ accept the best rival offer inside our limit; near-deadline accepts get
     top priority, ordering the duels whose rival concedes least first.
Two-issue duels (price + days) compare offers by total utility using `your_days_weight`.
"""
from __future__ import annotations

from typing import Optional

from ..executor import Intent, PRIO_DUEL, PRIO_DUEL_DEADLINE

PARAMS = {
    "GOOD_SHARE": 0.60,      # accept at once if margin ≥ 60 % of our limit (replay: 98 % of best)
    "STALL_TICKS": 3,        # rival unchanged this many ticks ⇒ accept if inside limit
    "SPEAK_AT": 5,           # ticks before deadline to speak if rival silent
    "ANCHOR": 0.30,          # our single anchored offer: buyer 0.70·L, seller 1.30·C
    "SAFE_TICKS": 1,         # accept at deadline − SAFE_TICKS at the latest (+1 per concurrent duel)
}


def _days_value(duel: dict, days: Optional[int]) -> float:
    """Our utility (in primas) from the delivery day, if the duel negotiates days."""
    w = duel.get("your_days_weight")
    if days is None or not w:
        return 0.0
    if isinstance(w, dict):
        return float(w.get(str(days), w.get(days, 0)) or 0)
    if isinstance(w, list) and 0 <= days < len(w):
        return float(w[days])
    if isinstance(w, (int, float)):
        return float(w) * days
    return 0.0


def margin(duel: dict, price: Optional[int], days: Optional[int] = None) -> Optional[float]:
    """Our surplus in primas if we close at this price (negative = outside our limit)."""
    if price is None:
        return None
    lim = float(duel["your_limit"])
    m = (lim - price) if duel["role"] == "buyer" else (price - lim)
    if m < 0:
        return m                     # days can never excuse a price outside our limit
    return m + _days_value(duel, days)


def rival_trajectory(duel: dict) -> list:
    """[(tick, price)] of the rival's priced messages, oldest first."""
    rival = duel.get("rival")
    return [(m["tick"], m["price"]) for m in duel.get("messages", [])
            if m.get("price") is not None and m.get("from") == rival]


def plan(duels: list, tick: int, our_alias_seen: set) -> list:
    intents = []
    # one accept per tick for the whole team: duels sharing a deadline must be staggered
    acceptable_by_deadline: dict = {}
    for d in duels:
        if d.get("status") == "live":   # every live duel of the wave may need its own tick
            acceptable_by_deadline[d.get("deadline_tick")] = acceptable_by_deadline.get(d.get("deadline_tick"), 0) + 1
    for d in duels:
        if d.get("status") != "live":
            continue
        deadline = d.get("deadline_tick")
        if deadline is None:
            continue
        left = deadline - tick
        ro = d.get("rival_offer") or {}
        price, days = ro.get("price"), ro.get("days")
        m = margin(d, price, days)
        lim = float(d["your_limit"])
        traj = rival_trajectory(d)
        # how much the rival improved over the last 3 ticks (slope, primas/tick in our favour)
        slope = 0.0
        if len(traj) >= 2:
            (t0, p0), (t1, p1) = traj[max(0, len(traj) - 4)], traj[-1]
            if t1 > t0:
                imp = (p0 - p1) if d["role"] == "buyer" else (p1 - p0)
                slope = imp / (t1 - t0)
        stalled = len(traj) >= 2 and all(p == traj[-1][1] for _, p in traj[-PARAMS["STALL_TICKS"]:]) \
            and len(traj) >= PARAMS["STALL_TICKS"]
        worsening = slope < 0
        why_base = f"duel {d['duel']} {d['role']} lim {lim:.0f} rival {price} days {days} left {left} slope {slope:.1f}"

        safe = PARAMS["SAFE_TICKS"] + max(0, acceptable_by_deadline.get(deadline, 1) - 1)
        if m is not None and m >= 0:
            accept_now = (
                left <= safe
                or m >= PARAMS["GOOD_SHARE"] * lim
                or worsening
                or stalled
            )
            if accept_now:
                prio = PRIO_DUEL_DEADLINE if left <= safe + 1 else PRIO_DUEL
                prio += max(0, 10 - int(slope))   # least-conceding first
                intents.append(Intent("duel_accept", "duel", {"duel_id": d["duel"]}, priority=prio,
                                      delta=m, why=why_base + " ⇒ accept"))
                continue

        # nobody is moving our way and time is running out: say one anchored number
        we_spoke = d.get("your_offer") is not None
        if (price is None or (m is not None and m < 0)) and left <= PARAMS["SPEAK_AT"] and not we_spoke:
            a = PARAMS["ANCHOR"]
            ours = int(round(lim * (1 - a))) if d["role"] == "buyer" else int(round(lim * (1 + a)))
            if price is not None:                      # never ask for worse than the rival already offers
                ours = min(ours, int(lim)) if d["role"] == "buyer" else max(ours, int(lim))
            args = {"duel_id": d["duel"], "text": f"{ours} P y cerramos ahora.", "price": ours}
            if "days" in (d.get("issues") or []):
                args["days"] = _best_days(d)
            intents.append(Intent("duel_say", "duel", args, priority=PRIO_DUEL, why=why_base + " ⇒ anchor"))
    return intents


def _best_days(d: dict) -> int:
    best, best_v = 0, float("-inf")
    for k in range(0, 11):
        v = _days_value(d, k)
        if v > best_v:
            best, best_v = k, v
    return best
