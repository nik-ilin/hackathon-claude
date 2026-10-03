"""Tests offline de flagger.py: números en palabras, mentiras sintéticas, faroles y mensajes reales parafraseados."""
import os
import tempfile
import unittest

import flagger as fl

CATALOG = {"sets": [{"id": "LAV", "cards": [
    {"id": "LAV-06", "name": "La Tabacalera", "rarity": "common", "page": True},
    {"id": "LAV-09", "name": "Cine Doré", "rarity": "uncommon", "page": True}]},
    {"id": "LAT", "cards": [
        {"id": "LAT-05", "name": "El Organillero", "rarity": "common", "page": True},
        {"id": "LAT-06", "name": "La Chulapa", "rarity": "common", "page": True},
        {"id": "LAT-08", "name": "Las Vistillas", "rarity": "common", "page": True}]}]}


def sells_card(ref, price, final=False, oid=1):
    return {"id": oid, "to": "t15", "status": "open", "final": final,
            "give": {"cash": 0, "assets": [], "types": [f"card:{ref}"]}, "want": {"cash": price, "assets": [], "types": []}}


def sells_pack(pack, price, oid=1):
    return {"id": oid, "to": "t15", "status": "open", "final": False,
            "give": {"cash": 0, "assets": [], "types": [f"pack:{pack}"]}, "want": {"cash": price, "assets": [], "types": []}}


def buys_card(ref, price, oid=1):
    return {"id": oid, "to": "t15", "status": "open", "final": False, "give": {"cash": price, "assets": [], "types": []},
            "want": {"cash": 0, "assets": [{"id": 9, "kind": "card", "ref": ref}], "types": []}}


def strong(text, offer):
    return [f.kind for f in fl.compare(text, offer, CATALOG) if f.strong]


class Numbers(unittest.TestCase):
    def test_words_and_digits(self):
        self.assertEqual(fl.numbers("Ninety-five, then."), [95])
        self.assertEqual(fl.numbers("treinta y dos primas"), [32])
        self.assertEqual(fl.numbers("veintiséis P y dieciséis"), [26, 16])
        self.assertEqual(fl.numbers("a hundred and twenty"), [120])
        self.assertEqual(fl.numbers("ciento cinco"), [105])
        self.assertEqual(fl.numbers("33 P"), [33])

    def test_refs_and_serials_are_not_prices(self):
        self.assertEqual(fl.numbers("LAV-03 #7/30"), [])

    def test_tagging(self):
        ns = fl.parse_numbers("forty years at this table, 12 P for you")
        self.assertEqual([(n.value, n.tagged, n.unit) for n in ns], [(40, False, "years"), (12, True, None)])
        self.assertTrue(fl.parse_numbers("por quince")[0].tagged)


class Honest(unittest.TestCase):
    """Mensajes que NO mienten (incluidos los reales parafraseados) → sin flag fuerte."""

    def test_price_matches_words(self):
        self.assertEqual(strong("You move, I move. 94 P.", sells_card("LAV-09", 94)), [])
        self.assertEqual(strong("Trece primas, Teatro. Sigue siendo mi número.", sells_card("LAV-09", 13)), [])

    def test_history_numbers_with_the_price(self):
        self.assertEqual(strong("Nine to eighteen. Nine moves. Ninety-five, then.", sells_card("LAV-09", 95)), [])
        self.assertEqual(strong("Thirty-two? No, señor. I shall stretch to 23 P.", buys_card("LAV-06", 23)), [])

    def test_bluff_is_never_a_flag(self):
        o = sells_card("LAV-09", 31)
        txt = "Thirty-one. That's the last number I say. Refuse it and I pack up."
        self.assertEqual(strong(txt, o), [])
        self.assertTrue(fl.is_bluff(txt, o))
        self.assertFalse(fl.is_bluff(txt, sells_card("LAV-09", 31, final=True)))

    def test_abuela_gift_card_is_not_a_lie(self):
        txt = "Let's meet in the middle, cariño: 5 P. And take this, a little present from me: El Organillero."
        self.assertEqual(strong(txt, sells_card("LAV-06", 5)), [])
        txt = "Dieciséis es poquito… te la dejo en 26 P, y te regalo esta de Las Vistillas."
        self.assertEqual(strong(txt, sells_card("LAV-09", 26)), [])

    def test_golden_chulapa_lore(self):
        txt = "Ay, la chulapa dorada... solo se imprimió una. Y la Tabacalera te la dejo en 10 P."
        self.assertEqual(strong(txt, sells_card("LAV-06", 10)), [])

    def test_chato_silver_pack_describing_a_card(self):
        self.assertEqual(strong("La Tabacalera, thirty-three. Silver pack, good one.", sells_card("LAV-06", 33)), [])

    def test_sentence_break_is_not_quantity(self):
        self.assertEqual(strong("Two steps from you. One from me. 96. Cards move fine today.", sells_card("LAV-09", 96)),
                         [])

    def test_untagged_mismatch_is_weak_only(self):
        fs = fl.compare("Bajaste cinco. Bien.", sells_card("LAV-09", 13), CATALOG)
        self.assertTrue(fs and not any(f.strong for f in fs))

    def test_no_offer_no_finding(self):
        self.assertEqual(fl.compare("20 P, amigo", None, CATALOG), [])


