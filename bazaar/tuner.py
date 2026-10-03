"""Bounded self-improvement, driven only by real outcomes.

Every learner follows the same contract: learn from data the server gave us, change a
whitelisted parameter inside hard bounds, log the decision (state/tuning.log), persist it
(journal kv "params"), and freeze before the finale.

  duels    — after each finished wave, replay ALL our finished duels through the policy
             for a grid of parameters and adopt the best (if it beats the current by ≥ 3 %).
  dealers  — learn each dealer's final prices per (mode, rarity) from every public haggle
             (ours and other teams'); skip threads whose expected final cannot beat our
             reservation (saves hourly quota for deals that can score).
  p2p      — list prices per rarity follow fill rates: sold quickly ⇒ +1, expired ⇒ −1.
  broker   — after each Market Test, compare bench efficiency by broker mode and keep the
             best; one controlled session tries `--probe-spread`.
  scoring  — per-component score deltas logged every 10 ticks (attribution / calibration).
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import statistics
import subprocess
import time

from .executor import STATE
from .strategy import dealers as S_dealers
from .strategy import duels as S_duels
from .strategy import p2p as S_p2p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTEL = os.path.join(ROOT, "intel", "market.db")
LOG = os.path.join(STATE, "tuning.log")
FREEZE_AT_HOURS = 22.0               # Sunday ~13:00: no more changes before the finale

DUEL_GRID = {"GOOD_SHARE": [0.4, 0.5, 0.6, 0.7, 0.8, 0.9], "STALL_TICKS": [2, 3, 4], "SAFE_TICKS": [1, 2]}
LIST_BOUNDS = {"common": (6, 12), "uncommon": (18, 30), "rare": (60, 90), "epic": (140, 210), "legendary": (380, 500)}


def tlog(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print("TUNE " + msg, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def replay_duels(duels: list, params: dict) -> float:
    """Margin our policy would have captured on these finished duels with `params`."""
    saved = dict(S_duels.PARAMS)
    S_duels.PARAMS.update(params)
    total = 0.0
    try:
        for d in duels:
            msgs = d.get("messages") or []
            dl = d.get("deadline_tick")
            if not msgs or dl is None:
                continue
            start = min(m["tick"] for m in msgs)
            for t in range(start, dl):
                seen = [m for m in msgs if m["tick"] <= t]
                priced = [m for m in seen if m.get("price") is not None and m.get("from") == d.get("rival")]
                if not priced:
                    continue
                st = dict(d, status="live", messages=seen, your_offer=None,
                          rival_offer={"price": priced[-1]["price"], "days": priced[-1].get("days")})
                acc = [i for i in S_duels.plan([st], t, set()) if i.kind == "duel_accept"]
                if acc:
                    total += max(0.0, acc[0].delta or 0.0)
                    break
    finally:
        S_duels.PARAMS.clear()
        S_duels.PARAMS.update(saved)
    return total


class Tuner:
    def __init__(self, api, journal, log):
        self.api, self.j, self.log = api, journal, log
        self.n_done_duels = self.j.get("tuner:n_done_duels", 0)
        self.last_scores = None
        self.restore()

    # ── persistence ─────────────────────────────────────────────────────
    def restore(self) -> None:
        p = self.j.get("params", {})
        S_duels.PARAMS.update(p.get("duels", {}))
        for k, v in (p.get("p2p_list") or {}).items():
            S_p2p.PARAMS["LIST_PRICE"][k] = v
        if p:
            tlog(f"restored params {json.dumps(p)}")

    def save(self) -> None:
        self.j.put("params", {"duels": {k: S_duels.PARAMS[k] for k in DUEL_GRID},
                              "p2p_list": S_p2p.PARAMS["LIST_PRICE"]})

    # ── entry point (called once per tick by the agent, must stay cheap) ─
    def step(self, tick: int, t_hours: float, me: dict, live_duels: list) -> None:
        self.attribution(tick, me)
        if t_hours >= FREEZE_AT_HOURS:
            return
        try:
            if not live_duels:
                self.tune_duels()
            if tick % 30 == 0:
                self.learn_dealer_finals()
            if tick % 20 == 0:
                self.tune_list_prices()
            self.tune_broker(t_hours, me)
        except Exception as e:  # a learner must never break the trading loop
            tlog(f"learner error {e!r}")

    # ── scoring attribution ─────────────────────────────────────────────
    def attribution(self, tick: int, me: dict) -> None:
        sc = me.get("score") or {}
        keys = ("neg_points", "duel_points", "ladder_points", "mm_points", "bench_points", "bench_efficiency", "rank")
        cur = {k: sc.get(k) for k in keys}
        if self.last_scores and tick % 10 == 0:
            diff = {k: round((cur[k] or 0) - (self.last_scores[k] or 0), 3)
                    for k in keys if isinstance(cur[k], (int, float)) and isinstance(self.last_scores[k], (int, float))}
            moved = {k: v for k, v in diff.items() if v}
            if moved:
                tlog(f"tick {tick} score Δ10 {moved} now {cur}")
        if self.last_scores is None or tick % 10 == 0:
            self.last_scores = cur

    # ── duels ───────────────────────────────────────────────────────────
    def tune_duels(self) -> None:
        done = [d for d in self.api.duels(done=True).get("duels", []) if d.get("status") != "live"]
        if len(done) <= self.n_done_duels:
            return
        self.n_done_duels = len(done)
        self.j.put("tuner:n_done_duels", self.n_done_duels)
        # recent sessions matter most: rivals change their code between sessions
        sessions = sorted({d.get("session") for d in done if d.get("session") is not None})
        recent = [d for d in done if d.get("session") in sessions[-2:]] if len(sessions) >= 2 else done
        if sum(1 for d in recent if d.get("messages")) < 6:
            return
        current = {k: S_duels.PARAMS[k] for k in DUEL_GRID}
        base = replay_duels(recent, current)
        best, best_v = current, base
        for combo in itertools.product(*DUEL_GRID.values()):
            p = dict(zip(DUEL_GRID, combo))
            v = replay_duels(recent, p)
            if v > best_v + 1e-9:
                best, best_v = p, v
        # also report how the live results compare with the replay of the current policy
        real = sum(max(0.0, (d.get("result") or 0)) for d in recent)
        if best != current and best_v >= base * 1.03 + 1:
            S_duels.PARAMS.update(best)
            self.save()
            tlog(f"duels: {current} → {best} (replay {base:.0f} → {best_v:.0f} on {len(recent)} duels; real result sum {real:.2f})")
        else:
            tlog(f"duels: keep {current} (replay {base:.0f}, best {best_v:.0f}, {len(recent)} duels)")

    # ── dealers ─────────────────────────────────────────────────────────
    def learn_dealer_finals(self) -> None:
        """Median final price per (dealer, topic kind) from every public haggle we recorded."""
        if not os.path.exists(INTEL):
            return
        db = sqlite3.connect(INTEL)
        rows = db.execute("""SELECT m.with_, t.topic, m.price FROM thread_messages m
                             JOIN threads t ON t.id = m.thread
                             WHERE m.final = 1 AND m.price IS NOT NULL AND m.sender = m.with_""").fetchall()
        rarity_of_asset = dict(db.execute("SELECT asset_id, rarity FROM provenance WHERE rarity IS NOT NULL"))
        rarity_of_ref = dict(db.execute("SELECT ref, rarity FROM provenance WHERE rarity IS NOT NULL"))
        finals: dict = {}
        for dealer, topic, price in rows:
            try:
                topic = json.loads(topic) if topic else {}
            except ValueError:
                continue
            if not topic:
                continue
            mode = "sell" if "sell" in topic else "buy"
            what = (topic.get("buy") or {})
            if mode == "sell":
                ids = (topic.get("sell") or {}).get("assets") or [None]
                kind = rarity_of_asset.get(ids[0], "asset") if len(ids) == 1 else "lot"
            else:
                kind = what.get("pack") or what.get("rarity") or rarity_of_ref.get(what.get("card"), "card")
            finals.setdefault(f"{dealer}|{mode}|{kind}", []).append(price)
        summary = {k: {"n": len(v), "median": statistics.median(v)} for k, v in finals.items() if len(v) >= 2}
        if summary != self.j.get("dealer_finals"):
            self.j.put("dealer_finals", summary)
            S_dealers.PARAMS["FINALS"] = summary
            tlog(f"dealer finals learned: {summary}")

    # ── p2p ─────────────────────────────────────────────────────────────
    def tune_list_prices(self) -> None:
        """Our listings: sold (settled) vs expired, per rarity, over the last ~2 hours."""
        rows = self.j.db.execute("""SELECT args, result, ts FROM intents
                                    WHERE kind='list' AND source='p2p' AND status='ok' AND ts > ?""",
                                 (time.time() - 7200,)).fetchall()
        if not rows:
            return
        offers = {o["id"]: o for o in self.api.my_offers().get("offers", [])}
        stats: dict = {}
        for args, result, ts in rows:
            try:
                a, r = json.loads(args), json.loads(result or "{}")
            except ValueError:
                continue
            if "assets" not in a.get("give", {}):
                continue                                   # bids, not listings
            oid = r.get("id") or (r.get("offer") or {}).get("id")
            rarity = r.get("give", {}).get("assets", [{}])[0].get("rarity") if isinstance(r.get("give"), dict) else None
            if not rarity or oid is None:
                continue
            age = time.time() - ts
            still_open = oid in offers
            st = stats.setdefault(rarity, {"sold": 0, "stale": 0})
            if not still_open and age < 1800:
                st["sold"] += 1                              # gone quickly: sold
            elif still_open and age > 1200:
                st["stale"] += 1                             # nobody wants it at this price
        for rarity, st in stats.items():
            lo, hi = LIST_BOUNDS.get(rarity, (1, 10**6))
            cur = S_p2p.PARAMS["LIST_PRICE"].get(rarity)
            if cur is None:
                continue
            new = cur
            if st["sold"] >= 2 and st["stale"] == 0:
                new = min(hi, cur + 1)
            elif st["stale"] >= 2 and st["sold"] == 0:
                new = max(lo, cur - 1)
            if new != cur:
                S_p2p.PARAMS["LIST_PRICE"][rarity] = new
                self.save()
                tlog(f"p2p list price {rarity}: {cur} → {new} ({st})")

    # ── broker ──────────────────────────────────────────────────────────
    def tune_broker(self, t_hours: float, me: dict) -> None:
        """After a Market Test, record efficiency by mode; keep the best; probe the spread once."""
        sc = me.get("score") or {}
        eff = sc.get("bench_efficiency")
        if eff is None or not me.get("venue"):
            return
        hist = self.j.get("broker_hist", [])
        if hist and hist[-1]["eff"] == eff:
            return                                            # no new session result yet
        mode = _read(os.path.join(STATE, "BROKER_MODE"), "v2")
        flags = _read(os.path.join(STATE, "BROKER_FLAGS"), "")
        hist.append({"t": t_hours, "eff": eff, "mode": mode, "flags": flags})
        self.j.put("broker_hist", hist)
        tlog(f"broker: session result eff={eff} with mode={mode} {flags}")
        by: dict = {}
        for h in hist:
            by.setdefault(f"{h['mode']} {h['flags']}".strip(), []).append(h["eff"])
        tried_probe = any("probe" in k for k in by)
        if not tried_probe and t_hours < 15.5:
            choice = ("v2", "--probe-spread")
        else:
            best = max(by, key=lambda k: statistics.mean(by[k]))
            parts = best.split(" ", 1)
            choice = (parts[0], parts[1] if len(parts) > 1 else "")
        if choice != (mode, flags):
            _write(os.path.join(STATE, "BROKER_MODE"), choice[0])
            _write(os.path.join(STATE, "BROKER_FLAGS"), choice[1])
            tlog(f"broker: switching to {choice} (results so far { {k: round(statistics.mean(v), 3) for k, v in by.items()} })")
            # restart between sessions; launchd brings it back with the new mode
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.team15.broker"],
                           capture_output=True)


def _read(path: str, default: str) -> str:
    try:
        return open(path).read().strip() or default
    except OSError:
        return default


def _write(path: str, value: str) -> None:
    with open(path + ".tmp", "w") as f:
        f.write(value)
    os.replace(path + ".tmp", path)
