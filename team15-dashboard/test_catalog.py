import unittest
from types import SimpleNamespace

from app import enrich_catalog_market, render_catalog


class CatalogViewTest(unittest.TestCase):
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
