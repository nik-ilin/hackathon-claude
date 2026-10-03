"""Perfil fast-close: margen pequeño positivo, negociación corta y página elegida por viabilidad. Sin red."""
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from pathlib import Path

import campaigns as cp
import coordinator as co
import market_intel as mi
import negotiation as neg
import page_campaign as pc
import trading as tr
from test_market_intel import TEAM, ask
from test_page_campaign import cargs, inventory, snapm, val_of


class Profile(unittest.TestCase):
    def test_01_profile_applies_only_unset_params(self):
        a = cargs(profile="fast-close", margin=2.0)
        applied = co.apply_profile(a, ["--profile", "fast-close", "--margin", "2"])
        self.assertEqual(a.margin, 2.0, "lo explícito manda")
        self.assertEqual((a.max_proposals, a.negotiation_ticks, a.page_campaign), (2, 4, "auto"))
        self.assertNotIn("margin", applied)
        b = cargs(profile="fast-close")
        co.apply_profile(b, ["--profile", "fast-close"])
        self.assertEqual(b.margin, 1.0)

    def test_02_team_negotiation_opens_near_comparable_and_second_offer_closes_gap(self):
        cfg = co.campaign_cfg(cargs(profile="fast-close", campaign="all", max_proposals=2, negotiation_ticks=4))
        self.assertEqual((cfg.buy_open_frac, cfg.sell_markup, cfg.concession, cfg.max_proposals,
                          cfg.negotiation_ticks), (0.92, 1.08, 0.6, 2, 4))
        base = co.campaign_cfg(cargs(campaign="all"))
        self.assertEqual(base.buy_open_frac, cp.CampaignConfig().buy_open_frac, "sin perfil no cambia nada")

    def test_03_small_positive_margin_deal_accepted_now(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), [ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=500)])
        val = val_of(s)
        st = pc.campaign_state(s, val, "MAL")
        g = st["gains"]["MAL-10"]
        p = pc.plan_target("MAL-10", g["gain"], g["completes_page"], st["level"], s, val,
                           pc.CampaignConfig(margin=1.0), mi.venues_from(s), None, [])
        self.assertEqual(p["best"]["candidate"]["type"], "accept")

    def test_04_directed_bid_opens_near_comparable_not_at_60pct(self):
        s = snapm(inventory())
        val = val_of(s)
        st = pc.campaign_state(s, val, "MAL")
        owner = {"team": "t04", "ownership_confidence": 0.9, "confidence": "LOW", "evidence": [], "last_seen_tick": 1,
                 "wants": [], "wants_of_ours": [], "trade_compatibility": 0.5, "response_history": 1,
                 "owner_wants_target": 0.0}
        orig = pc.owners_ranked
        pc.owners_ranked = lambda *a, **k: [owner]
        try:
            g = st["gains"]["MAL-09"]
            near = pc.plan_target("MAL-09", g["gain"], False, "HIGH", s, val, pc.CampaignConfig(anchor_near=True),
                                  mi.venues_from(s), None, [], {"value": 20})
            far = pc.plan_target("MAL-09", g["gain"], False, "HIGH", s, val, pc.CampaignConfig(),
                                 mi.venues_from(s), None, [], {"value": 20})
        finally:
            pc.owners_ranked = orig
        bid = lambda p: next(r for r in p["routes"] if r["route"] == "B puja dirigida")["cost"]
        self.assertEqual((bid(far), bid(near)), (12, 18), "60 % frente a ~92 % del comparable")

    def test_05_auto_page_choice_by_viability_and_blocked_pages(self):
        s = snapm(inventory(), [ask(901, "v02", 81, "MAL-08", 9, maker="t11", exp=500),
                                ask(902, "v02", 82, "MAL-09", 40, maker="t11", exp=500),
                                ask(903, "v02", 83, "MAL-10", 40, maker="t11", exp=500)])
        choice = pc.choose_page(s, val_of(s), mi.venues_from(s), {}, budget=200)
        mal = next(p for p in choice["pages"] if p["set"] == "MAL")
        self.assertEqual((choice["choice"], mal["est_cost"], mal["blocked"]), ("MAL", 89, []))
        poor = pc.choose_page(s, val_of(s), mi.venues_from(s), {}, budget=50)
        self.assertIsNone(poor["choice"], "no se elige una página que el presupuesto no puede completar")
        none = pc.choose_page(snapm(inventory()), val_of(s), mi.venues_from(s), {}, budget=500)
        self.assertEqual(next(p for p in none["pages"] if p["set"] == "MAL")["blocked"], ["MAL-08", "MAL-09", "MAL-10"])

    def test_06_auto_campaign_runs_inside_coordinator(self):
        s = snapm(inventory(["MAL-08", "MAL-09"]), [ask(970, "v02", 89, "MAL-10", 28, maker="t07", exp=500)])
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                led = {"actions": [], "spent_confirmed": 0, "threads": [], "blocked": {}, "class_tick": {}, "expiry_obs": []}
                a = cargs(profile="fast-close")
                co.apply_profile(a, ["--profile", "fast-close"])
                cands, pl, _ = co.candidates(s, led, a, neg.Journal(d))
            finally:
                co.DATA = saved
        self.assertEqual(pl["page_campaign"]["selection"]["choice"], "MAL")
        acc = [c for c in cands if c.get("page_campaign") == "MAL-10" and c["type"] == "accept"]
        self.assertEqual((acc[0]["offer"], acc[0]["blockers"]), (970, []))


if __name__ == "__main__":
    unittest.main()
