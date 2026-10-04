import json
import tempfile
import unittest
from pathlib import Path

from history_store import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def test_records_one_private_credential_free_snapshot_per_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "data" / "history.sqlite3")
            sample = {"tick": 10, "score": {"score": 4}, "catalog_rows": [{"ref": "LAT-01"}],
                      "api_key": "must-not-persist", "headers": {"X-Team-Key": "secret"}}
            self.assertTrue(store.record(sample))
            self.assertFalse(store.record({**sample, "score": {"score": 5}}))
            text = Path(tmp, "data", "history.sqlite3").read_bytes()
            self.assertNotIn(b"must-not-persist", text)
            self.assertNotIn(b"secret", text)
            rows = store.ticks()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["score"]["score"], 4)
            self.assertEqual(store.health()["last_tick"], 10)

    def test_score_and_card_series_preserve_available_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.sqlite3")
            store.record({"tick": 10, "score": {"score": 3, "negotiating": 2, "market": 1},
                          "catalog_rows": [{"ref": "LAT-01", "sold_median": 20,
                                           "sold_prices": [18, 20], "stock": 1}]})
            self.assertEqual(store.series("score")["points"][0]["score"], 3)
            card = store.series("card", ref="LAT-01")["points"][0]
            self.assertEqual(card["sold_median"], 20)
            self.assertEqual(card["sold_count"], 2)

    def test_rejects_unknown_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.sqlite3")
            with self.assertRaises(ValueError):
                store.series("agents")


if __name__ == "__main__":
    unittest.main()