class Lies(unittest.TestCase):
    """Mensajes sintéticos de un Pícaro que miente → flag fuerte."""

    def test_price_in_digits(self):
        self.assertEqual(strong("Deal at your 15 P, amigo. Quick.", sells_card("LAV-09", 25)), ["price"])

    def test_price_in_words(self):
        self.assertEqual(strong("Doce primas y es tuya, sin preguntas.", sells_card("LAV-09", 30)), ["price"])
        self.assertEqual(strong("For twenty, it's yours.", sells_card("LAV-09", 40)), ["price"])

    def test_small_difference_is_not_enough(self):
        self.assertEqual(strong("Doce primas y es tuya.", sells_card("LAV-09", 14)), [])     # 2 P < 3 P
        self.assertEqual(strong("88 P for it.", sells_card("LAV-09", 95)), [])               # 7 P < 10 %

    def test_other_card(self):
        self.assertEqual(strong("Cine Doré for 20 P, quick deal.", sells_card("LAV-06", 20)), ["card"])

    def test_other_pack(self):
        self.assertEqual(strong("A gold pack for 150 P. Few questions.", sells_pack("sobre_plata", 150)), ["pack"])

    def test_direction(self):
        self.assertEqual(strong("I pay you 20 P for La Tabacalera.", sells_card("LAV-06", 20)), ["direction"])

    def test_quantity(self):
        o = sells_card("LAV-09", 30)
        self.assertEqual(strong("Three cards for 30 P, amigo.", o), ["quantity"])

    def test_bluff_plus_lie_still_flags_the_lie(self):
        self.assertEqual(strong("My final price: 10 P.", sells_card("LAV-09", 30)), ["price"])

    def test_offer_matches_words_and_safe_to_accept(self):
        o = sells_card("LAV-09", 25, oid=77)
        self.assertFalse(fl.offer_matches_words("Deal at 15 P.", o, CATALOG))
        self.assertTrue(fl.offer_matches_words("Twenty-five P, final.", o, CATALOG))
        th = {"id": 1, "with": "picaros", "messages": [{"id": 5, "sender": "picaros", "text": "15 P, deal.", "offer": o}]}
        ok, why = fl.safe_to_accept(th, 77, CATALOG)
        self.assertFalse(ok)
        self.assertIn("15", why)
        self.assertTrue(fl.safe_to_accept(th, 999, CATALOG)[0])


class Candidates(unittest.TestCase):
    def threads(self):
        return [
            {"id": 1, "with": "picaros", "messages": [
                {"id": 10, "sender": "t15", "text": "15?", "offer": None},
                {"id": 11, "sender": "picaros", "tick": 3, "text": "15 P, done.", "offer": sells_card("LAV-09", 25)},
                {"id": 12, "sender": "picaros", "tick": 4, "text": "25 P.", "offer": sells_card("LAV-09", 25)}]},
            {"id": 2, "with": "t04", "messages": [
                {"id": 20, "sender": "t04", "text": "5 P", "offer": sells_card("LAV-09", 50)}]},
            {"id": 3, "with": "picaros", "messages": [
                {"id": 30, "sender": "picaros", "text": "5 P", "offer": dict(sells_card("LAV-09", 50), to="t07")}]},
        ]

    def test_only_dealer_threads_and_our_offers(self):
        c = fl.candidates(self.threads(), "t15", CATALOG)
        self.assertEqual([x["message_id"] for x in c], [11])

    def test_never_twice(self):
        self.assertEqual(fl.candidates(self.threads(), "t15", CATALOG, already=[11]), [])

    def test_run_flags_dry_and_execute_once(self):
        class Api:
            calls = []

            def flag(self, mid, reason):
                self.calls.append(mid)
                return {"ok": True}

        api = Api()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            state = fl.load_state(path)
            cands = fl.candidates(self.threads(), "t15", CATALOG)
            fl.run_flags(api, cands, state, path, execute=False)
            self.assertEqual(api.calls, [])
            fl.run_flags(api, cands, state, path, execute=True)
            fl.run_flags(api, cands, fl.load_state(path), path, execute=True)
            self.assertEqual(api.calls, [11])
            self.assertEqual(fl.load_state(path)["flagged"]["11"]["status"], "sent")

    def test_failed_flag_is_not_retried(self):
        class Api:
            n = 0

            def flag(self, mid, reason):
                Api.n += 1
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            cands = fl.candidates(self.threads(), "t15", CATALOG)
            fl.run_flags(Api(), cands, {}, path, execute=True)
            fl.run_flags(Api(), cands, fl.load_state(path), path, execute=True)
            self.assertEqual(Api.n, 1)


if __name__ == "__main__":
    unittest.main()
