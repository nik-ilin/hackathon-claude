import tempfile
import unittest
from pathlib import Path

import charts
import history

LB_820 = {"teams": [
    {"team": "t14", "rank": 1, "score": 29.82, "negotiating": 20.58, "market": 9.24, "deals": 32,
     "album_filled": 34, "pages_complete": 2},
    {"team": "t13", "rank": 12, "score": 24.78, "negotiating": 18.71, "market": 6.08, "deals": 60,
     "album_filled": 28, "pages_complete": 2},
    {"team": "t15", "rank": 11, "score": 24.91, "negotiating": 17.41, "market": 7.50, "deals": 48,
     "album_filled": 42, "pages_complete": 3},
    {"team": "t02", "rank": 13, "score": 23.42, "negotiating": 15.92, "market": 7.50, "deals": 51,
     "album_filled": 31, "pages_complete": 2},
]}


class Sampling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "d" / "score_history.jsonl"

    def test_completa_del_leaderboard_lo_que_no_trae_la_clave(self):
        row = history.sample(820, 8.0, {"score": 24.91}, LB_820, cash=74, collection_value=1366.5)
        self.assertEqual(row["negotiating"], 17.41)   # del leaderboard publico
        self.assertEqual(row["deals"], 48)
        self.assertEqual(row["cash"], 74)
        self.assertIsNone(row["ladder_points"])       # sigue necesitando la clave

    def test_lo_privado_manda_sobre_lo_publico(self):
        row = history.sample(820, 8.0, {"score": 24.91, "negotiating": 17.9, "ladder_points": 4.2}, LB_820)
        self.assertEqual(row["negotiating"], 17.9)
        self.assertEqual(row["ladder_points"], 4.2)

    def test_guarda_el_score_de_todos_para_la_carrera(self):
        row = history.sample(820, 8.0, {}, LB_820)
        self.assertEqual(row["rivals"]["t13"], 24.78)
        self.assertEqual(len(row["rivals"]), 4)

    def test_remuestrear_el_mismo_tick_sustituye_en_vez_de_duplicar(self):
        history.append(self.path, {"tick": 820, "score": 24.91})
        history.append(self.path, {"tick": 820, "score": 24.77})
        n = history.append(self.path, {"tick": 849, "score": 24.77})
        self.assertEqual(n, 2)
        rows = history.load(self.path)
        self.assertEqual([r["tick"] for r in rows], [820, 849])
        self.assertEqual(rows[0]["score"], 24.77)

    def test_crea_la_carpeta_y_tolera_lineas_corruptas(self):
        history.append(self.path, {"tick": 1, "score": 1.0})
        self.path.write_text(self.path.read_text() + "{no es json\n")
        self.assertEqual(len(history.load(self.path)), 1)
        self.assertEqual(history.load("/no/existe.jsonl"), [])


ROWS = [
    {"tick": 730, "score": 21.71, "negotiating": 14.21, "market": 7.5, "deals": 39, "rank": 14,
     "rivals": {"t13": 24.78, "t15": 21.71, "t02": 23.42}},
    {"tick": 805, "score": 23.54, "negotiating": 16.04, "market": 7.5, "deals": 45, "rank": 12,
     "rivals": {"t13": 24.78, "t15": 23.54, "t02": 23.42}},
    {"tick": 820, "score": 24.91, "negotiating": 17.41, "market": 7.5, "deals": 48, "rank": 11,
     "rivals": {"t13": 24.78, "t15": 24.91, "t02": 23.42}},
]


class Series(unittest.TestCase):
    def test_salta_los_huecos_en_vez_de_rellenarlos_con_ceros(self):
        rows = ROWS + [{"tick": 849, "negotiating": 17.4}]      # sin score
        self.assertEqual(len(history.series(rows, "score")), 3)
        self.assertEqual(history.series(rows, "score")[-1], (820, 24.91))

    def test_la_eficiencia_es_negociacion_por_trato(self):
        serie = history.efficiency_series(ROWS)
        self.assertEqual(serie[0], (730, round(14.21 / 39, 4)))
        self.assertEqual(serie[-1], (820, round(17.41 / 48, 4)))

    def test_no_divide_por_cero_tratos(self):
        self.assertEqual(history.efficiency_series([{"tick": 1, "negotiating": 0.0, "deals": 0}]), [])

    def test_atribuye_la_subida_a_los_tratos_del_intervalo(self):
        move = history.deltas(ROWS, "score", window=1)[0]
        self.assertEqual((move["from_tick"], move["to_tick"]), (805, 820))
        self.assertEqual(move["delta"], 1.37)
        self.assertEqual(move["delta_deals"], 3)
        self.assertEqual(move["points_per_deal"], round(1.37 / 3, 3))

    def test_no_reporta_lo_que_no_cambio(self):
        move = history.deltas(ROWS, "score", window=1)[0]
        self.assertNotIn("delta_market", move)                   # 7.5 clavado


