"""Tests offline de celestina: libros sintéticos, sin red."""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import celestina as ce


def card(aid, ref, kind="card"):
    return {"id": aid, "kind": kind, "ref": ref}


def ask(oid, ref, price, maker="t04", venue="rastro", aid=None, **kw):
    return {"id": oid, "maker": maker, "venue": venue, "to": kw.get("to"), "expires_tick": kw.get("exp", 999),
            "give": {"cash": 0, "assets": [card(aid or oid * 10, ref)], "types": []},
            "want": {"cash": price, "assets": [], "types": []}}


def bid(oid, ref, price, maker="t06", venue="rastro", **kw):
    return {"id": oid, "maker": maker, "venue": venue, "to": kw.get("to"), "expires_tick": kw.get("exp", 999),
            "give": {"cash": price, "assets": [], "types": []},
            "want": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}}


def swap(oid, gives, wants, maker, venue="rastro"):
    return {"id": oid, "maker": maker, "venue": venue, "to": None, "expires_tick": 999,
            "give": {"cash": 0, "assets": [card(oid * 10, gives)], "types": []},
            "want": {"cash": 0, "assets": [], "types": [f"card:{wants}"]}}


def pairs_of(raw, team="t15", own_ids=(), tick=100, zero=("v02", "v07"), **kw):
    offers = ce.eligible([ce.norm(o) for o in raw], team=team, own_ids=own_ids, tick=tick)
    return ce.find_pairs(offers, our_venue="v15", zero_fee=zero, **kw)


