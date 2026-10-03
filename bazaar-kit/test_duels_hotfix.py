"""Regresiones del hotfix de duelos (umbral, tendencia, estancamiento, fases y cierre seguro). Sin red."""
import unittest

import duels as dl
import duel_tree


def duel(role, lim, steps, deadline=475, offer=None, did=1):
    """steps: {tick: precio} del rival (el rival no reenvía su precio cada tick)."""
    hist = sorted(steps.items())
    return {"duel": did, "status": "live", "role": role, "your_limit": lim, "rival": "R", "deadline_tick": deadline,
            "rival_offer": {"price": hist[-1][1]} if hist else None,
            "messages": [{"tick": t, "from": "R", "price": p} for t, p in hist], "your_offer": offer,
            "issues": ["price"]}


def at(role, lim, steps, tick, same=1, offer=None):
    d = duel(role, lim, {t: p for t, p in steps.items() if t <= tick}, offer=offer)
    f = dl.analyze(d, tick, same)
    return f, dl.decide(d, f)


S2306 = {457: 115, 459: 107}
S2354 = {458: 107, 463: 106, 465: 105, 466: 103, 467: 101}
S2372 = {457: 120, 459: 137, 463: 147, 465: 150, 466: 152, 467: 154, 468: 155, 469: 156, 470: 158}


class Hotfix(unittest.TestCase):
    def test_01_buyer_strong_surplus_does_not_need_60pct_of_limit(self):
        f, (a, _) = at("buyer", 144, S2354, 467, same=3)  # +43, 29.9 %: antes exigía 86.4
        self.assertEqual((f["phase"], a), ("MID", "accept"))
        self.assertNotIn("good_margin_threshold", f)

    def test_02_seller_positive_surplus_same(self):
        f, (a, _) = at("seller", 100, {460: 120, 462: 121, 463: 121, 465: 122}, 467)
        self.assertEqual(f["surplus_now"], 22)
        self.assertEqual(a, "accept")

    def test_03_stalled_buyer_counters_then_accepts_instead_of_waiting_forever(self):
        f, (a, _) = at("buyer", 124, S2306, 463)
        self.assertEqual((f["trend"], f["stalled"], a), ("STALLED", True, "counter"))
        f, (a, _) = at("buyer", 124, S2306, 467, offer={"price": 101})
        self.assertEqual((f["phase"], a), ("MID", "accept"))

    def test_04_stalled_seller_same(self):
        steps = {460: 110, 461: 112}
        f, (a, _) = at("seller", 100, steps, 465)
        self.assertEqual((f["trend"], a), ("STALLED", "counter"))
        self.assertGreater(dl.counter_price(f), 112, "el vendedor pide más que la oferta rival, nunca su límite")
        f, (a, _) = at("seller", 100, steps, 468, offer={"price": 116})
        self.assertEqual(a, "accept")

    def test_05_strong_improving_rival_may_justify_waiting_early(self):
        f, (a, _) = at("seller", 135, S2372, 465)
        self.assertEqual((f["phase"], f["trend"], a), ("EARLY", "STRONG_IMPROVEMENT", "wait"))

    def test_06_weak_improvement_and_strong_surplus_accepts_mid_game(self):
        f, (a, why) = at("buyer", 144, S2354, 467)
        self.assertEqual((f["trend"], a), ("WEAK_IMPROVEMENT", "accept"))
        self.assertLess(f["expected_extra_gain"], f["risk_cost"])

    def test_07_last_safe_ticks_with_positive_offer_accepts(self):
        for tick in (472, 473, 474):
            f, (a, _) = at("seller", 135, S2372, tick, same=3)
            self.assertEqual(a, "accept", (tick, f))
        f, (a, why) = at("seller", 135, {470: 136}, 474)
        self.assertEqual(a, "accept", "+1 en la ventana final también se cierra")

    def test_08_outside_own_limit_never_accepts(self):
        for tick in range(461, 475):
            _, (a, _) = at("buyer", 100, {460: 130, 470: 125}, tick)
            self.assertNotEqual(a, "accept")
        self.assertFalse([c for c in dl.duel_candidates([duel("buyer", 100, {460: 130})], 474)
                          if c["type"] == "duel_accept"])

    def test_09_buyer_trend_sign(self):
        f, _ = at("buyer", 150, {460: 120, 462: 112, 464: 104}, 464)
        self.assertGreater(f["recent_improvement_rate"], 0, "comprador: que baje es mejora")
        f, _ = at("buyer", 150, {460: 104, 462: 112}, 462)
        self.assertEqual(f["trend"], "WORSENING")

    def test_10_seller_trend_sign(self):
        f, _ = at("seller", 100, {460: 104, 462: 112, 464: 120}, 464)
        self.assertGreater(f["recent_improvement_rate"], 0, "vendedor: que suba es mejora")
        f, _ = at("seller", 100, {460: 120, 462: 112}, 462)
        self.assertEqual(f["trend"], "WORSENING")

    def test_11_safe_ticks_configured_vs_effective_visible(self):
        d = duel("buyer", 144, S2354)
        facts = duel_tree.decision_facts(d, 467, same_deadline=3)
        self.assertEqual((facts["configured_safe_ticks"], facts["effective_safe_ticks"]), (1, 3))
        self.assertIn("mismo deadline", facts["safe_ticks_note"])
        for k in ("role", "own_limit", "rival_price", "surplus_now", "surplus_ratio", "ticks_left", "phase", "trend",
                  "recent_improvement_rate", "stalled", "expected_extra_gain", "risk_cost", "policy_action", "reason"):
            self.assertIn(k, facts)
        self.assertIn("GOOD_SHARE", facts["deprecated"])

    def test_12_never_waits_through_whole_duel_while_profitable(self):
        for steps, role, lim in ((S2306, "buyer", 124), (S2354, "buyer", 144), (S2372, "seller", 135),
                                 ({461: 90}, "buyer", 100)):
            actions, offer = [], None
            for tick in range(461, 475):
                f, (a, _) = at(role, lim, steps, tick, same=3, offer=offer)
                actions.append(a)
                if a == "counter":
                    offer = {"price": dl.counter_price(f)}
                if a == "accept":
                    break
            self.assertEqual(actions[-1], "accept", (role, lim, actions))


if __name__ == "__main__":
    unittest.main()
