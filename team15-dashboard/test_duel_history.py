import unittest

import duel_history


class DuelHistory(unittest.TestCase):
    def test_analysis_separates_confirmed_loss_from_observed_opportunity(self):
        duels = [
            {'duel': 1, 'session': 3, 'status': 'deal', 'role': 'buyer',
             'your_limit': 100, 'price': 99, 'days': 10, 'your_days_weight': 9,
             'days_meaning': 'each delivery day costs you this much cash',
             'issues': ['price', 'days'], 'result': -89, 'rival': 'R'},
            {'duel': 2, 'session': 3, 'status': 'no_deal', 'role': 'seller',
             'your_limit': 50, 'issues': ['price'], 'rival': 'R',
             'messages': [{'from': 'R', 'price': 60, 'tick': 4}]},
            {'duel': 3, 'session': 3, 'status': 'no_deal', 'role': 'seller',
             'your_limit': 50, 'issues': ['price', 'days'], 'your_days_weight': 2,
             'days_meaning': 'each delivery day adds this much cash to your side',
             'rival': 'R', 'messages': [{'from': 'R', 'price': 49, 'days': 10, 'tick': 4}]},
        ]
        a = duel_history.analysis(duels)
        self.assertEqual((a['negative'], a['negative_sum']), (1, -89))
        self.assertEqual(a['positive_offer_no_deal'], 1)
        self.assertEqual(a['reviews'][0]['days_effect'], -90)

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
