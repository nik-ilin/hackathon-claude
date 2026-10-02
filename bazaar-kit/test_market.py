"""Casos de seguridad económica y estructura del mercado; sin acceso a red."""
import copy
import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import market_agent as m
from bazaar_sdk import BazaarError


def fixture():
    cards = [{"id": "LAT-01", "book": 10, "page": True},
             {"id": "LAT-02", "book": 10, "page": True}]
    return {"catalog": {"sets": [{"id": "LAT", "name": "La Latina", "released": True, "cards": cards}],
                        "values": {"copy_marginals": [1, .25, .1]}},
            "me": {"id": "t15", "cash": 100, "affinity": {"LAT": 1.3}, "assets": [
                {"id": 1, "kind": "card", "ref": "LAT-01", "your_value": 3.2},
                {"id": 2, "kind": "card", "ref": "LAT-01", "your_value": 3.2}]},
            "clock": {"tick": 10, "limits": {"accepts_per_team_per_tick": 1, "max_open_offers_per_team": 30,
                                               "offers_per_team_per_tick": 12}},
            "venues": {"venues": [{"venue": "rastro", "owner": "world", "status": "open",
                                     "fee_bps": 500, "fee_per_card": 1}]},
            "offers": {"offers": []}, "board": {"offers": []}}


def ask(price=8):
    return {"id": 7, "maker": "masked", "to": None, "venue": "rastro", "thread": None,
            "status": "open", "expires_tick": 20,
            "give": {"cash": 0, "assets": [{"id": 9, "kind": "card", "ref": "LAT-02"}], "types": []},
            "want": {"cash": price, "assets": [], "types": []}}


def bid(price=9):
    a = ask()
    a["give"] = {"cash": price, "assets": [], "types": []}
    a["want"] = {"cash": 0, "assets": [], "types": ["card:LAT-01"]}
    return a


def actions(s, **kwargs):
    return m.plan(s, {"LAT-02": 13}, reserve=10, **kwargs)["actions"]


