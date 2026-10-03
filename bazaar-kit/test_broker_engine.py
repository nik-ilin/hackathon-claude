"""Pruebas offline de `broker_engine`. Sin red, sin claves, sin servidor.

Lo que se comprueba, en orden de importancia:

1. El emparejamiento es óptimo de verdad: se compara contra fuerza bruta.
2. Bate al cruce por cotización (el puesto gratuito) en excedente realizado.
3. El parser aguanta libros que no entiende sin lanzar ni inventar.
4. Los casos de borde: libro vacío, un par, cruces solapados, empates y un
   operador sin contraparte.
"""

from __future__ import annotations

import itertools
import unittest

import broker_engine as be

CFG = be.BrokerConfig()


def sell(oid, ask, *, run="b1", card="SYN-01", maker=None):
    return {"id": oid, "maker": maker or f"m{oid}", "status": "open",
            "give": {"cash": 0, "assets": [{"id": 1, "kind": "card", "ref": card}], "types": []},
            "want": {"cash": ask, "assets": [], "types": []}, "run": run}


def buy(oid, bid, *, run="b1", card="SYN-01", maker=None):
    return {"id": oid, "maker": maker or f"n{oid}", "status": "open",
            "give": {"cash": bid, "assets": [], "types": []},
            "want": {"cash": 0, "assets": [], "types": [f"card:{card}"]}, "run": run}


def book(*offers, fee_bps=0, fee_per_card=0, public=()):
    return be.parse_book({"tick": 201, "fee_bps": fee_bps, "fee_per_card": fee_per_card,
                          "bench_offers": list(offers), "offers": list(public)})


def truth_of(costs: dict, values: dict) -> be.Truth:
    return be.Truth({k: float(v) for k, v in costs.items()}, {k: float(v) for k, v in values.items()})


# ------------------------------------------------------------------- parser


class TestParser(unittest.TestCase):
    def test_venta_y_compra(self):
        s = be.parse_offer(sell("b1-s0", 12), bench=True)
        b = be.parse_offer(buy("b1-b0", 20), bench=True)
        self.assertEqual((s.side, s.quote, s.card), (be.SELL, 12, "SYN-01"))
        self.assertEqual((b.side, b.quote, b.card), (be.BUY, 20, "SYN-01"))

    def test_la_corrida_sale_del_id_del_banco(self):
        raw = sell("b12-7", 9)
        del raw["run"]
        self.assertEqual(be.parse_offer(raw, bench=True).run, "b12")

    def test_campos_desconocidos_no_estorban(self):
        raw = sell("b1-s0", 12)
        raw.update({"patience": 3, "sombrero": {"nuevo": True}, "final": False})
        self.assertIsNotNone(be.parse_offer(raw, bench=True))

    def test_formas_que_no_son_nuestras(self):
        """Trueque, lote, sobre, oferta cerrada, oferta vacía: se descartan, no se adivinan."""
        swap = {"id": 1, "give": {"cash": 0, "assets": [{"kind": "card", "ref": "A"}]},
                "want": {"cash": 0, "types": ["card:B"]}}
        lot = dict(sell(2, 10), qty=3)
        pack = {"id": 3, "give": {"cash": 0, "assets": [{"kind": "pack", "ref": "sobre"}]},
                "want": {"cash": 5, "types": []}}
        closed = dict(sell(4, 10), status="settled")
        for raw in (swap, lot, pack, closed, {}, {"id": 5}, {"id": 6, "give": {}, "want": {}}):
            with self.subTest(raw=raw.get("id")):
                self.assertIsNone(be.parse_offer(raw))

    def test_un_sobre_no_es_una_carta(self):
        pack = {"id": 3, "give": {"cash": 0, "assets": [{"kind": "pack"}]}, "want": {"cash": 5}}
        self.assertIsNone(be.parse_offer(pack))

    def test_un_libro_basura_da_un_libro_vacio(self):
        for raw in (None, [], "nope", {"bench_offers": "x"}, {"offers": [None, 3, "a"]}):
            with self.subTest(raw=raw):
                b = be.parse_book(raw)
                self.assertEqual(b.offers, ())

    def test_lo_descartado_queda_a_la_vista(self):
        b = book(sell("b1-s0", 10), {"id": 99, "give": {}, "want": {}})
        self.assertEqual(len(b.bench), 1)
        self.assertEqual(len(b.unsupported), 1)

    def test_nombres_alternativos_de_clave(self):
        b = be.parse_book({"bench": [sell("b1-s0", 10)], "public_offers": []})
        self.assertEqual(len(b.bench), 1)

    def test_el_tablon_publico_tambien_se_lee(self):
        """Una oferta real de /api/venues/v02/offers (VERIFICADA hoy, tick 250)."""
        raw = {"id": 3417, "maker": "ma55bf699", "to": None, "venue": "v02", "status": "open",
               "give": {"cash": 0, "assets": [{"id": 390, "kind": "card", "ref": "LAT-05", "serial": 12,
                                               "rarity": "common", "set": "LAT", "print_run": 300}], "types": []},
               "want": {"cash": 7, "assets": [], "types": []},
               "expires_tick": 262, "created_tick": 202, "final": False}
        o = be.parse_offer(raw)
        self.assertEqual((o.side, o.quote, o.card, o.run), (be.SELL, 7, "LAT-05", "LAT-05"))