class Detection(unittest.TestCase):
    def test_cross_pair_at_midpoint(self):
        ps = pairs_of([ask(1, "RET-06", 26), bid(2, "RET-06", 30)])
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertEqual((p["kind"], p["ref"], p["sell"]["price"], p["buy"]["price"], p["mid"]), ("cross", "RET-06", 26, 30, 28))
        self.assertEqual((p["sell"]["team"], p["buy"]["team"]), ("t04", "t06"))

    def test_near_pair_within_rastro_fee_only(self):
        ps = pairs_of([ask(1, "MAL-05", 4), bid(2, "MAL-05", 2)])
        self.assertEqual([p["kind"] for p in ps], ["near"])
        self.assertEqual(ps[0]["gap"], 2)
        # hueco 5 > comisión del Rastro sobre 10 (2 P): no es pareja
        self.assertEqual(pairs_of([ask(1, "MAL-05", 10), bid(2, "MAL-05", 5)]), [])
        # salvo que se amplíe a propósito
        self.assertEqual(len(pairs_of([ask(1, "MAL-05", 10), bid(2, "MAL-05", 5)], near_extra=3)), 1)

    def test_rastro_fee(self):
        self.assertEqual([ce.rastro_fee(p) for p in (1, 20, 21, 40)], [2, 2, 3, 3])

    def test_excludes_our_offers_by_team_id_and_pseudonym(self):
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, maker="t15"), bid(2, "RET-06", 30)]), [])
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, maker="m1"), bid(2, "RET-06", 30, maker="m2")], own_ids=[1]), [])
        # el mismo seudónimo en el mismo venue también es nuestro
        raw = [ask(1, "RET-06", 26, maker="m1"), ask(3, "RET-06", 27, maker="m1"), bid(2, "RET-06", 30, maker="m2")]
        self.assertEqual(pairs_of(raw, own_ids=[1]), [])

    def test_excludes_directed_expiring_and_same_party(self):
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, to="t06"), bid(2, "RET-06", 30)]), [])
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, exp=101), bid(2, "RET-06", 30)], tick=100), [])
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, maker="t06"), bid(2, "RET-06", 30, maker="t06")]), [])

    def test_same_zero_fee_venue_is_already_home(self):
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, venue="v02"), bid(2, "RET-06", 30, venue="v02")]), [])
        self.assertEqual(pairs_of([ask(1, "RET-06", 26, venue="v15"), bid(2, "RET-06", 30, venue="v15")]), [])
        self.assertEqual(len(pairs_of([ask(1, "RET-06", 26, venue="v02"), bid(2, "RET-06", 30, venue="v07")])), 1)
        self.assertEqual(len(pairs_of([ask(1, "RET-06", 26), bid(2, "RET-06", 30)])), 1)  # los dos en el Rastro

    def test_each_offer_used_once_best_first(self):
        raw = [ask(1, "LAT-03", 5), ask(3, "LAT-03", 6, maker="t07"), bid(2, "LAT-03", 9), bid(4, "LAT-03", 7, maker="t09")]
        ps = pairs_of(raw)
        self.assertEqual([(p["sell"]["offer"], p["buy"]["offer"]) for p in ps], [(1, 2), (3, 4)])
        ids = [x for p in ps for x in (p["sell"]["offer"], p["buy"]["offer"])]
        self.assertEqual(len(ids), len(set(ids)))

    def test_different_items_never_pair(self):
        self.assertEqual(pairs_of([ask(1, "RET-06", 26), bid(2, "RET-07", 30)]), [])
        # una puja por un sobre no casa con la venta de una carta del mismo ref
        pk = bid(2, "X", 30); pk["want"]["types"] = ["pack:RET-06"]
        self.assertEqual(pairs_of([ask(1, "RET-06", 26), pk]), [])

    def test_complementary_swap(self):
        ps = pairs_of([swap(1, "LAT-04", "MAL-12", "t06"), swap(2, "MAL-12", "LAT-04", "t13")])
        self.assertEqual([p["kind"] for p in ps], ["swap"])
        self.assertEqual(ps[0]["a"]["gives"], ["card:LAT-04"])
        self.assertEqual(pairs_of([swap(1, "LAT-04", "MAL-12", "t06"), swap(2, "MAL-12", "LAT-05", "t13")]), [])
        self.assertEqual(pairs_of([swap(1, "LAT-04", "MAL-12", "t06"), swap(2, "MAL-12", "LAT-04", "t06")]), [])

    def test_swap_with_cash_requirement(self):
        a, b = swap(1, "LAT-04", "MAL-12", "t06"), swap(2, "MAL-12", "LAT-04", "t13")
        b["want"]["cash"] = 3
        self.assertEqual(pairs_of([a, b]), [])  # t06 no pone dinero
        a["give"]["cash"] = 3
        self.assertEqual(len(pairs_of([a, b])), 1)

    def test_rank_cross_then_swap_then_near(self):
        raw = [ask(1, "A-01", 5), bid(2, "A-01", 4), ask(3, "B-01", 10), bid(4, "B-01", 12),
               swap(5, "C-01", "D-01", "t07"), swap(6, "D-01", "C-01", "t08")]
        self.assertEqual([p["kind"] for p in pairs_of(raw)], ["cross", "swap", "near"])

    def test_unpaired_leads_are_live_orders_not_invented_pairs(self):
        raw = [bid(1, "RET-06", 30, venue="rastro"), bid(2, "RET-06", 25, venue="v15"),
               ask(3, "LAT-04", 12), ask(4, "LAT-04", 9), ask(5, "RET-06", 28)]
        offers = [ce.norm(o) for o in raw]
        leads = ce.find_leads(offers, paired_ids={1, 5})
        self.assertEqual([(x["side"], x["offer"]) for x in leads], [("bid", 2), ("ask", 4)])
        text, used = ce.compose_leads(leads, venue="v15", fee_bps=0, fee_per_card=0)
        self.assertEqual(len(used), 2)
        self.assertIn("no matching counterparty confirmed", text)
        self.assertIn("A counterparty can accept this order", text)
        self.assertIn("Maker must repost on v15", text)
        self.assertLessEqual(len(text), ce.MAX_CHARS)

    def test_external_lead_needs_time_to_repost(self):
        offers = [ce.norm(bid(1, "RET-06", 30, exp=108)),
                  ce.norm(bid(2, "LAT-03", 9, venue="v15", exp=102))]
        leads = ce.find_leads(offers, tick=100)
        self.assertEqual([x["offer"] for x in leads], [2])


