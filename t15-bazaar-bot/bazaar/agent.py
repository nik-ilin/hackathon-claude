"""Team 15 agent: one process, one key, one write path.

    BAZAAR_KEY=... python3 -m bazaar.agent [--dry-run]

Each tick: read (clock, me, duels, threads, offers) → value book → strategies emit
Intents → executor applies guard, limits, the single accept and kill switches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

from .api import Api, BazaarError, team_key
from .executor import STATE, Executor, Intent, Journal
from .strategy import dealers as S_dealers
from .strategy import duels as S_duels
from .strategy import p2p as S_p2p
from .tuner import Tuner
from .values import ValueBook

TEAM = "t15"
VENUE_BOND = 270
ALLOWED_P2P_VENUES = {"rastro"}           # + low-ranked team venues, refreshed live
NEVER_VENUES = {"v01", "v02", "v03"}       # leaders' markets: trading there feeds their MM score
NEVER_OWNERS = {"t06", "t12", "t13"}
DUEL_WINDOW_TICKS = 6                      # no new dealer threads this close to a duel deadline
VENUE_OPEN_AT = 6.85                       # just before the 7.0 Market Test (audit 2: cash first)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)


def alert(msg: str) -> None:
    log(f"ALERT {msg}")
    with open(os.path.join(STATE, "alerts.log"), "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


class Agent:
    def __init__(self, dry_run: bool):
        self.api = Api(team_key())
        self.j = Journal()
        self.ex = Executor(self.api, self.j, log, dry_run=dry_run)
        self.catalog = self.api.catalog()
        self.dealers: list = []
        self.venues: list = []
        self.last_tick = None
        self.blocked: dict = self.j.get("blocked", {})          # dealer -> until tick
        self.known_threads: set = set(self.j.get("known_threads", []))
        self.started_tick = None
        self.tuner = Tuner(self.api, self.j, log)
        self.top_teams: set = set(NEVER_OWNERS)
        self.vcache: dict = {}

    # ── reads ───────────────────────────────────────────────────────────
    def read(self) -> dict:
        s = {"clock": self.api.clock()}
        if s["clock"].get("paused"):
            return s
        s["me"] = self.api.me()
        s["duels"] = self.api.duels().get("duels", [])
        th = self.api.my_threads(status="open")
        s["threads"] = th.get("threads", th) if isinstance(th, dict) else th
        s["offers"] = self.api.my_offers().get("offers", [])
        tick = s["clock"]["tick"]
        if not self.dealers or tick % 10 == 0:
            self.dealers = self.api.dealers().get("personas", [])
            self.venues = self.api.venues().get("venues", [])
            try:
                lb = self.api.leaderboard()
                rows = lb.get("teams", []) if isinstance(lb, dict) else lb
                self.top_teams = {r.get("team") for r in rows if (r.get("rank") or 99) <= 5}
            except Exception:
                self.top_teams = set(NEVER_OWNERS)
            if tick % 20 == 0:
                self.catalog = self.api.catalog()
        return s

    def allowed_venues(self) -> list:
        """Rastro plus team venues whose owners are not in the top 5 (never feed the leaders)."""
        top = set(self.top_teams)
        out = []
        for v in self.venues:
            if v.get("status") != "open" or v.get("owner") == TEAM:
                continue
            if v["venue"] in NEVER_VENUES or v.get("owner") in NEVER_OWNERS:
                continue
            if v["venue"] in ALLOWED_P2P_VENUES or (v.get("owner") and v["owner"] not in top and not v.get("house")):
                out.append(v)
        return out

    # ── second agent detector ───────────────────────────────────────────
    def check_foreign_activity(self, threads: list) -> None:
        for t in threads:
            tid = t.get("id")
            if tid in self.known_threads:
                continue
            if (t.get("team") == TEAM and self.started_tick is not None
                    and (t.get("created_tick") or 0) >= self.started_tick
                    and not self._recent_open(t.get("with"), t.get("created_tick") or 0)):
                alert(f"thread {tid} with {t.get('with')} opened by someone else using our key — STOP_TRADING")
                open(os.path.join(STATE, "STOP_TRADING"), "w").close()
            self.known_threads.add(tid)       # pre-existing threads are adopted once
        self.j.put("known_threads", sorted(self.known_threads))

    def count_closed_deals(self, open_ids: set, round_no) -> None:
        """A dealer thread that left 'open' with status 'deal' is a ladder deal, whoever accepted."""
        prev = set(self.j.get("open_dealer_threads", []))
        for tid in prev - open_ids:
            try:
                th = self.api.thread(tid)
            except BazaarError:
                continue
            if th.get("status") == "deal" and not self.j.get(f"counted:{tid}"):
                k = f"deals:{th.get('with')}:{round_no}"
                self.j.put(k, self.j.get(k, 0) + 1)
                self.j.put(f"counted:{tid}", True)
                log(f"LADDER deal closed with {th.get('with')} (thread {tid}) — {k} = {self.j.get(k)}")
        self.j.put("open_dealer_threads", sorted(open_ids))

    def _recent_open(self, did, ctick: int) -> bool:
        p = self.j.get(f"pending_open:{did}")
        return p is not None and abs(ctick - p) <= 3

    # ── one tick ────────────────────────────────────────────────────────
    def tick(self, s: dict) -> None:
        clock, me = s["clock"], s["me"]
        tick, limits = clock["tick"], clock.get("limits", {})
        tick_end = time.time() + max(1.5, float(clock.get("next_tick_in", clock.get("tick_seconds", 30))) - 1.5)
        if self.started_tick is None:
            self.started_tick = tick
            for t in s["threads"]:
                self.known_threads.add(t.get("id"))
        self.check_foreign_activity(s["threads"])
        self.j.score(tick, me.get("score", {}))
        vb = ValueBook(self.api, me, self.catalog, cache=self.vcache)
        assets = me.get("assets", [])
        cash = int(me.get("cash", 0))
        round_no = clock.get("round")
        intents: list = []

        # 0. sealed packs → open (contents are luck; the cards are ours either way)
        for a in assets:
            if a.get("kind") == "pack" and float(clock.get("t_hours", 0)) >= 4.0:
                intents.append(Intent("open_pack", "ops", {"asset_id": a["id"]}, why=f"open {a['ref']}"))

        # 1. duels
        live = [d for d in s["duels"] if d.get("status") == "live"]
        duel_its = S_duels.plan(live, tick, set())
        if duel_its:                    # act on the offers we just evaluated, before anything slow
            self.ex.run_tick(tick, limits, duel_its, tick_end)
            if any(i.kind == "duel_accept" and i.result is not None and not isinstance(i.result, BazaarError)
                   for i in duel_its):
                limits = {**limits, "accepts_per_team_per_tick": 0}
        duel_soon = bool(live)          # a duel wave owns the team's single accept per tick

        # 2. venue: open our board market once cash allows (bond refundable)
        venue_mine = me.get("venue")
        reserve = 0
        if not venue_mine and int(me.get("level", 1)) >= 2:
            if cash >= VENUE_BOND + 5 and float(clock.get("t_hours", 0)) >= VENUE_OPEN_AT and not self.j.get("venue_opened"):
                self.open_venue(tick)
                if self.j.get("venue_opened"):
                    cash -= VENUE_BOND
            elif not self.j.get("venue_opened"):
                reserve = VENUE_BOND + 5           # keep cash for the bond
        S_dealers.PARAMS["CASH_RESERVE"] = reserve

        # 3. dealers: continue open threads, then open new ones
        want_refs = self.want_refs(vb)
        open_persona = {t["with"]: t for t in s["threads"] if t.get("kind") == "persona"}
        self.count_closed_deals({t["id"] for t in open_persona.values()}, round_no)
        for dealer_id, t in open_persona.items():
            try:
                th = self.api.thread(t["id"])
            except BazaarError:
                continue
            conv = self.j.get(f"conv:{t['id']}", {})
            if "reservation" not in conv or conv["reservation"] is None:
                conv["reservation"] = self.reservation_for(th, vb)
            elif "sell" in (th.get("topic") or {}):
                fresh = self.reservation_for(th, vb)
                conv["reservation"] = None if fresh is None else max(conv["reservation"], fresh)
            if conv["reservation"] is None:
                intents.append(Intent("close_thread", "dealer", {"thread_id": t["id"]}, why="asset gone"))
                continue
            if "rarity" not in conv:
                conv["rarity"] = self.topic_rarity(th.get("topic") or {}, vb)
            intents += S_dealers.haggle(th, conv, vb, tick)
            self.j.put(f"conv:{t['id']}", conv)
        reserved_assets: set = set()
        if not duel_soon:
            listed = {a["id"] for o in s["offers"] if o.get("maker") == TEAM
                      for a in (o.get("give") or {}).get("assets", []) if isinstance(a, dict)}
            free_assets = [a for a in assets if a.get("id") not in listed]
            for t in open_persona.values():                # assets already offered to a dealer
                reserved_assets.update((t.get("topic") or {}).get("sell", {}).get("assets", []))
            free_assets = [a for a in free_assets if a.get("id") not in reserved_assets]
            for d in self.dealers:
                did = d.get("id")
                if d.get("status") != "active" or did not in me.get("unlocked", []) or did in open_persona:
                    continue
                if self.blocked.get(did, -1) > tick:
                    continue
                done = self.j.get(f"deals:{did}:{round_no}", 0)
                pick = S_dealers.choose_topic(d, vb, free_assets, done, cash, want_refs)
                if pick:
                    topic, res, why = pick
                    reserved_assets.update(topic.get("sell", {}).get("assets", []))
                    intents.append(Intent("open_thread", "dealer", {"with": did, "topic": topic},
                                          why=f"{did}: {why} res {res:.0f}"))
                    self.j.put(f"pending_res:{did}", res)

        # 4. P2P on allowed venues
        mine = {o["id"] for o in s["offers"] if o.get("maker") == TEAM}
        if self.started_tick == tick:
            intents += self.cancel_legacy_offers(s["offers"], tick)
        allowed = self.allowed_venues()
        cash_avail = cash - reserve
        if not (tick % 2 or duel_soon):
            for v in allowed:
                try:
                    board = self.api.board(v["venue"]).get("offers", [])
                except BazaarError:
                    continue
                intents += S_p2p.take_from_board(board, v, vb, assets, mine, cash_avail)
        # offers addressed to us
        to_us = [o for o in s["offers"] if o.get("to") == TEAM and o.get("maker") != TEAM]
        rastro = next((v for v in self.venues if v["venue"] == "rastro"), {"venue": "rastro", "fee_bps": 500, "fee_per_card": 1})
        if not duel_soon:
            for v in allowed:
                here = [o for o in to_us if o.get("venue") == v["venue"]]
                if here:
                    intents += S_p2p.take_from_board(here, v, vb, assets, mine, cash_avail)
        my_listings = [o for o in s["offers"] if o.get("maker") == TEAM and (o.get("give") or {}).get("assets")]
        my_bids = [o for o in s["offers"] if o.get("maker") == TEAM and (o.get("give") or {}).get("cash")]
        listed_ids = {a["id"] for o in my_listings for a in o["give"]["assets"] if isinstance(a, dict)} | reserved_assets
        if tick % 3 == 0:
            intents += S_p2p.list_spares(assets, vb, listed_ids, rastro, len(my_listings))
            bid_refs = {t.split(":", 1)[1] for o in my_bids for t in (o.get("want") or {}).get("types", [])}
            intents += S_p2p.bid_for_pages(want_refs, vb, bid_refs, rastro, cash - reserve, len(my_bids))

        if duel_soon:
            intents = [i for i in intents if not (i.kind in ("accept", "duel_accept") and i.source != "duel")]
        done = self.ex.run_tick(tick, limits, intents, tick_end)
        self.after(done, intents, tick, round_no)
        if time.time() < tick_end:            # learning only with time to spare in the tick
            self.tuner.step(tick, float(clock.get("t_hours", 0)), me, live)

    def after(self, done: list, intents: list, tick: int, round_no) -> None:
        for it in intents:
            r = it.result
            if it.kind == "open_thread":
                did = it.args["with"]
                self.j.put(f"pending_open:{did}", tick)
                if isinstance(r, BazaarError):
                    if r.code in ("persona_quota", "cooloff", "sold_out", "locked"):
                        until = (r.extra or {}).get("until_tick") or tick + (120 if r.code == "persona_quota" else 20)
                    else:
                        until = tick + 10                  # any other refusal: don't hammer it
                    self.blocked[did] = int(until)
                    self.j.put("blocked", self.blocked)
                elif isinstance(r, dict):
                    tid = r.get("id") or r.get("thread_id") or (r.get("thread") or {}).get("id")
                    if tid:
                        self.known_threads.add(tid)
                        self.j.put(f"conv:{tid}", {"reservation": self.j.get(f"pending_res:{did}")})
        self.j.put("known_threads", sorted(x for x in self.known_threads if x is not None))

    # ── helpers ─────────────────────────────────────────────────────────
    def cancel_legacy_offers(self, offers: list, tick: int) -> list:
        """Offers we did not create through this executor (Friday, other agents): withdraw them."""
        ours = self.j.our_message_ids()
        return [Intent("cancel", "p2p", {"offer_id": o["id"]}, why="legacy offer, not guard-checked")
                for o in offers if o.get("maker") == TEAM and o.get("status") == "open" and o["id"] not in ours
                and not o.get("thread")]

    def reservation_for(self, th: dict, vb: ValueBook) -> float:
        topic = th.get("topic") or {}
        if "sell" in topic:
            ids = set(topic["sell"].get("assets", []))
            assets = [a for a in vb.me["assets"] if a["id"] in ids]
            return max(1.0, sum(vb.loss(a) for a in assets)) if assets else None
        buy = topic.get("buy", {})
        if "card" in buy:
            g = vb.gain([buy["card"]])
            return 0.0 if g is None else g * S_dealers.PARAMS["MAX_BUY_SHARE"]
        if "pack" in buy:
            return vb.pack_ev(buy["pack"])
        return 0.0

    @staticmethod
    def topic_rarity(topic: dict, vb: ValueBook):
        buy = topic.get("buy", {})
        if "card" in buy and buy["card"] in vb.cards:
            return vb.cards[buy["card"]]["rarity"]
        if "sell" in topic:
            ids = set(topic["sell"].get("assets", []))
            for a in vb.me["assets"]:
                if a["id"] in ids:
                    return a.get("rarity")
        return buy.get("rarity")

    def want_refs(self, vb: ValueBook) -> list:
        """Missing page cards, best pages first (RET, then by affinity and closeness)."""
        released = {x["id"] for x in self.catalog.get("sets", []) if x.get("released")}
        sets = sorted(vb.affinity, key=lambda s: (-vb.affinity[s]))
        out = []
        for s in sets:
            if s not in released or vb.affinity[s] < 1.0:
                continue
            have, of, complete = vb.page_state(s)
            if complete:
                continue
            refs = [r for r, c in vb.cards.items() if c["set"] == s and c["rarity"] in ("common", "uncommon", "rare")
                    and not vb.held[r]]
            order = {"common": 0, "uncommon": 1, "rare": 2}
            out += sorted(refs, key=lambda r: order[vb.cards[r]["rarity"]])
        return out

    def open_venue(self, tick: int) -> None:
        if self.ex.dry or self.ex.stopped("all"):
            log("[DRY] would open venue")
            return
        try:
            r = self.api.open_venue("Mercado Quince · 0 %", fee_bps=0, fee_per_card=0,
                                    rules={"mechanism": "board"},
                                    description="Cero comisión. Un broker cruza los mejores pares cada tick.")
        except BazaarError as e:
            log(f"open_venue refused {e.code}: {e.message}")
            return
        path = os.path.join(STATE, "venue_open.json")
        with open(path, "w") as f:
            json.dump(r, f)
            f.flush()
            os.fsync(f.fileno())
        key = r.get("broker_key")
        if key:
            tmp = os.path.join(STATE, "broker_key.txt.tmp")
            with open(tmp, "w") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(STATE, "broker_key.txt"))
        self.j.put("venue_opened", tick)
        alert(f"venue opened at tick {tick}: {json.dumps({k: v for k, v in r.items() if k != 'broker_key'})}")

    # ── loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        log(f"agent up (dry_run={self.ex.dry})")
        while True:
            try:
                with open(os.path.join(STATE, "heartbeat.agent"), "w") as f:
                    f.write(str(time.time()))
                c = self.api.clock()
                if c.get("paused"):
                    time.sleep(5)
                    continue
                if c["tick"] == self.last_tick:
                    time.sleep(min(1.0, max(0.2, float(c.get("next_tick_in", 1)) / 2)))
                    continue
                self.last_tick = c["tick"]
                s = self.read()
                if s["clock"].get("paused"):
                    continue
                self.tick(s)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"tick error: {e!r}\n{traceback.format_exc(limit=3)}")
                time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run:
        ap.error("Agente alternativo en evaluación: usa --dry-run; para operar usa bazaar-kit/run.sh memory")
    Agent(dry_run=True).run()


if __name__ == "__main__":
    main()
