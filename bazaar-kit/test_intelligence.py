"""Capa de inteligencia de contrapartes (SQLite) — sin red ni operaciones."""
import os
import sqlite3
import tempfile
import unittest

import intelligence as it
from test_market_intel import TEAM, A, ask, bid, offer, snap, swap


def feed_settlement(sid, tick, ref, frm, to, price=20):
    return {"id": 9000 + sid, "tick": tick, "type": "settlement", "payload": {
        "settlement": sid, "tick": tick, "kind": "trade", "parties": [frm, to], "venue": "v02", "fee": 0,
        "items": [{"id": 700 + sid, "kind": "card", "ref": ref, "frm": frm, "to": to}], "price": price}}


class DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "market.db")
        self.intel = it.Intelligence(self.path, TEAM)

    def tearDown(self):
        self.intel.close()
        self.dir.cleanup()

    def test_01_schema_tables_and_indexes(self):
        tables = {r[0] for r in self.intel.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("teams", "cards", "venues", "offers", "offer_snapshots", "settlements", "market_snapshots",
                  "inventory_evidence", "team_card_interest", "team_set_interest", "counterparty_profiles",
                  "reservation_estimates", "interactions", "dealer_interactions", "model_metadata"):
            self.assertIn(t, tables)
        idx = {r[0] for r in self.intel.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"ix_offers_maker", "ix_offers_ref", "ix_offers_venue", "ix_snap_tick", "ix_ev_team"} <= idx)

    def test_02_duplicate_ingestion_is_idempotent_and_persists(self):
        s = snap([A(1, "LAT-01")], [ask(10, "v02", 70, "MAL-02", 12, maker="t07"), bid(11, "v02", "LAT-03", 9, maker="t07")],
                 feed=[feed_settlement(1, 99, "MAL-03", "t03", "t07")])
        self.intel.ingest(s)
        first = self.intel.stats()
        self.intel.ingest(s)
        self.assertEqual({k: v for k, v in self.intel.stats().items() if k != "path"},
                         {k: v for k, v in first.items() if k != "path"})
        self.assertEqual(first["offers"], 2)
        self.assertEqual(first["settlements"], 1)
        self.intel.close()
        again = it.Intelligence(self.path, TEAM)
        self.assertEqual(again.stats()["offers"], 2)
        self.assertEqual(again.meta("last_tick"), "100")
        self.intel = again

    def test_03_card_and_set_interest_with_escalation_and_decay(self):
        bids = [bid(20 + i, "v02", "LAT-03", p, maker="t07", created=60 + 5 * i) for i, p in enumerate((55, 63, 70, 77, 82))]
        bids += [bid(30, "v02", "LAT-02", 10, maker="t07", created=90), bid(31, "v02", "MAL-03", 5, maker="t09", created=90)]
        self.intel.ingest(snap([A(1, "LAT-01")], bids))
        self.intel.update_models(100)
        w = self.intel.p_wants("t07", "LAT-03")
        self.assertGreater(w["p"], 0.9)
        self.assertTrue(any("escalada" in e for e in w["evidence"]))
        sets = self.intel.set_interest("t07")
        self.assertEqual(sets[0]["set_id"], "LAT")
        self.assertEqual(self.intel.p_wants("t07", "MAL-01")["confidence"], "UNKNOWN")
        self.assertAlmostEqual(it.decay(it.HALF_LIFE), 0.5)

    def test_04_reservation_lower_bound_no_false_precision(self):
        bids = [bid(20 + i, "v02", "LAT-03", p, maker="t07", created=60 + 5 * i) for i, p in enumerate((55, 63, 70, 77, 82))]
        self.intel.ingest(snap([A(1, "LAT-01")], bids))
        self.intel.update_models(100)
        r = self.intel.reservation("t07", "LAT-03")
        self.assertEqual(r.lower_bound, 82)
        self.assertIsNone(r.upper_bound, "el máximo no se conoce")
        self.assertEqual(r.confidence, "MEDIUM")
        self.assertGreater(r.median_estimate, 82)
        self.assertEqual(self.intel.reservation("t07", "LAT-02").confidence, "UNKNOWN")
        one = it.Intelligence(os.path.join(self.dir.name, "b.db"), TEAM)
        one.ingest(snap([], [bid(40, "v02", "LAT-03", 60, maker="t08")]))
        one.update_models(100)
        r1 = one.reservation("t08", "LAT-03")
        self.assertEqual((r1.lower_bound, r1.median_estimate, r1.confidence), (60, None, "LOW"))
        one.close()

    def test_05_ownership_evidence_decay_and_sale(self):
        self.intel.ingest(snap([], [ask(10, "v02", 70, "MAL-02", 12, maker="t07", created=99)]))
        own = self.intel.p_owns("t07", "MAL-02")
        self.assertGreater(own["p"], 0.8)
        self.assertEqual(self.intel.p_owns("t07", "LAT-01")["p"], 0.0)
        self.assertEqual(self.intel.p_owns("t07", "LAT-01")["confidence"], "UNKNOWN")
        self.intel.tick = 99 + 2 * it.HALF_LIFE
        self.assertLess(self.intel.p_owns("t07", "MAL-02")["p"], own["p"], "la evidencia vieja pesa menos")
        self.intel.ingest(snap([], feed=[feed_settlement(2, 101, "MAL-02", "t07", "t09")], tick=101))
        self.assertLess(self.intel.p_owns("t07", "MAL-02")["p"], 0.2, "la vendió después")
        self.assertGreater(self.intel.p_owns("t09", "MAL-02")["p"], 0.9)

    def test_06_strategy_classification_and_best_counterparties(self):
        sells = [ask(50 + i, "v02", 80 + i, r, 9, maker="t11") for i, r in enumerate(("LAT-01", "MAL-01", "LAT-02", "MAL-02"))]
        page = [bid(60 + i, "v02", r, 9, maker="t07") for i, r in enumerate(("LAT-01", "LAT-02", "LAT-03"))]
        self.intel.ingest(snap([], sells + page + [swap(70, "v02", 90, "LAT-03", "MAL-01", maker="t11")]))
        self.intel.update_models(100)
        self.assertIn(self.intel.profile("t11")["dominant_strategy"], ("LIQUIDATION", "CASH_ACCUMULATION"))
        self.assertEqual(self.intel.profile("t07")["dominant_strategy"], "PAGE_COMPLETION")
        self.assertEqual(self.intel.profile("t99")["confidence"], "UNKNOWN")
        best = self.intel.best_counterparties("LAT-03", our_tradables=["MAL-01"])
        self.assertEqual(best[0]["team"], "t11")
        self.assertGreater(best[0]["interest_match"], 0, "t11 pide MAL-01, que nosotros tenemos de sobra")

    def test_07_best_buyers_directed_and_counterparty_value(self):
        self.intel.ingest(snap([A(1, "LAT-01")], mine=[offer(80, "v02", {"cash": 86}, {"types": ["card:LAT-01"]},
                                                          maker="t14", to=TEAM)]))
        self.intel.update_models(100)
        b = self.intel.best_buyers("LAT-01")
        self.assertEqual((b[0]["team"], b[0]["directed_bid_to_us"], b[0]["reservation"][0]), ("t14", 86, 86))
        cv = self.intel.counterparty_value("LAT-01", "t14", market_value=20)
        self.assertEqual(cv["lower_bound"], 86)
        self.assertIn("no su valor privado", cv["note"])
        ctx = self.intel.context("t14")
        self.assertEqual(ctx.likely_wants[0]["ref"], "LAT-01")

    def test_08_db_failure_degrades(self):
        import contextlib
        import io
        import coordinator as co

        class Boom:
            def ingest(self, s):
                raise sqlite3.OperationalError("disk I/O error")

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(co.feed_intel(Boom(), snap([])))
        self.assertIn("INTELIGENCIA: fallo", out.getvalue())


if __name__ == "__main__":
    unittest.main()