class TestComisiones(unittest.TestCase):
    def test_se_redondea_hacia_arriba_por_carta(self):
        self.assertEqual(be.Fees(300, 1).on(7), 2)      # ⌈0.21⌉ + 1
        self.assertEqual(be.Fees(500, 1).on(40), 3)     # cuadra con El Rastro: 40 -> 3 P
        self.assertEqual(be.Fees().on(100), 0)


class TestCorridas(unittest.TestCase):
    def test_una_corrida_con_dos_cartas_se_parte(self):
        b = book(sell("b1-s0", 10, card="A"), buy("b1-b0", 20, card="B"))
        self.assertEqual(sorted(be.runs(b.offers)), ["b1/A", "b1/B"])
        self.assertEqual(be.plan(b, CFG), [])  # cartas distintas: el motor no lo cruzaría

    def test_una_sola_carta_no_se_parte(self):
        b = book(sell("b1-s0", 10), buy("b1-b0", 20))
        self.assertEqual(list(be.runs(b.offers)), ["b1"])


# -------------------------------------------------------------- casos borde


class TestBordes(unittest.TestCase):
    def test_libro_vacio(self):
        b = book()
        self.assertEqual(be.plan(b, CFG), [])
        self.assertEqual(be.plan_quote_cross(b, CFG), [])
        self.assertEqual(be.possible_surplus(b, be.Truth()), 0.0)
        self.assertEqual(be.efficiency([], b, be.Truth()), 0.0)

    def test_un_solo_par(self):
        b = book(sell("s", 10), buy("b", 20))
        m = be.plan(b, CFG)
        self.assertEqual(len(m), 1)
        self.assertEqual((m[0].sell, m[0].buy, m[0].price), ("s", "b", 10))

    def test_un_par_que_no_cruza(self):
        self.assertEqual(be.plan(book(sell("s", 30), buy("b", 10)), CFG), [])

    def test_un_operador_sin_contraparte(self):
        """Tres vendedores y una puja: se cruza el coste más bajo y nadie más."""
        b = book(sell("s0", 10), sell("s1", 12), sell("s2", 14), buy("b0", 20))
        m = be.plan(b, CFG)
        self.assertEqual([(x.sell, x.buy) for x in m], [("s0", "b0")])

    def test_solo_un_lado(self):
        self.assertEqual(be.plan(book(sell("s0", 10), sell("s1", 12)), CFG), [])
        self.assertEqual(be.plan(book(buy("b0", 10)), CFG), [])

    def test_empates(self):
        """Cotizaciones idénticas: se cruzan todos los pares posibles, sin repetir oferta."""
        b = book(sell("s0", 10), sell("s1", 10), buy("b0", 10), buy("b1", 10))
        m = be.plan(b, CFG)
        self.assertEqual(len(m), 2)
        self.assertEqual(len({x.sell for x in m}), 2)
        self.assertEqual(len({x.buy for x in m}), 2)
        self.assertEqual(be.legal_matches(m, b)[1], [])

    def test_empate_exacto_es_legal_sin_comision(self):
        b = book(sell("s", 10), buy("b", 10))
        self.assertEqual(be.plan(b, CFG)[0].price, 10)

    def test_el_mismo_maker_no_se_cruza_consigo_mismo(self):
        b = book(sell("s", 10, maker="x"), buy("b", 20, maker="x"))
        self.assertEqual(be.plan(b, CFG), [])
        self.assertEqual(be.plan_quote_cross(b, CFG), [])
        self.assertEqual(be.plan(b, be.BrokerConfig(allow_same_maker=True)), be.plan(b, be.BrokerConfig(allow_same_maker=True)))
        self.assertEqual(len(be.plan(b, be.BrokerConfig(allow_same_maker=True))), 1)

    def test_tope_de_emparejamientos(self):
        offers = [sell(f"s{i}", 10) for i in range(5)] + [buy(f"b{i}", 20) for i in range(5)]
        b = book(*offers)
        self.assertEqual(len(be.plan(b, CFG)), 5)
        self.assertEqual(len(be.plan(b, be.BrokerConfig(max_matches=2))), 2)