class Announcement(unittest.TestCase):
    def test_text_uses_only_book_numbers_and_fits(self):
        raw = []
        for i in range(30):
            raw += [ask(100 + i, f"RET-{i:02d}", 20 + i, maker=f"t{i % 9 + 1:02d}"),
                    bid(200 + i, f"RET-{i:02d}", 21 + i, maker=f"t{(i + 3) % 9 + 10:02d}")]
        ps = pairs_of(raw)
        text, used = ce.compose(ps, venue="v15", max_pairs=30)
        self.assertLessEqual(len(text), ce.MAX_CHARS)
        self.assertTrue(used)
        allowed = {0, 5, 1, 15}  # 0 %, 0 P, 5 % + 1 P, v15
        for p in used:
            allowed |= {p["sell"]["price"], p["buy"]["price"], p["mid"], p["gap"], p["rastro_fee"]}
            self.assertIn(p["ref"], text)
        nums = {int(n) for n in re.findall(r"(?<![A-Z]-)(?<![\w#])\d+", text)}
        self.assertLessEqual(nums, allowed | {int(p["ref"][-2:]) for p in used})
        self.assertNotIn("t15", text)

    def test_unknown_team_named_by_offer_id(self):
        ps = pairs_of([ask(1, "RET-06", 26, maker="m9"), bid(2, "RET-06", 30, maker="m8")])
        text, _ = ce.compose(ps)
        self.assertIn("offer #1", text)
        self.assertNotIn("m9", text)

    def test_fee_is_stated_as_configured(self):
        ps = pairs_of([ask(1, "RET-06", 26), bid(2, "RET-06", 30)])
        self.assertIn("0 % and 0 P per card", ce.compose(ps)[0])
        self.assertIn("1.5 % and 2 P per card", ce.compose(ps, fee_bps=150, fee_per_card=2)[0])

    def test_empty_without_pairs(self):
        self.assertEqual(ce.compose([]), ("", []))

    def test_style_lines(self):
        p = pairs_of([ask(1, "RET-06", 29), bid(2, "RET-06", 26, venue="v02")])[0]
        line = ce.pair_line(p, "v15")
        self.assertEqual(line, "RET-06: t06 bids 26 (v02), t04 asks 29 (El Rastro), 3 P apart; "
                               "El Rastro's fee at 28 is 3 P. Meet at 28 on v15.")


class Pacing(unittest.TestCase):
    def test_every_and_repeat(self):
        st = {"last_tick": None, "pairs": {}}
        self.assertTrue(ce.may_announce(st, 100, 10))
        p = {"key": "cross:card:A:t1>t2"}
        st = ce.record(st, [p], 100, 60)
        self.assertFalse(ce.may_announce(st, 109, 10))
        self.assertTrue(ce.may_announce(st, 110, 10))
        self.assertEqual(ce.fresh([p], st, 150, 60), [])
        self.assertEqual(ce.fresh([p], st, 160, 60), [p])

    def test_state_roundtrip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "s.json")
            self.assertEqual(ce.load_state(path), {"last_tick": None, "pairs": {}})
            ce.save_state(path, {"last_tick": 5, "pairs": {"k": 5}})
            self.assertEqual(ce.load_state(path), {"last_tick": 5, "pairs": {"k": 5}})


