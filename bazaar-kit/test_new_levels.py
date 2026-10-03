"""Tests offline de new_levels.py: detección de niveles activos, spares del taller, ruta del taller y sobres."""
import unittest

import new_levels as nl

CATALOG = {"sets": [{"id": "LAV", "cards": [{"id": f"LAV-0{i}", "page": True} for i in range(1, 4)]},
                    {"id": "MAL", "cards": [{"id": f"MAL-0{i}", "page": True} for i in range(1, 4)]}]}

ANNOUNCED = {"levels": [{"id": "picaros", "kind": "persona", "name": "Los Pícaros", "state": "announced",
                         "teaser": "«Quick deals. Few questions.»"},
                        {"id": "taller", "kind": "taller", "name": "The Workshop", "state": "announced",
                         "teaser": "«Three spares. One surprise.»"}]}
ACTIVE = {"levels": [{"id": "picaros", "kind": "persona", "name": "Los Pícaros", "state": "active",
                      "how": "Open a thread with them (with: picaros)."},
                     {"id": "taller", "kind": "taller", "name": "The Workshop", "state": "active",
                      "how": 'Hand in three spare cards: POST /api/taller {"assets": [id, id, id]}; one surprise back.'}]}
DEALERS = {"personas": [{"id": "picaros", "status": "active", "menu": {"sells": []},
                         "traits": {"shrewdness": 0.9, "strictness": 0.2}}]}


def card(i, ref, v):
    return {"id": i, "kind": "card", "ref": ref, "your_value": v}


def me():
    return {"id": "t15", "assets": [
        # LAV completa (01, 02, 03): sobrantes 2 de LAV-01 y 1 de LAV-02
        card(1, "LAV-01", 9), card(2, "LAV-01", 3), card(3, "LAV-01", 4),
        card(4, "LAV-02", 8), card(5, "LAV-02", 2),
        card(6, "LAV-03", 1),                                   # única copia: nunca spare
        # MAL incompleta: MAL-01 x2 → un sobrante; la que se queda es la de menor id
        card(7, "MAL-01", 6), card(8, "MAL-01", 7),
        {"id": 90, "kind": "pack", "ref": "sobre_barrio", "name": "Neighbourhood pack", "your_value": 20}]}


class Levels(unittest.TestCase):
    def test_announced_is_not_active(self):
        self.assertFalse(nl.is_active(nl.level_of(ANNOUNCED, "picaros")))
        self.assertTrue(nl.is_active(nl.level_of(ACTIVE, "picaros")))
        self.assertTrue(nl.is_active(None, nl.dealer_of(DEALERS, "picaros")))

    def test_report_prints_how_menu_and_plan(self):
        txt = "\n".join(nl.report(ACTIVE, DEALERS, me(), CATALOG))
        self.assertIn("how: Open a thread", txt)
        self.assertIn("traits:", txt)
        self.assertIn("safe_to_accept", txt)
        self.assertIn("POST /api/taller", txt)
        self.assertIn("sobres sin abrir: [(90, 'sobre_barrio')]", txt)

    def test_report_announced_has_no_plan_and_pending_route(self):
        txt = "\n".join(nl.report(ANNOUNCED, {"personas": []}, me(), CATALOG))
        self.assertNotIn("plan Pícaros", txt)
        self.assertIn("envío: pendiente", txt)


class Workshop(unittest.TestCase):
    def test_three_lowest_value_safe_spares(self):
        sp = nl.workshop_spares(me(), CATALOG)
        self.assertEqual([s["id"] for s in sp], [5, 2, 3])
        ids = {s["id"] for s in nl.workshop_spares(me(), CATALOG, n=10)}
        self.assertEqual(ids, {2, 3, 5, 8})                     # nunca 1, 4, 6 (página) ni 7 (la que se queda)

    def test_committed_copies_are_skipped_and_one_copy_stays(self):
        offers = [{"maker": "t15", "status": "open", "give": {"assets": [{"id": 8}]}}]
        ids = {s["id"] for s in nl.workshop_spares(me(), CATALOG, offers, n=10)}
        self.assertNotIn(8, ids)
        self.assertNotIn(7, ids)                                # si 8 ya sale, 7 es la última copia

    def test_request_needs_route(self):
        self.assertIsNone(nl.workshop_request(None, [1, 2, 3]))
        self.assertIsNone(nl.workshop_request("Bring three spares to the workshop.", [1, 2, 3]))
        r = nl.workshop_request(nl.level_of(ACTIVE, "taller")["how"], [5, 2, 3])
        self.assertEqual(r, {"method": "POST", "path": "/api/taller", "body": {"assets": [5, 2, 3]}})
        r = nl.workshop_request('POST /api/workshop {"cards": [1]}', [5])
        self.assertEqual(r["body"], {"cards": [5]})