# ------------------------------------------ el núcleo: cruces solapados y DP


class TestSolapados(unittest.TestCase):
    """El caso que explica por qué el puesto gratuito deja excedente en la mesa.

    asks [10, 20] y bids [25, 15]. El puesto empareja el ask más bajo con el bid
    más alto (10x25) y para, porque 20 > 15: UN cruce. Pero 10x15 y 20x25 cruzan
    los dos: DOS cruces, y cada par añade excedente verdadero positivo.
    """

    def setUp(self):
        self.b = book(sell("s_lo", 10), sell("s_hi", 20), buy("b_hi", 25), buy("b_lo", 15))
        self.truth = truth_of({"s_lo": 8, "s_hi": 16}, {"b_hi": 30, "b_lo": 18})

    def test_el_puesto_hace_un_solo_cruce(self):
        base = be.plan_quote_cross(self.b, CFG)
        self.assertEqual([(m.sell, m.buy) for m in base], [("s_lo", "b_hi")])

    def test_el_plan_hace_dos(self):
        m = be.plan(self.b, CFG)
        self.assertEqual(len(m), 2)
        self.assertEqual({(x.sell, x.buy) for x in m}, {("s_lo", "b_lo"), ("s_hi", "b_hi")})
        self.assertEqual(be.legal_matches(m, self.b)[1], [])

    def test_y_realiza_mas_excedente(self):
        base = be.efficiency(be.plan_quote_cross(self.b, CFG), self.b, self.truth)
        mine = be.efficiency(be.plan(self.b, CFG), self.b, self.truth)
        self.assertGreater(mine, base)
        self.assertEqual(mine, 1.0)  # (30−8) + (18−16) = 24 = todo el excedente posible

    def test_el_excedente_no_depende_del_precio_de_cruce(self):
        """La razón de diseño: el excedente de un par es valor − coste, y punto."""
        plan_ask = be.plan(self.b, be.BrokerConfig(price_policy="ask"))
        plan_mid = be.plan(self.b, be.BrokerConfig(price_policy="mid_capped"))
        self.assertNotEqual([m.price for m in plan_ask], [m.price for m in plan_mid])
        self.assertEqual(be.realized_surplus(plan_ask, self.b, self.truth),
                         be.realized_surplus(plan_mid, self.b, self.truth))


def brute_force(sells, buys, fees, model, cfg):
    """El mejor emparejamiento por enumeración. Sólo para libros diminutos."""
    best = 0.0
    for k in range(1, min(len(sells), len(buys)) + 1):
        for ss in itertools.combinations(sells, k):
            for bs in itertools.permutations(buys, k):
                total = 0.0
                for s, b in zip(ss, bs):
                    p = be.cross_price(s.quote, b.quote, fees, policy=cfg.price_policy)
                    if p is None:
                        total = float("-inf")
                        break
                    w = model.value_of(b) - model.cost_of(s)
                    if w - fees.on(p) < cfg.min_surplus:
                        total = float("-inf")
                        break
                    total += w
                best = max(best, total)
    return best


