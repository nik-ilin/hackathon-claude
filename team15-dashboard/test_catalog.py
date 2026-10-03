import unittest
from types import SimpleNamespace

from app import enrich_catalog_market, render_catalog, render_dashboard_overview, render_command_deck


class CatalogViewTest(unittest.TestCase):
    def test_buy_margin_requires_verified_private_value_and_confirmed_sale(self):
        row = {'ref': 'LAT-11', 'set': 'La Latina', 'released': True, 'stock': 0,
               'free': 0, 'buy_ceiling': 232, 'sold_median': 160,
               'held_by': [], 'wanted_by': [], 'minted': 2, 'print_run': 9}
        public = render_dashboard_overview({'catalog_rows': [row], 'verified': False})
        private = render_dashboard_overview({'catalog_rows': [row], 'verified': True})
        self.assertIn('0/0</strong>', public)
        self.assertIn('1/1</strong>', private)
        self.assertIn('+72 P', private)

    def test_race_gap_uses_public_score_when_private_score_is_newer(self):
        html = render_command_deck({
            'score': {'score': 30}, 'verified': True,
            'leaderboard': {'teams': [{'team': 't04', 'score': 25},
                                      {'team': 't15', 'score': 23}]},
        })
        self.assertIn('Faltan 2 puntos', html)
        self.assertIn('30</strong>', html)

    def test_confirmed_sales_and_mint_counts_are_kept_distinct_from_limits(self):
        rows = [{'ref': 'LAV-01', 'set': 'Lavapiés', 'rarity': 'rare', 'released': True,
                 'stock': 0, 'free': 0, 'sell_floor': None, 'buy_ceiling': 20,
                 'held_by': ['t04'], 'wanted_by': ['t06']}]
        catalog = {'sets': [{'cards': [{'id': 'LAV-01', 'minted': 4, 'print_run': 30}]}]}
        enrich_catalog_market(rows, catalog, {'LAV-01': SimpleNamespace(settled=[11, 15, 19])})
        self.assertEqual(rows[0]['sold_median'], 15)
        self.assertEqual((rows[0]['minted'], rows[0]['print_run']), (4, 30))
        html = render_catalog({'catalog_rows': rows, 'verified': True, 'buy_capacity': 12})
        self.assertIn('15 P', html)
        self.assertIn('11–19 P', html)
        self.assertIn('4 / 30', html)
        self.assertIn('12 P', html)  # cash cap, separately explained from value cap
        self.assertIn('Valor 20 P · caja 12 P', html)

    def test_no_sale_or_mint_data_is_not_invented(self):
        rows = [{'ref': 'LAV-02', 'set': 'Lavapiés', 'rarity': 'common', 'released': True,
                 'stock': 0, 'free': 0, 'sell_floor': None, 'buy_ceiling': None,
                 'held_by': [], 'wanted_by': []}]
        enrich_catalog_market(rows, {'sets': []}, {})
        html = render_catalog({'catalog_rows': rows, 'verified': False})
        self.assertIn('Sin venta registrada', html)
        self.assertIn('Sin tirada publicada', html)
        self.assertIn('Sin valoración privada verificada', html)


if __name__ == '__main__':
    unittest.main()

class StrategyExportTest(unittest.TestCase):
    def test_strategy_export_preserves_ratios_and_ranking(self):
        from app import strategy_export
        rows = [{'ref': 'LAV-01', 'set': 'Lavapiés', 'rarity': 'rare', 'released': True,
                 'stock': 0, 'free': 0, 'sell_floor': None, 'buy_ceiling': 20,
                 'held_by': ['t01', 't02'], 'wanted_by': ['t03'], 'minted': 4,
                 'print_run': 30, 'sold_median': 15, 'sold_prices': [11, 15, 19]}]
        payload = strategy_export({'catalog_rows': rows, 'verified': True, 'buy_capacity': 12,
                                   'tick': 8, 'leaderboard': {'teams': [
                                       {'team': 't15', 'score': 10}, {'team': 't02', 'score': 12}]}})
        self.assertEqual(payload['schema'], 'team15.strategy.v2')
        self.assertEqual(payload['ranking']['position'], 2)
        self.assertEqual(payload['cards'][0]['holder_count_observed'], 2)
        self.assertEqual(payload['cards'][0]['demand_to_holder_ratio'], .5)
        self.assertEqual(payload['cards'][0]['scarcity_ratio_minted_to_print_run'], .1333)