class FakePublic:
    """Solo métodos GET: si celestina llamara a otra cosa, el test fallaría con AttributeError."""

    def __init__(self, ticks, boards, venues=None):
        self.ticks, self.boards, self.calls = list(ticks), boards, []
        self._venues = venues or [{"venue": "rastro", "fee_bps": 500, "fee_per_card": 1, "status": "open"},
                                  {"venue": "v15", "fee_bps": 0, "fee_per_card": 0, "status": "open"}]

    def clock(self):
        self.calls.append("clock")
        return {"tick": self.ticks.pop(0) if len(self.ticks) > 1 else self.ticks[0]}

    def venues(self):
        self.calls.append("venues")
        return {"venues": self._venues}

    def feed(self, limit=150):
        self.calls.append("feed")
        return {"events": [{"type": "offer.listed", "payload": {"offer": {"id": 1, "maker": "t04"}}}]}

    def board(self, venue):
        self.calls.append(f"board:{venue}")
        return {"offers": self.boards.get(venue, [])}


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        env = {k: v for k, v in os.environ.items() if k not in ("BAZAAR_KEY", "BROKER_KEY", "STARTER_BROKER_KEY")}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def boards(self):
        return {"rastro": [ask(1, "RET-06", 26, maker="m1"), bid(2, "RET-06", 30, maker="m2")]}

    def test_snapshot_reads_only_and_maps_makers_from_feed(self):
        pub = FakePublic([100], self.boards())
        snap = ce.snapshot(pub, lambda f, *a: f(*a), our_venue="v15", scan=None)
        self.assertEqual(pub.calls, ["clock", "venues", "feed", "board:rastro", "board:v15"])
        self.assertEqual({o["id"]: o["team"] for o in snap["offers"]}, {1: "t04", 2: None})
        self.assertEqual(snap["zero_fee"], {"v15"})

    def test_dry_run_never_announces(self):
        pub = FakePublic([100], self.boards())
        with mock.patch.object(ce, "public_client", return_value=pub), \
                mock.patch("bazaar_sdk.Broker") as broker, mock.patch("builtins.print") as out:
            self.assertEqual(ce.main(["--state", self.state, "--min-interval", "0", "--makers-db", ""]), 0)
        broker.assert_not_called()
        self.assertFalse(os.path.exists(self.state))
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("DRY RUN", printed)
        self.assertIn("RET-06: offer #2 bids 30 (El Rastro), t04 asks 26 (El Rastro). Meet at 28 on v15.", printed)

    def test_execute_needs_broker_key(self):
        with mock.patch("builtins.print"):
            self.assertEqual(ce.main(["--execute", "--state", self.state]), 2)

    def test_execute_announces_once_then_waits(self):
        os.environ["BROKER_KEY"] = "bk_test"
        fake = mock.MagicMock()
        for tick in (100, 105):
            pub = FakePublic([tick], self.boards())
            with mock.patch.object(ce, "public_client", return_value=pub), \
                    mock.patch("bazaar_sdk.Broker", return_value=fake), mock.patch("builtins.print"):
                ce.main(["--execute", "--state", self.state, "--min-interval", "0", "--makers-db", ""])
        self.assertEqual(fake.announce.call_count, 1)  # el segundo, 5 ticks después, se retiene (--every 10)
        self.assertEqual(ce.load_state(self.state)["last_tick"], 100)
        self.assertEqual([c[0] for c in fake.method_calls], ["announce"])  # nada de match ni otras escrituras

    def test_missing_or_feeful_venue(self):
        args = ce.parse(["--state", self.state])
        snap = {"tick": 1, "offers": [ce.norm(o) for o in self.boards()["rastro"]], "own_ids": [], "team": None,
                "zero_fee": set(), "ours": None}
        self.assertEqual(ce.cycle(snap, args, {"pairs": {}})["text"], "")
        snap["ours"] = {"venue": "v15", "fee_bps": 200, "fee_per_card": 0, "status": "open"}
        self.assertIn("2 % and 0 P per card", ce.cycle(snap, args, {"pairs": {}})["text"])

    def test_leads_mode_finds_a_single_sided_order_without_claiming_a_match(self):
        args = ce.parse(["--leads", "--state", self.state])
        snap = {"tick": 100, "offers": [ce.norm(bid(2, "RET-06", 30, venue="v15"))],
                "own_ids": [], "team": "t15", "zero_fee": {"v15"},
                "ours": {"venue": "v15", "fee_bps": 0, "fee_per_card": 0, "status": "open"}}
        res = ce.cycle(snap, args, {"pairs": {}})
        self.assertEqual(res["pairs"], [])
        self.assertEqual(res["leads"][0]["offer"], 2)
        self.assertIn("no matching counterparty confirmed", res["text"])