class TestOptimalidad(unittest.TestCase):
    def test_la_dp_iguala_a_la_fuerza_bruta(self):
        """Si la DP no fuera exacta, aquí se vería: 60 libros aleatorios pequeños."""
        import random
        rng = random.Random(7)
        for case in range(60):
            n_s, n_b = rng.randint(0, 4), rng.randint(0, 4)
            offers = [sell(f"s{i}", rng.randint(1, 40)) for i in range(n_s)]
            offers += [buy(f"b{i}", rng.randint(1, 40)) for i in range(n_b)]
            b = book(*offers, fee_bps=rng.choice([0, 300]), fee_per_card=rng.choice([0, 1]))
            cfg = be.BrokerConfig()
            model = be.estimate_limits(b.offers, cfg)
            got = sum(model.value_of(o2) - model.cost_of(o1)
                      for o1, o2 in _pairs(be.plan(b, cfg), b))
            want = brute_force([o for o in b.offers if o.side == be.SELL],
                               [o for o in b.offers if o.side == be.BUY], b.fees, model, cfg)
            with self.subTest(case=case):
                self.assertAlmostEqual(got, want, places=6)

    def test_el_plan_nunca_propone_algo_que_el_motor_rechace(self):
        for seed in range(30):
            b, _ = be.synth_book(seed, fees=be.Fees(300, 1))
            ok, bad = be.legal_matches(be.plan(b, CFG), b)
            with self.subTest(seed=seed):
                self.assertEqual(bad, [])


def _pairs(matches, b):
    by_id = {o.id: o for o in b.offers}
    return [(by_id[m.sell], by_id[m.buy]) for m in matches]


# ------------------------------------------------------------- precio y comisión


class TestPrecio(unittest.TestCase):
    def test_el_ask_es_el_precio_legal_mas_bajo(self):
        self.assertEqual(be.cross_price(10, 20, be.Fees(300, 1)), 10)
        self.assertIsNone(be.cross_price(10, 10, be.Fees(300, 1)))  # 10 + 1 > 10

    def test_el_punto_medio_del_puesto_puede_ser_ilegal(self):
        """El fallo concreto de `starter_broker.bench_plan`: no descuenta la comisión."""
        b = book(sell("s", 10), buy("b", 12), fee_bps=300, fee_per_card=1)
        base = be.plan_quote_cross(b, CFG)
        self.assertEqual(base[0].price, 11)                 # (10+12)//2
        self.assertEqual(be.legal_matches(base, b)[0], [])  # 11 + 2 > 12: rechazado
        mine = be.plan(b, CFG)
        self.assertEqual(mine[0].price, 10)                 # 10 + 1 <= 12: aceptado
        self.assertEqual(len(be.legal_matches(mine, b)[0]), 1)

    def test_mid_capped_baja_hasta_que_es_legal(self):
        p = be.cross_price(10, 12, be.Fees(300, 1), policy="mid_capped")
        self.assertEqual(p, 10)
        self.assertLessEqual(p + be.Fees(300, 1).on(p), 12)

    def test_la_comision_se_resta_del_excedente_realizado(self):
        b = book(sell("s", 10), buy("b", 30), fee_bps=1000)
        t = truth_of({"s": 8}, {"b": 40})
        m = be.plan(b, CFG)
        self.assertEqual(be.realized_surplus(m, b, t, charge_fee=False), 32.0)
        self.assertEqual(be.realized_surplus(m, b, t), 31.0)  # ⌈10 %·10⌉ = 1 se va al mercado