def rcard(set_, i, rarity, book):
    return {"id": f"{set_}-{i:02d}", "rarity": rarity, "book": book, "page": i <= 3}


# Catálogo con rarezas y book: LAT publicado (comunes 1-3 de 2 P, poco comunes 4-5 de 20 P y 40 P, rara 6 oculta),
# RET sin publicar (su poco común de 500 P no puede salir del taller).
RCAT = {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25},
        "sets": [{"id": "LAT", "released": True,
                  "cards": [rcard("LAT", 1, "common", 2), rcard("LAT", 2, "common", 2), rcard("LAT", 3, "common", 2),
                            rcard("LAT", 4, "uncommon", 20), rcard("LAT", 5, "uncommon", 40),
                            dict(rcard("LAT", 6, "rare", 90), hidden=True)]},
                 {"id": "RET", "released": False, "cards": [rcard("RET", 4, "uncommon", 500)]}]}


def rme(extra=()):
    a = lambda i, ref, r, v: {"id": i, "kind": "card", "ref": ref, "rarity": r, "your_value": v}
    return {"id": "t15", "affinity": {"LAT": 1.0, "RET": 1.0}, "assets": [
        a(1, "LAT-01", "common", 2), a(2, "LAT-01", "common", 0.5), a(3, "LAT-01", "common", 0.5),
        a(4, "LAT-02", "common", 2), a(5, "LAT-02", "common", 0.5),
        a(10, "LAT-04", "uncommon", 20), a(11, "LAT-04", "uncommon", 5)] + list(extra)}


class WorkshopRarity(unittest.TestCase):
    def test_spares_never_mix_rarities_and_keep_one_copy(self):
        sp = nl.workshop_spares(rme(), RCAT)
        self.assertEqual(sorted(s["id"] for s in sp), [2, 3, 5])     # tres comunes; la poco común 11 no se mezcla
        self.assertEqual({s["rarity"] for s in sp}, {"common"})
        pool = nl.spare_pool(rme(), RCAT)
        self.assertEqual([s["id"] for s in pool["uncommon"]], [11])  # 10 se queda (menor id)
        self.assertNotIn(1, {s["id"] for v in pool.values() for s in v})
        self.assertNotIn(4, {s["id"] for v in pool.values() for s in v})

    def test_ev_uses_released_next_rarity_only(self):
        plan = nl.workshop_plan(rme(), RCAT)
        self.assertEqual((plan["rarity"], plan["to"]), ("common", "uncommon"))
        # LAT-04 ya la tenemos dos veces (3.ª copia: 20 × 0,1 = 2 P), LAT-05 no (40 P); RET-04 no está publicada.
        self.assertEqual(plan["pool"], 2)
        self.assertEqual(plan["ev"], 21.0)
        # Perdemos 2.ª y 3.ª copia de LAT-01 y 2.ª de LAT-02: 0,5 + 0,2 + 0,5 = 1,2 P (la página sigue completa).
        self.assertEqual(plan["loss"], 1.2)
        self.assertTrue(plan["recommend"])

    def test_margin_blocks_low_ev(self):
        self.assertFalse(nl.workshop_plan(rme(), RCAT, margin=30)["recommend"])
        cheap = nl.workshop_plan(rme(), RCAT, values={"LAT-04": 1, "LAT-05": 2})
        self.assertEqual(cheap["ev"], 1.5)
        self.assertFalse(cheap["recommend"])                         # 1,5 < 1,2 + 2

    def test_no_next_rarity_values_means_no_send(self):
        plan = nl.workshop_plan(rme(), CATALOG)                      # catálogo sin book ni rarezas publicadas
        self.assertFalse(plan["recommend"])

    def test_committed_copies_reduce_pool_below_three(self):
        offers = [{"maker": "t15", "status": "open", "give": {"assets": [{"id": 5}]}}]
        plan = nl.workshop_plan(rme(), RCAT, offers)
        self.assertEqual(plan["spares"], [])                         # 2 comunes y 1 poco común: no hay tres iguales
        self.assertFalse(plan["recommend"])

    def test_report_shows_ev_line(self):
        txt = "\n".join(nl.report(ACTIVE, DEALERS, rme(), RCAT))
        self.assertIn("common → uncommon", txt)
        self.assertIn("RECOMENDADO", txt)


class Packs(unittest.TestCase):
    def test_list_and_dry_run_open(self):
        packs = nl.unopened_packs(me())
        self.assertEqual([p["id"] for p in packs], [90])

        class Api:
            opened = []

            def open_pack(self, i):
                self.opened.append(i)
                return {"cards": [{"ref": "LAV-01"}], "luck": 0}

        api = Api()
        self.assertEqual(nl.open_packs(api, packs, execute=False), [])
        self.assertEqual(api.opened, [])
        self.assertEqual(nl.open_packs(api, packs, execute=True), [90])


if __name__ == "__main__":
    unittest.main()
