import unittest

from planner import build_rank, offer_team


CATALOG = {"values": {"copy_marginals": [1, .25, .1], "page_bonus": .25},
           "sets": [{"id": "LAV", "released": True, "cards": [
               {"id": "LAV-01", "set": "LAV", "book": 20, "rarity": "common", "page": True},
               {"id": "LAV-02", "set": "LAV", "book": 40, "rarity": "rare", "page": True}]}]}
ASSETS = [{"id": 11, "kind": "card", "ref": "LAV-01", "your_value": 5},
          {"id": 12, "kind": "card", "ref": "LAV-01", "your_value": 5}]
ME = {"id": "t15", "assets": ASSETS, "affinity": {"LAV": 1},
      "collection_value": 25, "cash": 150, "score": {"score": 6}}
VENUES = [{"venue": "rastro", "status": "open", "owner": "house", "fee_bps": 500, "fee_per_card": 1}]
RIVALS = {"teams": [{"team": "t04", "sample": 2, "held": [], "sought": ["LAV-01"],
                     "duplicates_offered": []}]}


def bid(offer_id=50, maker="alias", price=12):
    return {"id": offer_id, "maker": maker, "status": "open", "venue": "rastro", "to": None,
            "give": {"cash": price, "assets": [], "types": []},
            "want": {"cash": 0, "assets": [], "types": ["card:LAV-01"]}, "expires_tick": 20}


class PlannerTest(unittest.TestCase):
    def test_live_bid_is_ranked_with_our_fee_and_private_loss(self):
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {"rastro": [bid()]}, [],
                         RIVALS, {50: "t04"}, reserve=100)
        self.assertTrue(out["verified"])
        row = out["trades"][0]
        self.assertEqual((row["kind"], row["team"], row["offer_id"]), ("live", "t04", 50))
        self.assertEqual((row["price"], row["fee"], row["delta_value"], row["surplus"]), (12, 2, -5, 5))
        self.assertEqual(out["inventory"][0]["free_surplus"], 1)

    def test_alias_is_never_guessed_as_a_team(self):
        self.assertIsNone(offer_team(bid(), {}))
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {"rastro": [bid()]}, [],
                         {"teams": []}, {}, reserve=100)
        self.assertEqual(out["trades"], [])

    def test_proposal_is_separate_and_uses_our_floor(self):
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {}, [], RIVALS, {}, reserve=100)
        row = out["trades"][0]
        self.assertEqual((row["kind"], row["price"], row["surplus"]), ("proposal", 7, 2))
        self.assertIn("incierta", row["confidence"])

    def test_proposal_price_uses_observed_market_reference(self):
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {}, [], RIVALS, {},
                         market_refs={"LAV-01": {"fair": 12.2, "confidence": "MEDIUM"}})
        row = out["trades"][0]
        self.assertEqual(row["price"], 13)
        self.assertIn("Referencia de mercado 13 P", row["why"])

    def test_unverified_valuation_blocks_numeric_trades(self):
        me = {**ME, "collection_value": 30}
        out = build_rank(me, CATALOG, {"tick": 10}, VENUES, {"rastro": [bid()]}, [],
                         RIVALS, {50: "t04"})
        self.assertFalse(out["verified"])
        self.assertEqual(out["trades"], [])

    def test_locked_duplicate_is_not_proposed(self):
        own = [{**bid(61, "t15"), "give": {"cash": 0, "assets": [12], "types": []}}]
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {}, own, RIVALS, {})
        self.assertEqual(out["inventory"][0]["free_surplus"], 0)

    def test_reciprocal_interest_produces_conditional_swap(self):
        rivals = {"teams": [{**RIVALS["teams"][0], "duplicates_offered": ["LAV-02"]}]}
        out = build_rank(ME, CATALOG, {"tick": 10}, VENUES, {}, [], rivals, {})
        swaps = [row for row in out["trades"] if row["action"] == "Proponer canje"]
        self.assertEqual(len(swaps), 1)
        self.assertEqual((swaps[0]["give"], swaps[0]["receive"], swaps[0]["price"]),
                         (["LAV-01"], ["LAV-02"], 0))
        self.assertGreater(swaps[0]["surplus"], 0)


if __name__ == "__main__":
    unittest.main()