class TestLegalidad(unittest.TestCase):
    def test_una_oferta_no_se_usa_dos_veces(self):
        b = book(sell("s", 10), buy("b0", 20), buy("b1", 30))
        dup = [be.Match("s", "b0", 10, "b1", 1.0), be.Match("s", "b1", 10, "b1", 1.0)]
        ok, bad = be.legal_matches(dup, b)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(bad), 1)

    def test_los_lados_invertidos_se_rechazan(self):
        b = book(sell("s", 10), buy("b", 20))
        ok, bad = be.legal_matches([be.Match("b", "s", 15, "b1", 1.0)], b)
        self.assertEqual((ok, len(bad)), ([], 1))

    def test_ids_inexistentes_se_rechazan(self):
        b = book(sell("s", 10), buy("b", 20))
        self.assertEqual(be.legal_matches([be.Match("x", "y", 10, "b1", 1.0)], b)[0], [])

    def test_el_payload_es_el_de_la_api(self):
        m = be.Match("b1-s0", "b1-b0", 11, "b1", 4.0)
        self.assertEqual(m.as_payload(), {"sell": "b1-s0", "buy": "b1-b0", "price": 11})


# --------------------------------------------------- estimación de los límites


class TestLimites(unittest.TestCase):
    def test_theta_sale_de_las_medianas(self):
        b = book(sell("s0", 22), sell("s1", 22), buy("b0", 18), buy("b1", 18))
        model = be.estimate_limits(b.offers, CFG)
        self.assertEqual(model.source, "book")
        self.assertAlmostEqual(model.shade, (22 - 18) / 40)
        self.assertLess(model.cost(22), 22)   # el coste está por debajo del ask
        self.assertGreater(model.value(18), 18)  # el valor por encima del bid

    def test_sin_un_lado_se_usa_el_defecto(self):
        model = be.estimate_limits(book(sell("s", 10)).offers, CFG)
        self.assertEqual((model.source, model.shade), ("default", CFG.default_shade))

    def test_si_las_pujas_ya_superan_a_las_ventas_no_se_estima_sombreado(self):
        b = book(sell("s", 10), buy("b", 40))
        self.assertEqual(be.estimate_limits(b.offers, CFG).source, "default")

    def test_theta_esta_acotado(self):
        b = book(sell("s", 1000), buy("b", 1))
        self.assertLessEqual(be.estimate_limits(b.offers, CFG).shade, CFG.max_shade)

    def test_el_estimador_se_acerca_al_sombreado_real(self):
        """Con el generador, θ real medio = 0.25; el estimador debería rondarlo."""
        got = []
        for seed in range(30):
            b, _ = be.synth_book(seed, per_side=12, runs_n=1)
            got.append(be.estimate_limits(b.offers, CFG).shade)
        self.assertAlmostEqual(sum(got) / len(got), 0.25, delta=0.10)

    def test_ningun_cruce_por_cotizacion_tiene_excedente_verdadero_negativo(self):
        """Si el sombreado es hacia fuera, coste <= ask <= bid <= valor: por eso
        la estimación nunca crea cruces, sólo los ordena."""
        for seed in range(20):
            b, t = be.synth_book(seed)
            for m in be.legal_matches(be.plan(b, CFG), b)[0]:
                with self.subTest(seed=seed, m=m.sell):
                    self.assertGreater(t.value[m.buy] - t.cost[m.sell], 0)


# -------------------------------------------------------- medida y comparación


