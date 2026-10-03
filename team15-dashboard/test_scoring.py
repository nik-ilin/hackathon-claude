import statistics
import unittest

import scoring

LB = {"teams": [
    {"team": "t14", "rank": 1, "score": 29.82, "negotiating": 20.58, "market": 9.24, "level": 4, "pages_complete": 2, "album_filled": 34, "deals": 32, "luck": -46.3},
    {"team": "t05", "rank": 2, "score": 29.73, "negotiating": 22.23, "market": 7.5, "level": 4, "pages_complete": 2, "album_filled": 39, "deals": 47, "luck": -57.6},
    {"team": "t01", "rank": 3, "score": 29.09, "negotiating": 21.59, "market": 7.5, "level": 4, "pages_complete": 1, "album_filled": 31, "deals": 23, "luck": 13.7},
    {"team": "t12", "rank": 4, "score": 28.79, "negotiating": 16.49, "market": 12.3, "level": 3, "pages_complete": 1, "album_filled": 39, "deals": 44, "luck": 13.7},
    {"team": "t10", "rank": 5, "score": 28.36, "negotiating": 15.86, "market": 12.5, "level": 4, "pages_complete": 1, "album_filled": 32, "deals": 31, "luck": -46.3},
    {"team": "t03", "rank": 6, "score": 28.2, "negotiating": 23.45, "market": 4.75, "level": 4, "pages_complete": 1, "album_filled": 32, "deals": 25, "luck": -42.6},
    {"team": "t06", "rank": 7, "score": 27.93, "negotiating": 16.52, "market": 11.41, "level": 3, "pages_complete": 2, "album_filled": 35, "deals": 45, "luck": -46.3},
    {"team": "t18", "rank": 8, "score": 27.85, "negotiating": 20.35, "market": 7.5, "level": 3, "pages_complete": 2, "album_filled": 36, "deals": 31, "luck": -38.8},
    {"team": "t16", "rank": 9, "score": 26.92, "negotiating": 19.42, "market": 7.5, "level": 4, "pages_complete": 2, "album_filled": 27, "deals": 33, "luck": 13.7},
    {"team": "t17", "rank": 10, "score": 25.35, "negotiating": 16.75, "market": 8.6, "level": 3, "pages_complete": 2, "album_filled": 34, "deals": 24, "luck": -38.8},
    {"team": "t15", "rank": 11, "score": 24.91, "negotiating": 17.41, "market": 7.5, "level": 4, "pages_complete": 3, "album_filled": 42, "deals": 48, "luck": 6.2},
    {"team": "t13", "rank": 12, "score": 24.78, "negotiating": 18.71, "market": 6.08, "level": 3, "pages_complete": 2, "album_filled": 28, "deals": 60, "luck": -16.3},
    {"team": "t02", "rank": 13, "score": 23.42, "negotiating": 15.92, "market": 7.5, "level": 4, "pages_complete": 2, "album_filled": 31, "deals": 51, "luck": 13.7},
    {"team": "t04", "rank": 14, "score": 22.68, "negotiating": 15.18, "market": 7.5, "level": 4, "pages_complete": 2, "album_filled": 37, "deals": 56, "luck": 82.4},
    {"team": "t08", "rank": 15, "score": 20.75, "negotiating": 13.28, "market": 7.47, "level": 4, "pages_complete": 1, "album_filled": 30, "deals": 49, "luck": 7.1},
    {"team": "t09", "rank": 16, "score": 19.88, "negotiating": 10.53, "market": 9.35, "level": 4, "pages_complete": 1, "album_filled": 35, "deals": 36, "luck": -6.8},
    {"team": "t07", "rank": 17, "score": 18.58, "negotiating": 9.5, "market": 9.08, "level": 3, "pages_complete": 2, "album_filled": 35, "deals": 42, "luck": -42.6},
    {"team": "t11", "rank": 18, "score": 7.5, "negotiating": 0.0, "market": 7.5, "level": 3, "pages_complete": 0, "album_filled": 13, "deals": 0, "luck": 0.0},
]}