class Calibration(unittest.TestCase):
    def make_db(self, path):
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, tick INTEGER, type TEXT, actor TEXT, payload TEXT)")
        db.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY, maker TEXT)")
        db.execute("CREATE TABLE json_snapshots (snap_ts REAL, tick INTEGER, kind TEXT, payload TEXT)")
        db.execute("CREATE TABLE board_snapshots (snap_ts REAL, tick INTEGER, venue TEXT, offer_id INTEGER, maker TEXT,"
                   " too TEXT, give_cash INTEGER, give_assets TEXT, give_types TEXT, want_cash INTEGER, want_types TEXT,"
                   " created_tick INTEGER, expires_tick INTEGER)")
        n = iter(range(1, 100))

        def ev(tick, typ, payload):
            db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (next(n), tick, typ, None, json.dumps(payload)))

        def listed(tick, o):
            ev(tick, "offer.listed", {"venue": o["venue"], "offer": {**o, "thread": None}})
            db.execute("INSERT INTO offers VALUES (?,?)", (o["id"], o["maker"]))
        listed(10, ask(1, "RET-06", 26))
        listed(10, bid(2, "RET-06", 30))
        listed(10, ask(3, "LAT-03", 5, maker="t07"))
        listed(10, bid(4, "LAT-03", 4, venue="v07"))
        listed(10, ask(5, "MAL-01", 9, maker="t15"))  # nuestra: nunca cuenta
        listed(10, bid(6, "MAL-01", 12))
        ev(12, "offer.cancelled", {"offer": 3, "venue": "rastro"})  # LAT-03 vive 2 ticks
        ev(14, "settlement", {"venue": "rastro", "items": [{"id": 10, "kind": "card", "ref": "RET-06", "to": "t09"}]})
        ev(20, "clock", {})
        db.execute("INSERT INTO json_snapshots VALUES (0, 0, 'venues', ?)",
                   (json.dumps([{"venue": "v07", "fee_bps": 0, "fee_per_card": 0}]),))
        db.execute("INSERT INTO board_snapshots VALUES (1000, 11, 'rastro', 1, 'm1', '', 0, ?, '[]', 26, '[]', 10, 999)",
                   (json.dumps([card(10, "RET-06")]),))
        db.execute("INSERT INTO board_snapshots VALUES (1000, 11, 'rastro', 2, 'm2', '', 30, '[]', '[]', 0, ?, 10, 999)",
                   (json.dumps(["card:RET-06"]),))
        db.commit()
        db.close()

    def test_replay_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.db")
            self.make_db(path)
            books = ce.replay_books(ce._db(path), 10, 20)
            self.assertEqual(sorted(o["id"] for o in books[11]), [1, 2, 3, 4, 5, 6])
            self.assertEqual(sorted(o["id"] for o in books[13]), [1, 2, 4, 5, 6])
            self.assertEqual(sorted(o["id"] for o in books[15]), [2, 4, 5, 6])  # la carta de 1 cambió de manos
            args = ce.parse(["--calibrate", "--db", path, "--hours", "0.1", "--tick-seconds", "30"])
            res = ce.calibrate(args)
            self.assertEqual(res["events"]["by_kind"], {"cross": 1, "near": 1})
            self.assertEqual(res["board_snapshots"]["unique_pairs"], 1)
            self.assertTrue(all("t15" not in line for line in res["events"]["top"]))
            self.assertEqual(ce.makers_from_db(path), {1: "t04", 2: "t06", 3: "t07", 4: "t06", 5: "t15", 6: "t06"})
            self.assertEqual(ce.makers_from_db(os.path.join(d, "nope.db")), {})

    def test_known_makers_fill_board_pseudonyms(self):
        pub = FakePublic([100], {"rastro": [bid(2, "RET-06", 30, maker="m2")]})
        snap = ce.snapshot(pub, lambda f, *a: f(*a), our_venue="v15", scan=None, known={2: "t06"})
        self.assertEqual(snap["offers"][0]["team"], "t06")


if __name__ == "__main__":
    unittest.main()
