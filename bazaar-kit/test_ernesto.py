"""Don Ernesto (banco): acceso/menú, selección de ventas viables, negociación breve con suelo y protección de páginas."""
import copy
import tempfile
import unittest

import coordinator as co
import negotiation as neg
from test_coordinator import args
from test_dealer_ladder import bid, led0, mine, thread
from test_trading import asset, snap

MENU = {"buys": [{"rarity": "epic", "sets": "released"}, {"rarity": "legendary", "sets": "released"}],
        "sells": [{"list_price": 420, "name": "Gold pack", "pack": "sobre_oro"},
                  {"list_price": 585, "rarity": "legendary", "sets": "released"}], "deals_per_team_per_hour": 4}
BANCO = {"id": "banco", "name": "Don Ernesto", "status": "active", "enabled": True, "menu": MENU}


def world(assets, cash=200, unlocked=("banco",), epic=("LAT-03",), page=False):
    s = snap(assets, cash=cash)
    for c in s["catalog"]["sets"][0]["cards"]:
        c["rarity"] = "epic" if c["id"] in epic else "common"
        c["book"] = 40 if c["id"] in epic else 5
        c["page"] = page or c["id"] != "LAT-03"      # LAT-03 fuera de la página salvo page=True
    s["dealers"] = {"banco": copy.deepcopy(BANCO)}
    s["me"]["unlocked"] = list(unlocked)
    return s


def cands(s, led=None, **kw):
    out, _, _ = co.candidates(s, led or led0(), args(ernesto=True, **kw), neg.Journal(tempfile.mkdtemp()))
    return out


def opens(c):
    return [x for x in c if x["type"] == "dealer_sell_open" and x["dealer"] == "banco"]


class Access(unittest.TestCase):
    def test_access_and_menu_recognised(self):
        ok, why, buys, sells = co.ernesto_access(world([]))
        self.assertTrue(ok)
        self.assertEqual(buys, ["epic", "legendary"])
        self.assertEqual(sells[1]["list_price"], 585)
        self.assertFalse(co.ernesto_access(world([], unlocked=()))[0])
        self.assertFalse(co.ernesto_access({"dealers": {}, "me": {}})[0])

    def test_nothing_viable_is_explained_not_forced(self):
        c = cands(world([asset(1, "LAT-01")]))
        self.assertFalse(opens(c))
        info = [x for x in c if x.get("module") == "ernesto"][0]
        self.assertTrue(any("no tenemos" in b for b in info["blockers"]))

    def test_no_access_no_sale(self):
        s = world([asset(1, "LAT-03"), asset(2, "LAT-03")], unlocked=())
        self.assertFalse(opens(cands(s)))

    def test_flag_off_changes_nothing(self):
        s = world([asset(1, "LAT-03"), asset(2, "LAT-03")])
        out, _, _ = co.candidates(s, led0(), args(), neg.Journal(tempfile.mkdtemp()))
        self.assertFalse([x for x in out if x.get("module") == "ernesto"])


class Selection(unittest.TestCase):
    def test_duplicate_epic_is_offered_with_floor_above_private_value(self):
        c = opens(cands(world([asset(1, "LAT-03"), asset(2, "LAT-03")])))
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["asset"], 2)
        self.assertGreaterEqual(c[0]["floor"], 1)
        self.assertEqual(c[0]["blockers"], [])

    def test_last_copy_and_completed_page_are_never_offered(self):
        self.assertFalse(opens(cands(world([asset(1, "LAT-03")]))))
        s = world([asset(1, "LAT-01"), asset(2, "LAT-02"), asset(3, "LAT-03")], page=True)   # página completa, 1 copia
        self.assertFalse(opens(cands(s)))

    def test_committed_asset_is_not_reused(self):
        s = world([asset(1, "LAT-03"), asset(2, "LAT-03")])
        s["offers"]["offers"] = [{"id": 9, "maker": s["me"]["id"], "status": "open", "give": {"assets": [2]},
                                  "want": {"cash": 50}, "venue": "rastro"}]
        self.assertFalse([x for x in opens(cands(s)) if x["asset"] == 2])

    def test_open_thread_with_banco_blocks_a_second(self):
        s = world([asset(1, "LAT-03"), asset(2, "LAT-03")])
        s["threads"]["open"] = [thread(5, "banco", [], {"sell": {"assets": [1]}})]
        led = dict(led0(), threads=[5])
        self.assertTrue(all(x["blockers"] for x in opens(cands(s, led))))


class Negotiation(unittest.TestCase):
    def step(self, msgs, now=51, floor_cards=(1, 2)):
        s = world([asset(1, "LAT-03"), asset(2, "LAT-03")])
        s["clock"]["tick"] = now
        s["threads"]["open"] = [thread(8, "banco", msgs, {"sell": {"assets": [2]}}, created=50)]
        out = cands(s, dict(led0(), threads=[8]))
        return [x for x in out if x.get("thread") == 8]

    def test_waits_without_a_bid_and_while_awaiting(self):
        self.assertFalse([x for x in self.step([]) if x["type"] != "info"])
        out = self.step([bid("banco", 1, 51, 20, 2), mine(2, 51, 38)], now=51)
        self.assertFalse([x for x in out if x["type"].startswith("dealer_sell")])      # no repite durante la espera

    def test_counters_above_floor_then_stops_after_two(self):
        out = self.step([bid("banco", 1, 51, 20, 2)])
        self.assertEqual(out[0]["type"], "dealer_sell_counter")
        self.assertGreaterEqual(out[0]["price"], out[0]["floor"])
        self.assertGreater(out[0]["price"], 20)
        msgs = [bid("banco", 1, 51, 20, 2, "cancelled"), mine(2, 51, 38), bid("banco", 3, 52, 22, 2, "cancelled"),
                mine(4, 52, 30), bid("banco", 5, 53, 23, 2)]
        out = self.step(msgs, now=53)
        self.assertIn(out[0]["type"], ("dealer_close", "dealer_sell_accept"))          # no hay tercera contraoferta

    def test_final_below_floor_closes_and_final_above_accepts(self):
        low = self.step([bid("banco", 1, 51, 20, 2, "cancelled"), mine(2, 51, 38), bid("banco", 3, 52, 1, 2, final=True)], 52)
        self.assertEqual(low[0]["type"], "dealer_close")
        ok = self.step([bid("banco", 1, 51, 20, 2, "cancelled"), mine(2, 51, 38), bid("banco", 3, 52, 33, 2, final=True)], 52)
        self.assertEqual((ok[0]["type"], ok[0]["price"]), ("dealer_sell_accept", 33))

    def test_purchase_beyond_capital_stays_blocked(self):
        s = world([asset(1, "LAT-01")], cash=40)
        s["catalog"]["sets"][0]["cards"][2]["rarity"] = "legendary"
        c = cands(s)
        buys = [x for x in c if x["type"] == "dealer_open" and x["dealer"] == "banco"]
        self.assertTrue(all(x["blockers"] for x in buys))


if __name__ == "__main__":
    unittest.main()
