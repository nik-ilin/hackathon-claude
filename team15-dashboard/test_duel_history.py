import unittest

import duel_history


class DuelHistory(unittest.TestCase):
    def test_chart_and_table_cover_every_confirmed_duel(self):
        duels = [
            {'duel': 1, 'session': 1, 'status': 'no_deal', 'result': 0, 'deadline_tick': 10},
            {'duel': 2, 'session': 3, 'status': 'deal', 'result': 4.5,
             'price': 50, 'days': 2, 'rival': '<Rival>', 'deadline_tick': 30},
            {'duel': 3, 'session': 3, 'status': 'deal', 'result': -2,
             'price': 60, 'days': 1, 'rival': 'Rival 2', 'deadline_tick': 31},
        ]
        page = duel_history.render(duels)
        self.assertEqual(page.count('<rect '), 3)
        self.assertEqual(page.count('<tr class='), 3)
        self.assertIn('2 puntuables', page)
        self.assertIn('Ganancia', page)
        self.assertIn('Pérdida', page)
        self.assertIn('&lt;Rival&gt;', page)
        self.assertNotIn('<Rival>', page)


if __name__ == '__main__':
    unittest.main()
