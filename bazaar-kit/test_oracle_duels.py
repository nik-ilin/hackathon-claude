"""Pruebas de la política de duelos. Sin red, offline, deterministas.

Lo que se comprueba es lo que puede costar puntos a las 11:30:
no contestar (el peor resultado), aceptar lo aceptable, contraofertar una
sola vez cuando hay margen, la aritmética de la merma, y el intercambio
día-por-precio en los dos sentidos.
"""

import unittest

import oracle_duels as D


SOLO_PRECIO = D.SESSIONS["Duels I"]
DOS_ISSUES = D.SESSIONS["Duels II"]


# ------------------------------------------------------------------ merma

class TestMerma(unittest.TestCase):
    def test_factor_por_ronda(self):
        self.assertAlmostEqual(D.pie_factor(0.06, 0), 1.0)
        self.assertAlmostEqual(D.pie_factor(0.06, 1), 0.94)
        self.assertAlmostEqual(D.pie_factor(0.06, 2), 0.8836)
        self.assertAlmostEqual(D.pie_factor(0.06, 3), 0.830584)

    def test_tres_rondas_queman_mas_del_15_por_ciento(self):
        """Regatear tres veces con decay 0.06 ya cuesta ~17 % del pastel."""
        self.assertLess(D.pie_factor(0.06, 3), 0.84)
        self.assertLess(D.pie_factor(0.08, 3), 0.78)   # Duels II
        self.assertLess(D.pie_factor(0.10, 3), 0.73)   # Duels III

    def test_coste_relativo_de_una_ronda(self):
        self.assertAlmostEqual(D.round_cost_fraction(0.06), 0.06 / 0.94)
        self.assertAlmostEqual(D.round_cost_fraction(0.06), 0.063829, places=5)
        self.assertAlmostEqual(D.round_cost_fraction(0.08), 0.086956, places=5)
        self.assertAlmostEqual(D.round_cost_fraction(0.10), 0.111111, places=5)

    def test_umbral_de_concesion_minima(self):
        """Con 40 de excedente y decay 0.06, pedir menos de ~2.6 pierde dinero."""
        self.assertAlmostEqual(D.min_worthwhile_gain(40, 0.06), 40 * 0.06 / 0.94)
        self.assertGreater(D.min_worthwhile_gain(40, 0.06), 2.5)
        # Ganar justo el umbral deja indiferente: comprobamos la identidad.
        u, d = 40.0, 0.06
        self.assertAlmostEqual((u + D.min_worthwhile_gain(u, d)) * (1 - d), u)

    def test_tabla_de_merma(self):
        tabla = D.decay_table(0.06, pie=100.0, rounds=3)
        self.assertEqual([r for r, _, _ in tabla], [0, 1, 2, 3])
        self.assertAlmostEqual(tabla[1][2], 94.0)
        self.assertAlmostEqual(tabla[3][2], 83.0584)

    def test_decay_invalido(self):
        with self.assertRaises(ValueError):
            D.round_cost_fraction(1.0)
        with self.assertRaises(ValueError):
            D.pie_factor(0.06, -1)


# ------------------------------------------------------------- excedentes

class TestExcedentes(unittest.TestCase):
    def test_vendedor_y_comprador(self):
        self.assertEqual(D.surplus("seller", 40, 60), 20)
        self.assertEqual(D.surplus("buyer", 100, 60), 40)
        self.assertEqual(D.surplus("seller", 40, 30), -10)   # fuera de límite

    def test_pastel_y_cuota(self):
        # Vendedor con coste 40, comprador valora 100: pastel 60.
        self.assertEqual(D.pie_size("seller", 40, 100), 60)
        self.assertEqual(D.pie_size("buyer", 100, 40), 60)
        self.assertAlmostEqual(D.share_of_pie("seller", 40, 100, 70), 0.5)
        self.assertAlmostEqual(D.share_of_pie("buyer", 100, 40, 70), 0.5)

    def test_pastel_negativo_da_cuota_cero(self):
        self.assertEqual(D.pie_size("seller", 100, 40), -60)
        self.assertEqual(D.share_of_pie("seller", 100, 40, 70), 0.0)

    def test_inversa(self):
        self.assertEqual(D.price_for_surplus("seller", 40, 20), 60)
        self.assertEqual(D.price_for_surplus("buyer", 100, 40), 60)

    def test_role_desconocido(self):
        with self.assertRaises(ValueError):
            D.surplus("broker", 10, 10)