DEALERS = {
    "abuela": {"name": "Abuela Carmen", "level": 1, "open_to_all": True,
               "menu": {"buys": [{"rarity": "common"}, {"rarity": "uncommon"}]}},
    "pilar": {"name": "Doña Pilar", "level": 3, "open_to_all": True,
              "menu": {"buys": [{"rarity": "uncommon"}, {"rarity": "rare"}]}},
    "picaros": {"name": "Los Pícaros", "level": 4, "open_to_all": False,
                "menu": {"buys": [{"rarity": "common"}, {"rarity": "uncommon"}]}},
}


class Scoring(unittest.TestCase):
    def test_expone_el_desglose_que_el_panel_descartaba(self):
        out = scoring.scoring_block({"score": 24.91, "negotiating": 17.41, "market": 7.5,
                                     "ladder_points": 4.2, "duel_points": 0.0, "neg_points": -12}, LB)
        self.assertEqual(out["ladder_points"], 4.2)
        self.assertEqual(out["duel_points"], 0.0)
        self.assertEqual(out["missing_from_api"], [])

    def test_avisa_de_los_campos_que_faltan_sin_clave(self):
        out = scoring.scoring_block({"score": 24.91}, LB)
        self.assertEqual(out["missing_from_api"], ["ladder_points", "duel_points", "neg_points"])

    def test_separa_la_distancia_por_componente(self):
        out = scoring.scoring_block({"negotiating": 17.41, "market": 7.5}, LB)
        self.assertEqual(out["components"]["negotiating"]["best_team"], "t03")
        self.assertEqual(out["components"]["negotiating"]["gap_to_best"], round(23.45 - 17.41, 2))
        self.assertEqual(out["components"]["market"]["best_team"], "t10")
        self.assertEqual(out["components"]["market"]["gap_to_best"], 5.0)
        self.assertEqual(out["components"]["market"]["weight_in_total"], 30.0)
        self.assertEqual(out["components"]["market"]["teams_above"], 7)

    def test_ignora_equipos_inactivos_al_comparar(self):
        """t11 (score 7.50, negotiating 0.0) no ha jugado: incluirlo hundiría la mediana y la correlación."""
        out = scoring.scoring_block({"negotiating": 17.41, "market": 7.5}, LB)
        self.assertEqual(out["components"]["negotiating"]["median"], 16.75)
        with_dead = statistics.median([t["negotiating"] for t in LB["teams"]])
        self.assertNotEqual(out["components"]["negotiating"]["median"], with_dead)


class Ladder(unittest.TestCase):
    def test_cuenta_casillas_vacias_y_las_pesa_por_nivel(self):
        out = scoring.ladder_block(DEALERS, {"abuela": [5, 6, 7], "pilar": [25]}, unlocked={"picaros"})
        by = {l["dealer"]: l for l in out["levels"]}
        self.assertEqual(by["abuela"]["slots_empty"], 0)
        self.assertEqual(by["pilar"]["slots_empty"], 2)
        self.assertEqual(by["picaros"]["slots_empty"], 3)
        self.assertEqual(out["empty_slots_available"], 5)
        self.assertEqual(out["empty_slots_weighted_by_level"], 3 * 4 + 2 * 3)  # picaros pesa mas

    def test_ordena_por_nivel_descendente_porque_los_altos_pesan_mas(self):
        out = scoring.ladder_block(DEALERS, {}, unlocked={"picaros"})
        self.assertEqual([l["dealer"] for l in out["levels"]], ["picaros", "pilar", "abuela"])
        self.assertEqual(out["next_best_slot"], "picaros")

    def test_marca_los_tratos_de_mas_como_municion_gastada(self):
        out = scoring.ladder_block(DEALERS, {"abuela": [5, 6, 7, 8, 9]})
        by = {l["dealer"]: l for l in out["levels"]}
        self.assertEqual(by["abuela"]["slots_filled"], 3)
        self.assertEqual(by["abuela"]["deals_beyond_scoring"], 2)

    def test_un_vendedor_no_disponible_no_cuenta_como_hueco_accionable(self):
        out = scoring.ladder_block(DEALERS, {})
        self.assertEqual(out["empty_slots_available"], 6)  # picaros sigue cerrado
        self.assertEqual(out["next_best_slot"], "pilar")