class TestMedida(unittest.TestCase):
    def test_el_excedente_posible_ignora_las_cotizaciones(self):
        """Un par que no cruza por cotización sí cuenta en el máximo teórico:
        es la razón de que ni el puesto ni nosotros lleguemos nunca al 100 %."""
        b = book(sell("s", 30), buy("b", 10))
        t = truth_of({"s": 5}, {"b": 40})
        self.assertEqual(be.possible_surplus(b, t), 35.0)
        self.assertEqual(be.plan(b, CFG), [])
        self.assertEqual(be.efficiency(be.plan(b, CFG), b, t), 0.0)

    def test_la_eficiencia_esta_entre_0_y_1(self):
        for seed in range(20):
            b, t = be.synth_book(seed, fees=be.Fees(300))
            for matches in (be.plan_quote_cross(b, CFG), be.plan(b, CFG)):
                e = be.efficiency(matches, b, t)
                with self.subTest(seed=seed):
                    self.assertGreaterEqual(e, 0.0)
                    self.assertLessEqual(e, 1.0)

    def test_el_generador_es_reproducible(self):
        a, ta = be.synth_book(3)
        c, tc = be.synth_book(3)
        self.assertEqual([o.quote for o in a.offers], [o.quote for o in c.offers])
        self.assertEqual(ta.cost, tc.cost)

    def test_el_plan_bate_al_cruce_por_cotizacion(self):
        """La prueba que justifica todo el archivo, sobre 40 libros sintéticos."""
        for fees in (be.Fees(), be.Fees(300), be.Fees(300, 1)):
            c = be.compare(range(40), fees=fees)
            with self.subTest(fees=fees):
                self.assertGreater(c["plan_eff"], c["base_eff"] + 0.03)
                self.assertGreaterEqual(c["plan_matches"], c["base_matches"])
                self.assertEqual(c["plan_refused"], 0.0)

    def test_con_comision_el_puesto_se_come_rechazos(self):
        c = be.compare(range(40), fees=be.Fees(300, 1))
        self.assertGreater(c["base_refused"], 0.0)

    def test_gana_libro_a_libro_casi_siempre(self):
        c = be.compare(range(40), fees=be.Fees(300))
        worse = [r for r in c["rows"] if r["plan_eff"] < r["base_eff"] - 1e-9]
        self.assertEqual(worse, [], "el plan nunca debería realizar menos que el puesto")

    def test_libros_desequilibrados(self):
        c = be.compare(range(20), lopsided=0.5, fees=be.Fees(300))
        self.assertGreaterEqual(c["plan_eff"], c["base_eff"])


class TestSesion(unittest.TestCase):
    def test_la_sesion_completa_tambien_lo_confirma(self):
        s = be.session_compare(range(10), fees=be.Fees(300))
        self.assertGreater(s["plan_eff"], s["base_eff"])

    def test_esperar_no_paga_en_el_simulador(self):
        """Resultado negativo, anotado a propósito: con relajación y salidas,
        apartar un cruce para esperar uno mejor rinde menos que cruzar ya. Por
        eso `BrokerConfig.defer` viene apagado."""
        off = be.session_compare(range(10), cfg=be.BrokerConfig(defer=False), fees=be.Fees(300))
        on = be.session_compare(range(10), cfg=be.BrokerConfig(defer=True), fees=be.Fees(300))
        self.assertLessEqual(on["plan_eff"], off["plan_eff"] + 1e-9)

    def test_defer_aparta_cruces_pero_no_todos(self):
        b, _ = be.synth_book(1, per_side=8)
        cfg = be.BrokerConfig(defer=True, survive=0.99, relax=0.9)
        self.assertLess(len(be.plan(b, cfg, ticks_left=16)), len(be.plan(b, cfg, ticks_left=1)))

    def test_sin_ticks_por_delante_no_se_espera(self):
        b, _ = be.synth_book(1)
        cfg = be.BrokerConfig(defer=True)
        self.assertEqual(len(be.plan(b, cfg, ticks_left=1)), len(be.plan(b, be.BrokerConfig())))

    def test_un_operador_nunca_cotiza_mas_alla_de_su_limite(self):
        """Invariante del simulador: si se rompe, aparecen cruces con excedente
        negativo y la medida deja de significar nada."""
        b, t = be.synth_book(2)
        offers = b.offers
        for _ in range(40):
            offers = be._relaxed_true(offers, t, 0.5)
            for o in offers:
                limit = t.of(o.id)
                if o.side == be.SELL:
                    self.assertGreaterEqual(o.quote, limit - 1)
                else:
                    self.assertLessEqual(o.quote, limit + 1)


class TestInforme(unittest.TestCase):
    def test_el_informe_se_genera(self):
        text = be.report(range(10))
        self.assertIn("base", text)
        self.assertIn("plan", text)


if __name__ == "__main__":
    unittest.main()