# --------------------------------------- el silencio es el peor resultado

class TestContestarSiempre(unittest.TestCase):
    """Un duelo sin contestar da 0 a las dos partes: es el suelo absoluto."""

    def test_valor_del_silencio_es_cero(self):
        self.assertEqual(D.NO_ANSWER_VALUE, 0.0)

    def test_toda_decision_contesta(self):
        casos = [
            # sin oferta del rival
            D.DuelView(1, "seller", 40.0),
            # oferta buenísima
            D.DuelView(2, "seller", 40.0, rival_price=95.0),
            # oferta en el filo
            D.DuelView(3, "seller", 40.0, rival_price=41.0),
            # oferta fuera de límite
            D.DuelView(4, "seller", 40.0, rival_price=20.0),
            # comprador con oferta imposible
            D.DuelView(5, "buyer", 100.0, rival_price=180.0),
            # sin zona de acuerdo según nuestra creencia
            D.DuelView(6, "seller", 150.0, rival_price=None),
        ]
        for duel in casos:
            with self.subTest(duel.duel_id):
                dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
                self.assertTrue(dec.answers, "la política nunca puede callar")
                self.assertIn(dec.action, ("accept", "counter", "open"))
                self.assertIsNotNone(dec.price)
                self.assertGreaterEqual(
                    max(dec.ev_accept, dec.ev_counter), D.NO_ANSWER_VALUE,
                    "ninguna rama puede valer menos que no contestar")

    def test_cualquier_trato_dentro_de_limite_bate_al_silencio(self):
        duel = D.DuelView(7, "seller", 40.0, rival_price=41.0)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        # Aunque sólo gane 1 prima, su EV de aceptar es > 0 = silencio.
        self.assertGreater(dec.ev_accept, D.NO_ANSWER_VALUE)

    def test_nunca_se_acepta_fuera_de_limite(self):
        """Un trato fuera del límite resta puntos: peor que el cero del silencio."""
        duel = D.DuelView(8, "seller", 40.0, rival_price=25.0)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "counter")
        self.assertGreaterEqual(D.surplus("seller", 40.0, dec.price), 0.0)


# ---------------------------------------------------------------- apertura

class TestApertura(unittest.TestCase):
    def test_es_tomable_no_un_ancla(self):
        """Debe dejarles una tajada real: el consejo oficial y el cero mandan."""
        duel = D.DuelView(1, "seller", 40.0)
        dec = D.opening_offer(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "open")
        cuota_rival = D.rival_surplus("seller", 100.0, dec.price) / 60.0
        self.assertGreater(cuota_rival, 0.15, "no puede ser un ancla agresiva")
        self.assertLess(cuota_rival, 0.60, "tampoco regalamos el pastel")
        self.assertGreater(D.surplus("seller", 40.0, dec.price), 0.0)

    def test_dentro_de_la_banda(self):
        duel = D.DuelView(1, "buyer", 100.0)
        dec = D.opening_offer(duel, D.Beliefs(rival_limit=40.0), SOLO_PRECIO)
        self.assertGreaterEqual(dec.price, 40)
        self.assertLessEqual(dec.price, 100)

    def test_rival_duro_obliga_a_abrir_mas_generoso(self):
        duel = D.DuelView(1, "seller", 40.0)
        blando = D.opening_offer(duel, D.Beliefs(100.0, firmness=0.6), SOLO_PRECIO)
        duro = D.opening_offer(duel, D.Beliefs(100.0, firmness=2.0), SOLO_PRECIO)
        self.assertLess(duro.price, blando.price)

    def test_sin_zona_de_acuerdo_abre_en_el_limite(self):
        duel = D.DuelView(1, "seller", 120.0)
        dec = D.opening_offer(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.price, 120)
        self.assertGreaterEqual(D.surplus("seller", 120.0, dec.price), 0.0)

    def test_el_texto_invita_a_aceptar(self):
        dec = D.opening_offer(D.DuelView(1, "seller", 40.0),
                              D.Beliefs(100.0), SOLO_PRECIO)
        self.assertIn("cero", dec.text)
        self.assertTrue(dec.text.strip())

    def test_sesion_de_dos_issues_siempre_manda_days(self):
        """Un mensaje con precio y sin days se rechaza (`missing_days`)."""
        duel = D.DuelView(1, "seller", 40.0, issues=("price", "days"), days_weight=1.0)
        dec = D.opening_offer(duel, D.Beliefs(100.0, rival_days_weight=-4.0), DOS_ISSUES)
        self.assertIsNotNone(dec.days)
        self.assertIn("days", dec.payload())
        self.assertTrue(D.DAYS_MIN <= dec.days <= D.DAYS_MAX)

    def test_sesion_de_un_issue_no_manda_days(self):
        dec = D.opening_offer(D.DuelView(1, "seller", 40.0), D.Beliefs(100.0), SOLO_PRECIO)
        self.assertIsNone(dec.days)
        self.assertNotIn("days", dec.payload())