class FeedHealth(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def store(self, ticks):
        import json
        from pathlib import Path
        p = Path(self.dir.name) / "feed.jsonl"
        p.write_text("".join(json.dumps({"tick": t, "type": "x"}) + "\n" for t in ticks))
        return p

    def test_detecta_el_almacen_parado(self):
        out = scoring.feed_health([self.store(range(640, 671))], now_tick=849)
        self.assertEqual(out["status"], "stale")
        self.assertEqual(out["ticks_behind"], 179)
        self.assertEqual(out["last_tick"], 670)
        self.assertIn("feed_stream.py --collect", out["impact"])

    def test_un_almacen_al_dia_es_fresco(self):
        out = scoring.feed_health([self.store(range(820, 850))], now_tick=849)
        self.assertEqual(out["status"], "fresh")
        self.assertNotIn("impact", out)

    def test_sin_fichero_no_finge_datos(self):
        out = scoring.feed_health(["/no/existe.jsonl"], now_tick=849)
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["events"], 0)
        self.assertFalse(out["files"][0]["exists"])

    def test_informa_del_tramo_cubierto_no_solo_del_final(self):
        out = scoring.feed_health([self.store(range(640, 671))], now_tick=849)
        self.assertEqual(out["files"][0]["span_ticks"], 30)


class Peers(unittest.TestCase):
    def test_mide_los_motores_y_los_ordena_por_fuerza(self):
        out = scoring.peers_block(LB)
        top = out["drivers"][0]
        self.assertEqual(top["variable"], "negotiating")
        self.assertGreater(top["r_with_score"], 0.5)

    def test_deals_sale_negativo_y_album_no_explica_nada(self):
        by = {d["variable"]: d["r_with_score"] for d in scoring.peers_block(LB)["drivers"]}
        self.assertLess(by["deals"], 0)
        self.assertLess(abs(by["album_filled"]), abs(by["negotiating"]))

    def test_calcula_la_eficiencia_por_trato_y_nuestra_posicion(self):
        out = scoring.peers_block(LB)
        self.assertEqual(out["ours"]["team"], "t15")
        self.assertEqual(out["ours"]["negotiating_per_deal"], round(17.41 / 48, 3))
        self.assertEqual(out["efficiency_ranking"][0]["team"], "t01")        # 0.939 con 23 tratos
        self.assertEqual(out["efficiency_ranking"][1]["team"], "t03")        # 0.938 con 25
        self.assertEqual(out["our_efficiency_position"], 11)                 # de 17

    def test_clasifica_los_arquetipos(self):
        arch = scoring.peers_block(LB)["archetypes"]
        self.assertEqual([t["team"] for t in arch["negotiation_specialists"]],
                         ["t03", "t05", "t01", "t14", "t18"])
        self.assertEqual([t["team"] for t in arch["market_makers"]],
                         ["t10", "t12", "t06", "t09", "t14"])
        # t14 es el unico en los dos: es lider por no estar en el suelo de ninguno
        self.assertEqual({"t14"}, {t["team"] for t in arch["negotiation_specialists"]}
                         & {t["team"] for t in arch["market_makers"]})

    def test_no_inventa_correlaciones_con_pocos_equipos(self):
        self.assertEqual(scoring.peers_block({"teams": LB["teams"][:2]})["status"], "insufficient_data")
        self.assertIsNone(scoring.pearson([1.0, 2.0], [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()
