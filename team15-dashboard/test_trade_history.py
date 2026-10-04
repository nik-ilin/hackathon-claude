import tempfile
import unittest
from pathlib import Path

import trade_history


class TradeHistory(unittest.TestCase):
    def test_isolated_buy_uses_observed_private_value(self):
        raw = [{'settlement': 1, 'tick': 10, 'price': 60, 'parties': ['t15', 'picaros'],
                'items': [{'ref': 'CHA-10', 'frm': 'picaros', 'to': 't15'}]}]
        history = [{'tick': 9, 'cash': 635, 'collection_value': 1573.8, 'deals': 61},
                   {'tick': 10, 'cash': 575, 'collection_value': 1636.8, 'deals': 62}]
        row = trade_history.ledger(raw, history)[0]
        self.assertEqual((row['direction'], row['net'], row['quality']), ('Compra', 3, 'Buena'))

    def test_multiple_trades_or_other_cash_movement_remain_unrated(self):
        raw = [{'settlement': 1, 'tick': 10, 'price': 60,
                'items': [{'ref': 'A', 'frm': 'x', 'to': 't15'}]},
               {'settlement': 2, 'tick': 11, 'price': 50,
                'items': [{'ref': 'B', 'frm': 'x', 'to': 't15'}]}]
        history = [{'tick': 9, 'cash': 635, 'collection_value': 100, 'deals': 60},
                   {'tick': 12, 'cash': 525, 'collection_value': 210, 'deals': 62}]
        self.assertTrue(all(r['net'] is None for r in trade_history.ledger(raw, history)))
        one = trade_history.ledger(raw[:1], [history[0], {**history[1], 'deals': 61}])[0]
        self.assertIsNone(one['net'])

    def test_deduplicates_feed_copies(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'feed.jsonl'
            event = {'type': 'settlement', 'payload': {'settlement': 1, 'tick': 10,
                     'parties': ['t15', 'x'], 'items': []}}
            path.write_text(json.dumps(event) + '\n' + json.dumps(event) + '\n')
            self.assertEqual(len(trade_history.settlements([path, path])), 1)


if __name__ == '__main__':
    unittest.main()
