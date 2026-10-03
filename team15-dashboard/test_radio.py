import unittest

from radio import interpret, radio_event


class RadioTest(unittest.TestCase):
    def test_rumour_only_matches_the_named_set_and_rarity(self):
        catalog = {'sets': [{'id': 'MAL', 'name': 'Malasaña', 'cards': [
            {'id': 'MAL-05', 'rarity': 'common'}, {'id': 'MAL-09', 'rarity': 'rare'}]}]}
        guide = [{'ref': 'MAL-05'}, {'ref': 'MAL-09'}]
        event = {'id': 4, 'tick': 403, 'type': 'news.posted',
                 'payload': {'source': 'radio', 'source_name': 'Radio Rastro',
                             'headline': 'El Chato is looking for rare Malasaña cards',
                             'body': 'They say he pays above the usual price today.'}}
        item = radio_event(event)
        self.assertEqual(interpret([item], catalog, guide)[0]['matches'], ['MAL-09'])
        self.assertIn('cotización real', interpret([item], catalog, guide)[0]['action'])
        self.assertIn('Mantén los precios', interpret([item], catalog, guide[:1])[0]['action'])
        item['body'] += ' One hour, no more.'
        self.assertIn('hora anunciada ya pasó',
                      interpret([item], catalog, guide, current_tick=524)[0]['action'])

    def test_unrelated_news_does_not_change_price(self):
        item = {'id': 5, 'tick': 331, 'headline': 'Atleti win 2-1', 'body': 'Celebrations in Madrid'}
        out = interpret([item], {'sets': []}, [{'ref': 'MAL-05'}])[0]
        self.assertEqual(out['matches'], [])
        self.assertIn('no cambiar precios', out['action'])


if __name__ == '__main__':
    unittest.main()