class MarketTests(unittest.TestCase):
    def test_missing_and_duplicates(self):
        _, pages, duplicates = m.inventory(fixture()["me"], fixture()["catalog"])
        self.assertEqual(pages[0]["missing"], ["LAT-02"])
        self.assertEqual(duplicates, {"LAT-01": [2]})

    def test_buy_includes_fee_and_marginal_value(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        a = next(a for a in actions(s) if a["action"] == "buy")
        self.assertEqual(a["cost_max"], 10)
        self.assertEqual(a["surplus"], 3)

    def test_nominally_profitable_but_fee_makes_it_bad(self):
        s = fixture(); s["board"]["offers"] = [ask(11)]
        self.assertFalse(any(a["action"] == "buy" for a in actions(s)))

    def test_duplicate_sale_preserves_one_and_covers_loss(self):
        s = fixture(); s["board"]["offers"] = [bid()]
        a = next(a for a in actions(s) if a["action"] == "sell")
        self.assertEqual((a["asset"], a["net_cash_min"], a["surplus"]), (2, 7, 3))
        s["me"]["assets"].pop()
        self.assertFalse(any(a["action"] in ("sell", "list") for a in actions(s)))

    def test_locked_duplicate_not_offered_again(self):
        s = fixture(); o = ask(); o["maker"] = "t15"; o["give"]["assets"] = [2]
        s["offers"]["offers"] = [o]
        self.assertFalse(any(a["action"] in ("sell", "list") for a in actions(s)))

    def test_no_duplicate_buy_if_own_bid_exists(self):
        s = fixture(); o = bid(); o["maker"] = "t15"; o["want"]["types"] = ["card:LAT-02"]
        s["offers"]["offers"] = [o]
        self.assertFalse(any(a["action"] in ("buy", "bid") for a in actions(s)))

    def test_persistent_budget_and_reserve(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        self.assertFalse(any(a["action"] in ("buy", "bid") for a in actions(s, budget=20, committed_budget=20)))
        s["me"]["cash"] = 10
        self.assertFalse(any(a["action"] in ("buy", "bid") for a in actions(s)))

    def test_reject_malformed_or_extra_obligations(self):
        for change in [lambda o: o["want"].update(types=["card:LAT-01"]),
                       lambda o: o["want"].update(extra=4),
                       lambda o: o["give"].update(cash=5),
                       lambda o: o.update(to="other"),
                       lambda o: o.update(expires_tick=11),
                       lambda o: o.update(thread=3),
                       lambda o: o["want"].update(cash=True)]:
            o = ask(); change(o)
            self.assertIsNone(m.parse_offer(o, set(), "t15", 10))
        self.assertIsNone(m.parse_offer(ask(), {7}, "t15", 10))

    def test_no_unverified_completion_bonus(self):
        s = fixture(); s["board"]["offers"] = [ask(25)]
        self.assertFalse(any(a["action"] == "buy" for a in actions(s)))

    def test_unminted_cards_and_unrealistic_bids_are_skipped(self):
        s = fixture(); s["catalog"]["sets"][0]["cards"][1]["minted"] = 0
        self.assertFalse(any(a["action"] == "bid" for a in actions(s)))
        s["catalog"]["sets"][0]["cards"][1]["minted"] = 1
        s["catalog"]["sets"][0]["cards"][1]["book"] = 450
        self.assertFalse(any(a["action"] == "bid" for a in actions(s)))

    def test_announced_fee_change_blocks_actions(self):
        s = fixture(); s["venues"]["venues"][0]["pending_fee"] = {"fee_bps": 1000}
        self.assertEqual(actions(s), [])

    def test_alternates_passive_orders_but_prioritizes_executable_deals(self):
        aa = [{"action": "list"}, {"action": "bid"}]
        self.assertEqual(m.choose_action(aa, {"last_action_type": "list"})["action"], "bid")
        aa.append({"action": "buy"})
        self.assertEqual(m.choose_action(aa, {})["action"], "buy")

    def test_acceptance_requires_asset_and_cash(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        a = next(a for a in actions(s) if a["action"] == "buy")
        state = {"pending": {"action": a, "before_ids": [1, 2], "cash_before": 100}}
        self.assertFalse(m.reconcile(state, s))
        s["me"]["assets"].append({"id": 9})
        self.assertFalse(m.reconcile(state, s))
        s["me"]["cash"] = 90
        self.assertTrue(m.reconcile(state, s))
        self.assertIsNone(state["pending"])

    def test_ambiguous_listing_never_repeated(self):
        s = fixture()
        a = next(a for a in actions(s) if a["action"] == "list")
        state = {"pending": {"action": a, "before_ids": [1, 2], "cash_before": 100}}
        self.assertFalse(m.reconcile(state, s))

    def test_execute_revalidates_changed_offer(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        a = next(a for a in actions(s) if a["action"] == "buy")
        changed = copy.deepcopy(s); changed["board"]["offers"] = [ask(40)]
        class Reader:
            def snapshot(self): return changed
            def call(self, *args): return {"your_value": 13}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "cambiado"):
                m.execute_one(Reader(), {}, Path(d)/"state.json", a,
                              Namespace(max_spend=100, reserve=10, margin=2))

    def test_network_error_preserves_pending_and_budget(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        a = next(a for a in actions(s) if a["action"] == "buy")
        class API:
            def accept(self, *args): raise BazaarError("network", status=0)
        class Reader:
            api = API()
            def snapshot(self): return s
            def call(self, *args): return {"your_value": 13}
        state = {}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(BazaarError):
                m.execute_one(Reader(), state, Path(d)/"state.json", a,
                              Namespace(max_spend=100, reserve=10, margin=2))
        self.assertEqual(state["committed"], 10)
        self.assertIsNotNone(state["pending"])

    def test_full_dry_run_makes_no_mutations(self):
        s = fixture(); s["me"]["name"] = "test"; s["board"]["offers"] = [ask()]
        class Reader:
            def snapshot(self): return s
            def values(self, snapshot): return {"LAT-02": 13}
        args = Namespace(execute=False, max_spend=100, reserve=10, margin=2, per_card=40, action="best")
        with tempfile.TemporaryDirectory() as d, patch.object(m, "DATA", Path(d)), contextlib.redirect_stdout(io.StringIO()):
            state_path = Path(d)/"state.json"
            self.assertFalse(m.run_cycle(Reader(), args, state_path))
            self.assertFalse(state_path.exists())
            report = json.loads((Path(d)/"market_report.json").read_text())
            self.assertEqual(len(report["collection"]), 2)
            self.assertEqual(report["collection"][0]["copies"], 2)

    def test_bid_uses_cards_request_not_response_types(self):
        s = fixture()
        a = next(a for a in actions(s) if a["action"] == "bid")
        calls = []
        class API:
            def list_offer(self, give, want, **kw):
                calls.append((give, want, kw)); return {"id": 88}
        class Reader:
            api = API()
            def snapshot(self): return s
            def call(self, *args): return {"your_value": 13}
        state = {}
        with tempfile.TemporaryDirectory() as d:
            m.execute_one(Reader(), state, Path(d)/"state.json", a,
                          Namespace(max_spend=100, reserve=10, margin=2))
        self.assertEqual(calls[0][1], {"cards": ["LAT-02"]})
        self.assertIsNone(state["pending"])
        self.assertNotIn("settlements", state)
        self.assertEqual(state["committed"], a["cost_max"])

    def test_full_execution_then_settlement(self):
        s = fixture(); s["me"]["name"] = "test"; s["board"]["offers"] = [ask()]
        calls = []
        class API:
            def accept(self, offer): calls.append(offer); return {"status": "queued"}
        class Reader:
            api = API()
            def snapshot(self): return s
            def values(self, snapshot): return {"LAT-02": 13}
            def call(self, *args): return {"your_value": 13}
        args = Namespace(execute=True, max_spend=100, reserve=10, margin=2, per_card=40, action="buy")
        with tempfile.TemporaryDirectory() as d, patch.object(m, "DATA", Path(d)), contextlib.redirect_stdout(io.StringIO()):
            path = Path(d)/"state.json"
            self.assertTrue(m.run_cycle(Reader(), args, path))
            self.assertIsNotNone(json.loads(path.read_text())["pending"])
            s["me"]["assets"].append({"id": 9, "kind": "card", "ref": "LAT-02"})
            s["me"]["cash"] = 90; s["clock"]["tick"] += 1
            self.assertFalse(m.run_cycle(Reader(), args, path))
            state = json.loads(path.read_text())
            self.assertIsNone(state["pending"])
            self.assertEqual(len(state["settlements"]), 1)
            self.assertEqual(calls, [7])

    def test_known_rejection_releases_budget(self):
        s = fixture(); s["board"]["offers"] = [ask()]
        a = next(a for a in actions(s) if a["action"] == "buy")
        class API:
            def accept(self, *args): raise BazaarError("insufficient_cash", status=400)
        class Reader:
            api = API()
            def snapshot(self): return s
            def call(self, *args): return {"your_value": 13}
        state = {}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(BazaarError):
                m.execute_one(Reader(), state, Path(d)/"state.json", a,
                              Namespace(max_spend=100, reserve=10, margin=2))
        self.assertEqual(state["committed"], 0)
        self.assertIsNone(state["pending"])


if __name__ == "__main__":
    unittest.main()
