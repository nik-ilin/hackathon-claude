"""Clearing House client: runs on YOUR machine with YOUR key. One file, standard library only. Read it: the key goes
only to the game (BAZAAR_URL, header X-Team-Key); the clearing server receives card refs, asset ids and your
reservation prices, nothing else.

Trust model: the server is NOT trusted. `book` keeps a local copy of what you listed; `execute` posts a sell only
if the server's offer body gives exactly one of YOUR listed spares, for plain cash at or above YOUR min, addressed
to the buyer the plan names (check_sell); it accepts a buy only after reading that offer from the game's PUBLIC
venue book and seeing it is addressed to you, gives one copy of the plan's card, wants cash only, at the plan price,
within YOUR max including the fee (check_buy). Anything else is refused and reported back as failed. An offer that
wants one of your cards is never accepted.

    export BAZAAR_KEY=<your team key>
    python3 clearing_client.py --server https://<clearing> join --team t07 --venue v29 --invite <code>
    python3 clearing_client.py --server https://<clearing> book [--margin 0.15] [--sell-sets MAL,CHA] [--keep LAV-09]
    python3 clearing_client.py --server https://<clearing> plan                     # the proposal: review it
    python3 clearing_client.py --server https://<clearing> vote --ok | --no --why "..." [--trades r1-3]
    python3 clearing_client.py --server https://<clearing> execute [--until 13:50] [--auto-approve] [--dry-run]

book (full, the default): your haves are every duplicate copy (one copy of each card stays), every card of a set
you value below book (your multiplier < 1: you are not collecting it) and every card of --sell-sets, at
min = ceil(your_value * (1 + margin)); your wants are every page card with a value above --floor, held or not, at
max = floor(your_value * (1 - margin)), from GET /api/me/value (which includes page bonuses). A full book is what
lets the matcher find a profitable trade between almost any two teams (multipliers differ per team), which is
what covers every venue. --keep never sells; --no-full keeps only duplicates and missing cards.
execute: every 15 s reads your plan; posts each pending sell exactly once (addressed to the buyer) and reports the
offer id; accepts each buy once the offer id is known, one accept per tick, if you have the cash. --dry-run prints.
The token is kept in ~/.clearing_<team>.json."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

GAME = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai").rstrip("/")


def call(url: str, body=None, key: str | None = None, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-Team-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
        except Exception:
            err = {"error": str(e.code)}
        err["_status"] = e.code
        return err


def key_or_die() -> str:
    k = os.environ.get("BAZAAR_KEY")
    if not k:
        sys.exit("set BAZAAR_KEY (your team key; it is sent only to the game)")
    return k


def cfg_path(team: str) -> Path:
    return Path.home() / f".clearing_{team}.json"


def save_cfg(cfg: dict) -> None:
    """Token, my reservation prices and what I approved: written atomically, readable by me only (0600)."""
    p = cfg_path(cfg["team"])
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(cfg))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def load_cfg(a) -> dict:
    cands = [cfg_path(a.team)] if a.team else sorted(Path.home().glob(".clearing_t??.json"))
    for p in cands:
        if p.exists():
            return json.loads(p.read_text())
    sys.exit("no token: run join first")


def cmd_join(a) -> None:
    team = a.team
    if not team:
        me = call(f"{GAME}/api/me", key=key_or_die())
        team = me.get("id") or sys.exit(f"cannot read /api/me: {me}")
    body = {"team": team}
    if a.venue:
        body["venue"] = a.venue
    if a.invite:
        body["invite"] = a.invite
    p = cfg_path(team)
    if p.exists():
        body["token"] = json.loads(p.read_text())["token"]
    r = call(f"{a.server}/api/clearing/join", body)
    if "token" not in r:
        sys.exit(f"join refused: {r}")
    save_cfg({"team": team, "token": r["token"], "server": a.server})
    print(f"joined as {team} (venue {r.get('venue')}); token saved in {p} (0600)")


def cmd_book(a) -> None:
    cfg, key = load_cfg(a), key_or_die()
    me = call(f"{GAME}/api/me", key=key)
    cards = [x for x in me.get("assets") or [] if isinstance(x, dict) and x.get("kind", "card") == "card" and x.get("ref")]
    if not cards and "error" in me:
        sys.exit(f"cannot read /api/me: {me}")
    keep = set((a.keep or "").split(",")) - {""}
    sell_sets = set((a.sell_sets or "").upper().split(",")) - {""}
    held: dict = {}
    for c in cards:
        held.setdefault(c["ref"], []).append(c)
    catalog = call(f"{GAME}/api/catalog")
    book_of = {c.get("id"): float(c.get("book") or 0) for st in (catalog.get("sets") or []) if isinstance(st, dict)
               for c in (st.get("cards") or []) if isinstance(c, dict) and c.get("id")}
    haves = []
    for ref, copies in held.items():
        if ref in keep:
            continue
        copies = sorted(copies, key=lambda c: (c.get("your_value") or 0, c["id"]))
        book_price = book_of.get(ref) or float(copies[-1].get("book") or 0)
        mult = (float(copies[-1].get("your_value") or 0) / book_price) if book_price else None
        whole_set = ref[:3] in sell_sets or (a.full and mult is not None and mult < 1.0)
        spare = copies if whole_set else copies[:-1]      # one copy of each card stays unless the set is for sale
        for c in spare:
            v = float(c.get("your_value") or 0)
            haves.append({"card": ref, "asset": int(c["id"]), "min": max(1, int(math.ceil(v * (1 + a.margin))))})
    refs = sorted({c.get("id") for s in (catalog.get("sets") or []) if isinstance(s, dict)
                   for c in (s.get("cards") or []) if isinstance(c, dict) and c.get("id") and c.get("page", True)})
    wants = []
    for ref in refs:
        if (ref in held and not a.full) or ref[:3] in sell_sets:
            continue
        r = call(f"{GAME}/api/me/value?card={ref}", key=key)
        v = r.get("your_value") if isinstance(r, dict) else None
        if isinstance(v, (int, float)) and v >= a.floor:
            wants.append({"card": ref, "max": max(1, int(math.floor(v * (1 - a.margin)))), "qty": 1})
    cash = me.get("cash")
    wants = sorted(wants, key=lambda w: -w["max"])[: a.max_wants]
    if len(haves) + len(wants) > 60:                       # the server takes at most 60 items in all
        haves = sorted(haves, key=lambda h: -h["min"])[: max(10, 60 - min(len(wants), 40))]
        wants = wants[: 60 - len(haves)]
    if a.dry_run:
        print(json.dumps({"haves": haves, "wants": wants, "cash": cash}, indent=1))
        return
    r = call(f"{cfg['server']}/api/clearing/book", {"token": cfg["token"], "haves": haves, "wants": wants})
    if r.get("_status") or "error" in r:
        sys.exit(f"book REFUSED by the server, nothing listed: {r}")
    cfg["book"] = {"haves": haves, "wants": wants, "team": me.get("id"), "sent": datetime.now().isoformat(timespec="seconds")}
    cfg["bought"] = {}
    cfg["approved"] = None
    save_cfg(cfg)
    print(f"book sent: {r}  (cash {cash}); a copy is kept locally and every plan action is checked against it")


def cmd_plan(a) -> None:
    cfg = load_cfg(a)
    plan = call(f"{cfg['server']}/api/clearing/plan?token={cfg['token']}")
    print(json.dumps(plan, indent=1))
    if plan.get("round_status") == "proposed":
        print("\nThis is a PROPOSAL. Review it, then:  vote --ok   or   vote --no --why \"...\" [--trades r1-3,r1-5]",
              file=sys.stderr)


def my_commitment(plan: dict) -> str:
    """Computed HERE from the plan I read (never copied blindly from the server): SHA-256 of team, round, version
    and my actions of the open proposal, same canonical form as the server."""
    team, rid, ver = plan.get("team"), plan.get("round"), plan.get("version")
    mine = [x for x in (plan.get("actions") or []) if x.get("round_status") == plan.get("round_status")]
    rows = sorted((x["id"], x["role"], x["card"], x.get("asset"), x["price"], x["venue"], x.get("to") or x.get("from"))
                  for x in mine)
    return hashlib.sha256(json.dumps([team, rid, ver, rows], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approved_set(plan: dict) -> dict:
    """What I am approving, keyed by action id, with the fields that matter; persisted so execute only ever acts
    on actions I signed, whatever the server says later."""
    mine = [x for x in (plan.get("actions") or []) if x.get("round_status") == plan.get("round_status")]
    return {x["id"]: [x["role"], x["card"], x.get("asset"), x["price"], x["venue"], x.get("to") or x.get("from")] for x in mine}


def record_approval(cfg: dict, plan: dict) -> None:
    cfg["approved"] = {"round": plan.get("round"), "version": plan.get("version"), "commitment": my_commitment(plan),
                       "actions": approved_set(plan), "at": datetime.now().isoformat(timespec="seconds")}
    save_cfg(cfg)


def i_approved(cfg: dict, x: dict) -> bool:
    ap = (cfg.get("approved") or {}).get("actions") or {}
    return ap.get(x.get("id")) == [x["role"], x["card"], x.get("asset"), x["price"], x["venue"], x.get("to") or x.get("from")]


def cmd_vote(a) -> None:
    cfg = load_cfg(a)
    if a.ok == a.no:
        sys.exit("say --ok or --no")
    plan = call(f"{cfg['server']}/api/clearing/plan?token={cfg['token']}")
    body = {"token": cfg["token"], "ok": bool(a.ok), "version": plan.get("version")}
    if a.ok:
        body["commitment"] = my_commitment(plan)
        if body["commitment"] != plan.get("commitment"):
            sys.exit("the server's commitment hash does not match what I computed from my own plan: NOT voting")
        record_approval(cfg, plan)
    if a.no:
        body["why"] = a.why or "no reason given"
        ids = [x.strip() for x in a.trades.split(",") if x.strip()] if a.trades else []
        if ids:
            body["trades"] = ids
        ap = cfg.get("approved") or {}
        if ids and ap.get("actions"):
            for i in ids:
                ap["actions"].pop(i, None)
            cfg["approved"] = ap
        else:
            cfg["approved"] = None                 # a NOT OK revokes everything I had signed in this proposal
        save_cfg(cfg)                              # persisted BEFORE the request, whatever the server answers
    print(json.dumps(call(f"{cfg['server']}/api/clearing/vote", body), indent=1))


def check_sell(x: dict, book: dict, team: str) -> str | None:
    """The server's ready-made offer body is only posted if it is exactly one of OUR listed assets, for cash only,
    at or above OUR min, addressed to the buyer the plan names. Anything else is refused and reported."""
    body = x.get("post") or {}
    mine = {h["asset"]: h for h in book.get("haves", [])}
    give, want = body.get("give") or {}, body.get("want") or {}
    assets = give.get("assets") or []
    if set(body) - {"venue", "to", "give", "want", "expires_in_ticks"}:
        return "unexpected keys in the offer body"
    if len(assets) != 1 or assets[0] not in mine:
        return "offer gives something that is not one of my listed spares"
    if mine[assets[0]]["card"] != x.get("card") or x.get("asset") != assets[0]:
        return "card/asset mismatch"
    if set(give) - {"assets"} or give.get("cash"):
        return "offer gives cash too"
    if set(want) != {"cash"} or not isinstance(want["cash"], int) or want["cash"] != x.get("price"):
        return "offer wants something other than plain cash at the plan price"
    if want["cash"] < mine[assets[0]]["min"]:
        return f"price {want['cash']} is below my min {mine[assets[0]]['min']}"
    if body.get("to") != x.get("to") or not isinstance(body.get("to"), str) or body["to"] == team:
        return "offer is not addressed to the buyer the plan names"
    if not isinstance(body.get("venue"), str):
        return "no venue"
    return None


def venue_fee(venue: str, price: int) -> int | None:
    """The fee the ACCEPTER pays on that venue, read from the game's own /api/venues NOW (no cache: a fee can
    change under us). A pending fee change effective by the next tick (when an accept settles) counts, taking
    the higher of the two, as agent/market_desk.py does."""
    v = call(f"{GAME}/api/venues")
    row = next((x for x in (v.get("venues") or []) if isinstance(x, dict) and x.get("venue") == venue), None)
    if row is None or row.get("status", "open") != "open":
        return None
    try:
        tick = int((call(f"{GAME}/api/clock") or {}).get("tick") or 0)
    except Exception:
        tick = 0
    bps, per = int(row.get("fee_bps") or 0), int(row.get("fee_per_card") or 0)
    pend = row.get("pending_fee")
    if isinstance(pend, dict):          # any announced change counts, whenever it lands: the conservative debit
        bps, per = max(bps, int(pend.get("fee_bps") or 0)), max(per, int(pend.get("fee_per_card") or 0))
    return int(math.ceil(price * bps / 10000)) + per


def offer_from_feed(offer_id: int) -> tuple:
    """The offer as the GAME published it: the public feed's offer.listed event (addressed offers never show on a
    venue's public book, but the feed carries them with maker, to, give and want). Returns (offer, closed) where
    closed is True if a later feed event for that id says it is no longer open. (None, False) if not in the last
    500 events yet."""
    feed = call(f"{GAME}/api/feed?limit=500")
    events = feed.get("events") or [] if isinstance(feed, dict) else []
    listed, closed = None, False
    for e in sorted(events, key=lambda e: e.get("id") or 0):
        if not isinstance(e, dict):
            continue
        o = (e.get("payload") or {}).get("offer") if isinstance(e.get("payload"), dict) else None
        if not isinstance(o, dict) or o.get("id") != offer_id:
            continue
        if e.get("type") == "offer.listed":
            listed = o
        elif str(e.get("type", "")).startswith("offer.") or o.get("status", "open") != "open":
            closed = True
    return listed, closed


def check_buy(x: dict, book: dict, team: str, key: str, bought: dict, reserve: int) -> str | None:
    """Before accepting, read the offer from the game's PUBLIC book of that venue: it must be addressed to us, give
    exactly one card of the plan's ref, want only cash equal to the plan price. The fee comes from /api/venues (not
    from the plan), price + fee must be at or below OUR max for that card AND at or below the card's CURRENT value
    to us (/api/me/value, re-read now: a second copy is worth less), we must not exceed the qty we asked for, and
    cash after the debit must stay above the reserve. Nothing else is accepted."""
    wants = [w for w in book.get("wants", []) if w["card"] == x.get("card")]
    if not wants:
        return "I never asked for this card"
    my_max = max(w["max"] for w in wants)
    qty = sum(int(w.get("qty", 1)) for w in wants)
    if bought.get(x.get("card"), 0) >= qty:
        return f"already bought {qty} of {x.get('card')} this session: refused"
    price = int(x.get("price") or 0)
    cur = call(f"{GAME}/api/me/value?card={x.get('card')}", key=key)
    val = cur.get("your_value") if isinstance(cur, dict) else None
    me = call(f"{GAME}/api/me", key=key)
    cash = me.get("cash")
    offer, closed = offer_from_feed(int(x.get("offer") or 0))
    if offer is None:
        return "offer not visible in the public feed (yet)"
    if closed or offer.get("status", "open") != "open":
        return "offer is no longer open"
    if offer.get("maker") != x.get("from"):
        return f"offer maker {offer.get('maker')} is not the seller the plan names ({x.get('from')})"
    if offer.get("to") != team:
        return "offer is not addressed to me"
    if offer.get("venue") != x.get("venue"):
        return "offer is on another venue"
    try:
        tick = int((call(f"{GAME}/api/clock") or {}).get("tick") or 0)
        if int(offer.get("expires_tick") or 0) <= tick:
            return "offer has expired"
    except Exception:
        pass
    give, want = offer.get("give") or {}, offer.get("want") or {}
    assets = give.get("assets") or []
    refs = [a.get("ref") if isinstance(a, dict) else None for a in assets]
    if len(assets) != 1 or refs[0] != x.get("card") or give.get("cash") or give.get("types"):
        return "offer does not give exactly one copy of the plan's card"
    if want.get("assets") or want.get("types") or not isinstance(want.get("cash"), int):
        return "offer wants a card of mine or something other than cash: refused"
    if want["cash"] != price:
        return f"offer price {want['cash']} differs from the plan price {price}"
    fee = venue_fee(x.get("venue"), price)            # LAST, right before the accept: the freshest fee wins
    if fee is None:
        return "venue unknown to the game (yet)"
    debit = price + fee
    if debit > my_max:
        return f"price {price} + real fee {fee} = {debit} is above my max {my_max}"
    if not isinstance(val, (int, float)) or debit > val:
        return f"debit {debit} is not below the card's current value to me ({val}): refused"
    if not isinstance(cash, int) or cash - debit < reserve:
        return f"cash {cash} minus {debit} would go under my reserve {reserve}: refused"
    return None


def cmd_execute(a) -> None:
    cfg, key = load_cfg(a), key_or_die()
    book, team = cfg.get("book") or {}, cfg.get("team")
    if not book:
        sys.exit("no local copy of my book: run `book` first (execute only acts on what I listed myself)")
    posted, accepted, failed, voted = set(), set(), set(), set()
    bought = dict(cfg.get("bought") or {})          # survives restarts: kept in the local config file
    until = a.until
    while True:
        if until and datetime.now().strftime("%H:%M") >= until:
            print("until reached, stopping")
            return
        plan = call(f"{cfg['server']}/api/clearing/plan?token={cfg['token']}")
        if plan.get("round_status") == "proposed":
            key_v = (plan.get("round"), plan.get("version"))
            mine = [x for x in (plan.get("actions") or []) if x.get("round_status") == "proposed"]
            if mine and plan.get("your_vote") is None:
                if a.auto_approve and key_v not in voted and a.dry_run:
                    voted.add(key_v)
                    print(f"{datetime.now():%H:%M:%S} proposal r{key_v[0]} v{key_v[1]}: dry run, NOT voting")
                elif a.auto_approve and key_v not in voted:
                    voted.add(key_v)
                    if my_commitment(plan) != plan.get("commitment"):
                        print(f"{datetime.now():%H:%M:%S} commitment hash mismatch on r{key_v[0]} v{key_v[1]}: NOT voting")
                        continue
                    record_approval(cfg, plan)
                    r = call(f"{cfg['server']}/api/clearing/vote", {"token": cfg["token"], "ok": True, "version": key_v[1],
                                                                     "commitment": my_commitment(plan)})
                    print(f"{datetime.now():%H:%M:%S} proposal r{key_v[0]} v{key_v[1]}: auto-approved -> {r.get('status')}, waiting for {r.get('waiting_for')}")
                elif key_v not in voted:
                    voted.add(key_v)
                    print(f"{datetime.now():%H:%M:%S} proposal r{key_v[0]} v{key_v[1]} needs your vote: run `plan`, then `vote --ok` or `vote --no --why ...`")
                    for x in mine:
                        print(f"   {x['id']} {x['role'].upper()} {x['card']} {x['price']} P on {x['venue']} with {x.get('to') or x.get('from')}")
        cfg = load_cfg(a)                       # re-read: a manual `vote --ok` in another shell records approval here
        acts = []
        for x in (plan.get("actions") or []):
            if x.get("round_status") != "approved":
                continue
            if not i_approved(cfg, x):
                if x["id"] not in failed:
                    print(f"{datetime.now():%H:%M:%S} IGNORED {x['id']}: the server says approved but I never signed this exact action")
                    failed.add(x["id"])
                continue
            acts.append(x)
        pending = [x for x in acts if x["id"] not in accepted | failed]
        if not pending and plan.get("round"):
            print(f"{datetime.now():%H:%M:%S} nothing pending in round {plan['round']}; waiting for the next run")
        did_accept = False
        for x in pending:
            if x["role"] == "sell" and x["id"] not in posted and x.get("offer") is None:
                body = x["post"]
                bad = check_sell(x, book, team)
                if bad:
                    print(f"{datetime.now():%H:%M:%S} REFUSED sell {x['id']}: {bad}")
                    failed.add(x["id"])
                    call(f"{cfg['server']}/api/clearing/report", {"token": cfg["token"], "action": x["id"], "status": "failed", "error": "client refused: " + bad})
                    continue
                print(f"{datetime.now():%H:%M:%S} SELL {x['card']} #{x['asset']} at {x['price']} on {x['venue']} to {x['to']}  (checked against my book)")
                if a.dry_run:
                    posted.add(x["id"])
                    continue
                r = call(f"{GAME}/api/offers", body, key=key)
                oid = r.get("id") or (r.get("offer") or {}).get("id")
                if oid:
                    posted.add(x["id"])
                    call(f"{cfg['server']}/api/clearing/report", {"token": cfg["token"], "action": x["id"], "offer": int(oid)})
                    print(f"   posted offer {oid}")
                else:
                    print(f"   refused: {r}")
                    if r.get("_status") in (400, 404, 409):
                        failed.add(x["id"])
                        call(f"{cfg['server']}/api/clearing/report",
                             {"token": cfg["token"], "action": x["id"], "status": "failed", "error": str(r.get("error"))})
            elif x["role"] == "sell" and x.get("offer"):
                posted.add(x["id"])
            elif x["role"] == "buy" and x.get("offer") and not did_accept:
                bad = check_buy(x, book, team, key, bought, a.reserve)
                if bad:
                    if "(yet)" in bad:
                        continue                      # the book lags a tick; try again next loop
                    print(f"{datetime.now():%H:%M:%S} REFUSED buy {x['id']}: {bad}")
                    failed.add(x["id"])
                    call(f"{cfg['server']}/api/clearing/report", {"token": cfg["token"], "action": x["id"], "status": "failed", "error": "client refused: " + bad})
                    continue
                print(f"{datetime.now():%H:%M:%S} BUY  {x['card']} at {x['price']} (+{x.get('fee', 0)} fee) on {x['venue']}: accept offer {x['offer']}  (public offer checked: to me, one {x['card']}, cash only)")
                if a.dry_run:
                    accepted.add(x["id"])
                    continue
                r = call(f"{GAME}/api/offers/{int(x['offer'])}/accept", {}, key=key)
                if r.get("_status") is None:
                    accepted.add(x["id"])
                    bought[x["card"]] = bought.get(x["card"], 0) + 1
                    cfg["bought"] = bought
                    save_cfg(cfg)
                    did_accept = True
                    call(f"{cfg['server']}/api/clearing/report", {"token": cfg["token"], "action": x["id"], "status": "accepted"})
                    print(f"   accepted: {r}")
                else:
                    print(f"   refused: {r}")
                    if r.get("_status") in (400, 404) and r.get("error") not in ("rate_limited", "tick_limit"):
                        failed.add(x["id"])
                        call(f"{cfg['server']}/api/clearing/report",
                             {"token": cfg["token"], "action": x["id"], "status": "failed", "error": str(r.get("error"))})
        if a.once:
            return
        time.sleep(a.every)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--server", default=os.environ.get("CLEARING_URL", ""), help="clearing house base URL")
    p.add_argument("--team", default="", help="your team id (t07); read from /api/me if omitted")
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("join")
    j.add_argument("--venue", default="", help="your venue id, so trades can be routed onto it")
    j.add_argument("--invite", default="", help="the invite code Team 3 gave your team")
    j.set_defaults(fn=cmd_join)
    b = sub.add_parser("book")
    b.add_argument("--margin", type=float, default=0.15, help="keep this share of your value on each side")
    b.add_argument("--sell-sets", default="", help="sets to sell entirely, e.g. MAL,CHA")
    b.add_argument("--keep", default="", help="cards never to sell, e.g. LAV-09,LAV-10")
    b.add_argument("--floor", type=float, default=5.0, help="want only cards worth at least this to you")
    b.add_argument("--max-wants", type=int, default=60)
    b.add_argument("--no-full", dest="full", action="store_false",
                   help="only duplicates for sale and missing cards wanted (default: full book, see docstring)")
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(fn=cmd_book)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)
    v = sub.add_parser("vote", help="approve or reject the open proposal")
    v.add_argument("--ok", action="store_true")
    v.add_argument("--no", action="store_true")
    v.add_argument("--why", default="")
    v.add_argument("--trades", default="", help="comma-separated action ids to reject (default: all yours)")
    v.set_defaults(fn=cmd_vote)
    e = sub.add_parser("execute")
    e.add_argument("--until", default="", help="HH:MM wall clock stop")
    e.add_argument("--every", type=float, default=15.0)
    e.add_argument("--once", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--auto-approve", action="store_true", help="vote OK on every proposal (prices are inside your numbers anyway)")
    e.add_argument("--reserve", type=int, default=40, help="cash never to go under when accepting a buy")
    e.set_defaults(fn=cmd_execute)
    for sp in (j, b, e, v, sub.choices["plan"]):
        sp.add_argument("--server", dest="server_sub", default="", help=argparse.SUPPRESS)
        sp.add_argument("--team", dest="team_sub", default="", help=argparse.SUPPRESS)
    a = p.parse_args()
    a.server = getattr(a, "server_sub", "") or a.server or ""      # an explicit flag wins over CLEARING_URL
    a.team = getattr(a, "team_sub", "") or a.team or ""
    if a.cmd == "join" and not a.server:
        sys.exit("--server is required")
    a.fn(a)


if __name__ == "__main__":
    main()