# ------------------------------------------------------- regla de respuesta

class TestRespuesta(unittest.TestCase):
    def test_oferta_ya_aceptable_en_la_primera_ronda_se_acepta(self):
        """Nos dan 50 de 60 de pastel: arrancar más no cubre la merma."""
        duel = D.DuelView(1, "seller", 40.0, rival_price=90.0, rounds_used=1)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "accept")
        self.assertEqual(dec.price, 90)
        self.assertGreaterEqual(dec.ev_accept, dec.ev_counter)

    def test_oferta_mala_con_margen_genera_una_contra(self):
        """Nos dan 5 de 60: hay muchísimo más que el 6.4 % que cuesta la ronda."""
        duel = D.DuelView(2, "seller", 40.0, rival_price=45.0, rounds_used=0)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "counter")
        self.assertGreater(dec.price, 45)
        self.assertLess(dec.price, 100, "la contra sigue dentro del pastel")
        self.assertGreater(dec.detail["achievable_gain"], dec.detail["needed_gain"])

    def test_solo_una_contra(self):
        """Gastada la contra, se cierra: la segunda ronda se come la mejora."""
        duel = D.DuelView(3, "seller", 40.0, rival_price=50.0, rounds_used=1)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO, max_counters=1)
        self.assertEqual(dec.action, "accept")
        self.assertIn("contras", dec.reason)

    def test_pocos_ticks_fuerza_aceptar(self):
        duel = D.DuelView(4, "seller", 40.0, rival_price=45.0, rounds_used=0, ticks_left=1)
        dec = D.respond(duel, D.Beliefs(rival_limit=100.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "accept")
        self.assertIn("ticks", dec.reason)

    def test_merma_mas_dura_empuja_a_aceptar_antes(self):
        """El mismo duelo con decay 0.10 se cierra antes que con 0.06."""
        duel = D.DuelView(5, "seller", 40.0, rival_price=78.0, rounds_used=1)
        suave = D.respond(duel, D.Beliefs(rival_limit=100.0),
                          D.SessionParams("x", 1, 16, 0.02, 3))
        dura = D.respond(duel, D.Beliefs(rival_limit=100.0),
                         D.SessionParams("x", 1, 16, 0.30, 3))
        self.assertGreater(dura.detail["needed_gain"], suave.detail["needed_gain"])
        self.assertEqual(dura.action, "accept")

    def test_decay_aplicado_en_varias_rondas(self):
        """El EV de aceptar la misma oferta cae ronda a ronda por la merma."""
        evs = []
        for r in range(4):
            duel = D.DuelView(6, "seller", 40.0, rival_price=90.0, rounds_used=r)
            evs.append(D.respond(duel, D.Beliefs(100.0), DOS_ISSUES).ev_accept)
        self.assertEqual(evs, sorted(evs, reverse=True))
        self.assertAlmostEqual(evs[1] / evs[0], 1 - DOS_ISSUES.decay, places=6)
        self.assertAlmostEqual(evs[3] / evs[0], (1 - DOS_ISSUES.decay) ** 3, places=6)

    def test_comprador_simetrico(self):
        duel = D.DuelView(7, "buyer", 100.0, rival_price=55.0, rounds_used=1)
        dec = D.respond(duel, D.Beliefs(rival_limit=40.0), SOLO_PRECIO)
        self.assertEqual(dec.action, "accept")
        duel2 = D.DuelView(8, "buyer", 100.0, rival_price=98.0, rounds_used=0)
        dec2 = D.respond(duel2, D.Beliefs(rival_limit=40.0), SOLO_PRECIO)
        self.assertEqual(dec2.action, "counter")
        self.assertLess(dec2.price, 98, "un comprador contraoferta más bajo")


# ------------------------------------------------------- día por precio

class TestDiaPorPrecio(unittest.TestCase):
    def test_utilidad_lineal_y_dia_preferido(self):
        self.assertEqual(D.days_utility(3.0, 4), 12.0)
        self.assertEqual(D.preferred_day(3.0), 10)
        self.assertEqual(D.preferred_day(-3.0), 0)

    def test_dia_eficiente_va_a_quien_mas_le_importa(self):
        # A nosotros nos da casi igual (1), a ellos les urge mucho (-9): día 0.
        self.assertEqual(D.efficient_day(1.0, -9.0), 0)
        # Al contrario: a nosotros nos urge (-9), a ellos casi nada (1): día 0
        # también, porque el 0 es NUESTRO ideal.
        self.assertEqual(D.efficient_day(-9.0, 1.0), 0)
        # Los dos quieren tarde: día 10.
        self.assertEqual(D.efficient_day(2.0, 5.0), 10)

    def test_cedemos_tiempo_y_cobramos_precio(self):
        """Nos da casi igual el día (w=+1), a ellos les urge (w=-8): les damos el 0."""
        t = D.days_trade(our_weight=1.0, their_weight=-8.0)
        self.assertEqual(t["day"], 0)
        self.assertEqual(t["our_ideal_day"], 10)
        self.assertTrue(t["we_concede_time"])
        self.assertAlmostEqual(t["our_loss"], 10.0)      # perdemos 1*10
        self.assertAlmostEqual(t["their_gain"], 80.0)    # ganan 8*10 respecto al día 10
        self.assertAlmostEqual(t["created"], 70.0)       # pastel nuevo: NO es suma cero
        self.assertAlmostEqual(t["compensation"], 10.0)  # mínimo a cobrar

    def test_cobramos_tiempo_y_pagamos_precio(self):
        """El sentido contrario: nos urge a NOSOTROS (w=-8) y a ellos no (w=+1)."""
        t = D.days_trade(our_weight=-8.0, their_weight=1.0)
        self.assertEqual(t["day"], 0)                    # nuestro ideal gana
        self.assertFalse(t["we_concede_time"])
        self.assertAlmostEqual(t["our_loss"], 0.0)
        self.assertAlmostEqual(t["created"], 0.0)        # no cedemos nada
        self.assertAlmostEqual(t["compensation"], 0.0)

    def test_el_pastel_nunca_se_destruye_al_elegir_el_dia(self):
        """`created >= 0` siempre: el día eficiente maximiza la suma."""
        for w_us in (-9.0, -3.0, -0.5, 0.0, 0.5, 3.0, 9.0):
            for w_them in (-9.0, -3.0, 0.0, 3.0, 9.0):
                with self.subTest(w_us=w_us, w_them=w_them):
                    self.assertGreaterEqual(
                        D.days_trade(w_us, w_them)["created"], -1e-9)

    def test_el_pastel_crece_con_dos_issues(self):
        """Duels II: 'the pie grows'. El pastel total > el de precio."""
        plan = D.days_trade(1.0, -8.0)
        self.assertGreater(D.total_pie(60.0, plan), 60.0)
        self.assertEqual(D.total_pie(60.0, None), 60.0)

    def test_penalizacion_del_dia_nunca_es_positiva(self):
        for w in (-5.0, 0.0, 5.0):
            for day in range(11):
                self.assertLessEqual(D.day_penalty(w, day), 0.0)
        self.assertEqual(D.day_penalty(5.0, 10), 0.0)    # nuestro ideal
        self.assertEqual(D.day_penalty(5.0, 0), -50.0)
        self.assertEqual(D.day_penalty(1.0, None), 0.0)

    def test_el_precio_se_mueve_en_el_sentido_correcto(self):
        self.assertEqual(D.price_shift("seller", 10.0), 10.0)   # vendedor cobra más
        self.assertEqual(D.price_shift("buyer", 10.0), -10.0)   # comprador paga menos

    def test_contra_que_cede_dia_pide_mas_precio_que_sin_dias(self):
        """Mismo duelo: si cedemos el día, el precio sube para compensarlo."""
        base = D.DuelView(1, "seller", 40.0, rival_price=42.0, rival_days=10,
                          issues=("price", "days"), days_weight=1.0)
        ceder = D.respond(base, D.Beliefs(100.0, rival_days_weight=-8.0), DOS_ISSUES)
        self.assertEqual(ceder.action, "counter")
        self.assertEqual(ceder.days, 0, "el día va a quien más le urge")
        sin_ceder = D.respond(
            D.DuelView(2, "seller", 40.0, rival_price=42.0, rival_days=10,
                       issues=("price", "days"), days_weight=0.0),
            D.Beliefs(100.0, rival_days_weight=0.0), DOS_ISSUES)
        self.assertGreater(ceder.price, sin_ceder.price,
                           "ceder tiempo se cobra en precio, no se regala")

    def test_comprador_que_cede_dia_baja_el_precio_que_ofrece(self):
        duel = D.DuelView(3, "buyer", 100.0, rival_price=98.0, rival_days=0,
                          issues=("price", "days"), days_weight=1.0)
        dec = D.respond(duel, D.Beliefs(40.0, rival_days_weight=-8.0), DOS_ISSUES)
        self.assertEqual(dec.action, "counter")
        self.assertEqual(dec.days, 0)
        # Compensarnos como comprador = ofrecer menos dinero.
        self.assertLess(dec.price, 98)

    def test_dias_siempre_en_rango(self):
        for w_us in (-9.0, -1.0, 0.0, 1.0, 9.0):
            for w_them in (-9.0, 0.0, 9.0):
                with self.subTest(w_us=w_us, w_them=w_them):
                    t = D.days_trade(w_us, w_them)
                    self.assertTrue(D.DAYS_MIN <= t["day"] <= D.DAYS_MAX)


class TestOfertaSiempreTomable(unittest.TestCase):
    """Pedir más de lo que el rival aguanta convierte un pastel grande en un 0."""

    def test_la_contra_deja_algo_al_rival(self):
        duel = D.DuelView(1, "seller", 40.0, rival_price=42.0, rival_days=10,
                          issues=("price", "days"), days_weight=1.0)
        b = D.Beliefs(100.0, rival_days_weight=-8.0)
        dec = D.respond(duel, b, DOS_ISSUES)
        self.assertGreater(D.rival_surplus("seller", 100.0, dec.price), 0.0,
                           "una oferta que el rival no puede tomar vale cero")

    def test_nunca_se_contraoferta_fuera_de_nuestro_limite(self):
        for role, limit, rival in (("seller", 40.0, 100.0), ("buyer", 100.0, 40.0)):
            for w_us, w_them in ((9.0, -9.0), (-9.0, 9.0), (0.5, -9.0)):
                duel = D.DuelView(1, role, limit,
                                  rival_price=limit, rival_days=5,
                                  issues=("price", "days"), days_weight=w_us)
                dec = D.respond(duel, D.Beliefs(rival, rival_days_weight=w_them),
                                DOS_ISSUES)
                with self.subTest(role=role, w_us=w_us, w_them=w_them):
                    self.assertGreaterEqual(
                        D.surplus(role, limit, dec.price), -1e-9,
                        "jamás un precio fuera de nuestro límite")

    def test_dias_de_la_oferta_siempre_en_rango(self):
        for w_us in (-9.0, 0.0, 9.0):
            duel = D.DuelView(1, "seller", 40.0, rival_price=50.0, rival_days=3,
                              issues=("price", "days"), days_weight=w_us)
            dec = D.respond(duel, D.Beliefs(100.0, rival_days_weight=-2.0), DOS_ISSUES)
            if dec.days is not None:
                self.assertTrue(D.DAYS_MIN <= dec.days <= D.DAYS_MAX)


class TestPrioridad(unittest.TestCase):
    """Con 6 duelos a la vez el cuello de botella es la atención, no la política."""

    def test_un_duelo_sin_contestar_va_primero(self):
        virgen = D.DuelView(1, "seller", 40.0, ticks_left=10)
        hablado = D.DuelView(2, "seller", 40.0, rival_price=70.0,
                             rounds_used=1, ticks_left=10)
        b = {1: D.Beliefs(100.0), 2: D.Beliefs(100.0)}
        self.assertGreater(D.urgency(virgen, b[1], DOS_ISSUES),
                           D.urgency(hablado, b[2], DOS_ISSUES))

    def test_lo_que_expira_pesa_mas(self):
        b = D.Beliefs(100.0)
        pronto = D.DuelView(1, "seller", 40.0, rival_price=70.0, rounds_used=1, ticks_left=1)
        tarde = D.DuelView(2, "seller", 40.0, rival_price=70.0, rounds_used=1, ticks_left=12)
        self.assertGreater(D.urgency(pronto, b, DOS_ISSUES), D.urgency(tarde, b, DOS_ISSUES))

    def test_pastel_grande_antes_que_pequeno(self):
        b_grande, b_peque = D.Beliefs(200.0), D.Beliefs(45.0)
        d = D.DuelView(1, "seller", 40.0, rival_price=44.0, rounds_used=1, ticks_left=8)
        self.assertGreater(D.urgency(d, b_grande, DOS_ISSUES),
                           D.urgency(d, b_peque, DOS_ISSUES))

    def test_triage_corta_por_max_concurrent(self):
        duels = [D.DuelView(i, "seller", 40.0, ticks_left=10 - i) for i in range(1, 9)]
        sel = D.triage(duels, {}, SOLO_PRECIO)
        self.assertEqual(len(sel), SOLO_PRECIO.max_concurrent)
        # El de menos ticks restantes va primero: es el que puede morir en 0.
        self.assertEqual(sel[0].duel_id, 8)

    def test_triage_sin_creencias_no_explota(self):
        sel = D.triage([D.DuelView(1, "buyer", 80.0)], {}, DOS_ISSUES, budget=5)
        self.assertEqual(len(sel), 1)


# -------------------------------------------------------- parseo y sesiones

class TestLecturaDelApi(unittest.TestCase):
    """SUPOSICIÓN: el formato del duelo. `/api/duels` da 401 sin clave."""

    def test_formato_del_docstring_del_sdk(self):
        d = D.DuelView.from_api({
            "id": 305, "role": "seller", "your_limit": 40,
            "rival_offer": {"price": 62, "days": 3},
            "issues": ["price", "days"], "your_days_weight": -2.5,
            "ticks_left": 9, "item": "Mercado de Vallehermoso",
        })
        self.assertEqual((d.duel_id, d.role, d.limit), (305, "seller", 40.0))
        self.assertEqual((d.rival_price, d.rival_days), (62.0, 3))
        self.assertTrue(d.two_issue)
        self.assertEqual(d.days_weight, -2.5)

    def test_alias_y_oferta_anidada(self):
        d = D.DuelView.from_api({
            "duel_id": 7, "side": "buyer", "limit": 90,
            "their_offer": {"offer": {"price": 70, "days": 1}},
        }, session=DOS_ISSUES)
        self.assertEqual(d.duel_id, 7)
        self.assertEqual(d.rival_price, 70.0)
        self.assertEqual(d.rival_days, 1)
        self.assertTrue(d.two_issue, "los issues caen a los de la sesión")

    def test_duelo_sin_oferta(self):
        d = D.DuelView.from_api({"id": 1, "role": "seller", "your_limit": 40})
        self.assertFalse(d.has_rival_offer)
        self.assertIsNone(d.rival_price)

    def test_parametros_reales_del_schedule(self):
        """Verificado contra `GET /api/schedule` (público): cuatro sesiones."""
        self.assertEqual(sorted(D.SESSIONS),
                         ["Duels I", "Duels II", "Duels III", "Final"])
        self.assertEqual([D.SESSIONS[n].decay for n in
                          ("Duels I", "Duels II", "Duels III", "Final")],
                         [0.06, 0.08, 0.10, 0.10])
        self.assertEqual([D.SESSIONS[n].duel_ticks for n in
                          ("Duels I", "Duels II", "Duels III", "Final")],
                         [16, 16, 12, 12])
        self.assertEqual([D.SESSIONS[n].max_concurrent for n in
                          ("Duels I", "Duels II", "Duels III", "Final")],
                         [3, 6, 4, 4])
        self.assertFalse(D.SESSIONS["Duels I"].two_issue)
        self.assertTrue(all(D.SESSIONS[n].two_issue for n in
                            ("Duels II", "Duels III", "Final")))

    def test_la_merma_es_parametro_no_constante(self):
        """El mismo duelo con las cuatro sesiones da cuatro umbrales distintos."""
        duel = D.DuelView(1, "seller", 40.0, rival_price=70.0, rounds_used=0)
        needed = [D.respond(duel, D.Beliefs(100.0), D.SESSIONS[n]).detail["needed_gain"]
                  for n in ("Duels I", "Duels II", "Duels III")]
        self.assertEqual(needed, sorted(needed), "más decay = hay que exigir más")
        self.assertNotEqual(needed[0], needed[1])

    def test_chuleta_menciona_lo_critico(self):
        txt = D.cheat_sheet(SOLO_PRECIO)
        self.assertIn("CONTESTA", txt)
        self.assertIn("6.4%", txt.replace(",", "."))
        self.assertIn("no mandes days", D.cheat_sheet(SOLO_PRECIO))
        self.assertIn("missing_days", D.cheat_sheet(DOS_ISSUES))


if __name__ == "__main__":
    unittest.main()