class Race(unittest.TestCase):
    def test_los_vecinos_salen_del_estado_final_no_del_inicial(self):
        # al final t15 (24.91) ya ha adelantado a t13 (24.78): arriba no queda nadie de los guardados,
        # asi que el vecino relevante es el de abajo. Elegidos por el estado final, no por el inicial.
        race = history.rank_race(ROWS, "t15")
        self.assertEqual(race["neighbours"], ["t13"])
        self.assertEqual(race["position"], 1)
        self.assertEqual(len(race["series"]["t15"]), 3)
        self.assertEqual(race["series"]["t15"][0], (730, 21.71))
        # con around=2 entra tambien el siguiente
        self.assertEqual(history.rank_race(ROWS, "t15", around=2)["neighbours"], ["t13", "t02"])

    def test_sin_historia_no_inventa_carrera(self):
        self.assertEqual(history.rank_race([], "t15")["series"], {})
        self.assertEqual(history.rank_race(ROWS, "t99")["neighbours"], [])


class Summary(unittest.TestCase):
    def test_resume_de_donde_venimos_y_el_mejor_trato(self):
        out = history.summary(ROWS)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["score_change"], 3.2)
        self.assertEqual((out["rank_from"], out["rank_to"]), (14, 11))
        self.assertEqual(out["span_ticks"], 90)
        # dos intervalos: 730->805 da 1.83 en 6 tratos (0.305) y 805->820 da 1.37 en 3 (0.457).
        # El mejor es el segundo: menos tratos y mas puntos, que es exactamente la tesis.
        self.assertEqual(out["best_deal"]["points_per_deal"], round(1.37 / 3, 3))
        self.assertEqual(out["best_deal"]["to_tick"], 820)

    def test_una_sola_muestra_no_es_historia(self):
        self.assertEqual(history.summary(ROWS[:1])["status"], "insufficient_history")


class Charts(unittest.TestCase):
    def test_dibuja_una_linea_por_serie_con_su_ultimo_valor(self):
        svg = charts.line_chart({"negociación": history.series(ROWS, "negotiating"),
                                 "mercado": history.series(ROWS, "market")},
                                lo=0, hi=30, title="Componentes")
        self.assertEqual(svg.count("<path"), 2)
        self.assertIn("17.41", svg)
        self.assertIn("7.50", svg)

    def test_un_limite_explicito_no_se_acolcha(self):
        """Con lo=0, hi=30 el eje dibujaba de -2.40 a 32.40: acolchar un limite dado
        rompe el sentido de darlo, y poner los componentes sobre su escala real es el punto."""
        svg = charts.line_chart({"mercado": history.series(ROWS, "market")}, lo=0, hi=30, title="x")
        self.assertIn("0.00", svg)
        self.assertIn("30.00", svg)
        self.assertNotIn("-2.40", svg)
        self.assertNotIn("32.40", svg)

    def test_sin_limites_si_deja_aire_a_los_lados(self):
        svg = charts.line_chart({"score": history.series(ROWS, "score")}, title="x")
        self.assertNotIn("21.71", svg)   # el minimo real no toca el borde

    def test_la_referencia_se_dibuja_y_se_etiqueta(self):
        svg = charts.line_chart({"mercado": history.series(ROWS, "market")},
                                baseline=7.5, baseline_label="suelo", title="x")
        self.assertIn('class="baseline"', svg)
        self.assertIn("suelo", svg)

    def test_una_serie_vacia_lo_dice_en_vez_de_dibujar_una_linea_plana(self):
        svg = charts.line_chart({"score": []}, title="Score")
        self.assertIn("Sin historia", svg)
        self.assertNotIn("<path", svg)

    def test_escapa_las_etiquetas(self):
        svg = charts.line_chart({"<b>x</b>": [(1, 1.0), (2, 2.0)]}, title="<i>t</i>")
        self.assertNotIn("<b>", svg)
        self.assertNotIn("<i>", svg)

    def test_el_puesto_se_dibuja_invertido_porque_1_esta_arriba(self):
        up = charts.line_chart({"puesto": [(1, 14.0), (2, 11.0)]}, invert=True, title="x")
        down = charts.line_chart({"puesto": [(1, 14.0), (2, 11.0)]}, invert=False, title="x")
        self.assertNotEqual(up, down)


if __name__ == "__main__":
    unittest.main()
