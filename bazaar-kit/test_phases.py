"""Fases hacia el cierre: operación activa, transición y tesorería. Sin red ni operaciones."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import coordinator as co
import market_intel as mi
import negotiation as neg
import page_guard as pg
import phases as ph
from test_audit_fixes import new_led
from test_market_intel import TEAM, A, ask, bid, offer
from test_page_campaign import cargs, inventory, snapm

CLOSE = "2026-10-03T23:00:00+02:00"


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-03T{hhmm}:00+02:00")


def world(cash=120, boards=(), mine=(), closes=CLOSE, owned=("MAL-08", "MAL-09")):
    s = snapm(inventory(list(owned)), list(boards), mine=list(mine), cash=cash)
    s["clock"]["closes"] = closes
    return s


def run(s, now, led=None, **kw):
    ph.NOW_OVERRIDE = at(now) if isinstance(now, str) else now
    kw.setdefault("phases", True)
    kw.setdefault("reserve", 5)
    kw.setdefault("page_campaign", "MAL")
    try:
        with tempfile.TemporaryDirectory() as d:
            saved = co.DATA
            co.DATA = Path(d)
            try:
                co.INTEL_STATE["intel"] = None
                return co.candidates(s, led or new_led(), cargs(**kw), neg.Journal(d))
            finally:
                co.DATA = saved
    finally:
        ph.NOW_OVERRIDE = None


def pick(cands, **m):
    return [c for c in cands if all(c.get(k) == v for k, v in m.items())]


class PhaseClock(unittest.TestCase):
    def st(self, now, closes=CLOSE, cfg=None, conf=300, pub=0, ex=0):
        return ph.state({"closes": closes}, cfg or ph.PhaseConfig(enabled=True), ph.scenarios(conf, pub, ex), at(now))

    def test_01_phase_changes_and_follow_the_official_close(self):
        self.assertEqual([self.st(t)["phase"] for t in ("18:30", "21:29", "21:31", "22:29", "22:31", "22:59")],
                         ["A", "A", "B", "B", "C", "C"])
        # cierre distinto (el servidor lo mueve): las fases se desplazan, no hay 23:00 fijo
        late = "2026-10-03T23:30:00+02:00"
        self.assertEqual([self.st(t, late)["phase"] for t in ("21:29", "22:05", "23:05")], ["A", "B", "C"])
        sun = "2026-10-04T15:00:00+02:00"
        self.assertEqual(self.st("18:30", sun)["phase"], "A")
        w = [self.st(t)["w"] for t in ("21:31", "22:00", "22:29")]
        self.assertTrue(w[0] < w[1] < w[2] <= 1.0)
        self.assertEqual(ph.state({}, ph.PhaseConfig(enabled=True), ph.scenarios(0, 0, 0))["phase"], "A")
        self.assertFalse(self.st("23:00", cfg=ph.PhaseConfig(enabled=False))["enabled"])

    def test_02_unreachable_target_accelerates_gradually_and_explains(self):
        ok = self.st("21:00", conf=160)
        self.assertEqual((ok["accelerated_min"], ok["reachable"]), (0, True))
        bad = self.st("21:00", conf=20, pub=10, ex=10)   # máximo observado 40 < 150
        self.assertFalse(bad["reachable"])
        self.assertGreater(bad["accelerated_min"], 0)
        self.assertEqual(bad["phase"], "B", "21:00 ya es transición porque se adelantó")
        self.assertIn("NO ALCANZABLE", bad["reason"])
        mid = self.st("21:00", conf=20, pub=60, ex=40)    # máximo 120: déficit menor ⇒ adelanto menor
        self.assertLess(mid["accelerated_min"], bad["accelerated_min"])
        self.assertLessEqual(bad["accelerated_min"], 60)

    def test_03_scenarios_never_include_unsettled_money_in_confirmed(self):
        sc = ph.scenarios(100, 40, 25)
        self.assertEqual((sc["confirmed"], sc["with_published_sales"], sc["with_executable_sales"]), (100, 140, 165))


class PhaseBehaviour(unittest.TestCase):
    def sale_board(self):
        # puja de otro equipo por nuestro duplicado MAL-02 (venta rentable) y un ask de MAL-10 (compra de campaña)
        return [bid(801, "v02", "MAL-02", 20, maker="t07", exp=900), ask(802, "v02", 90, "MAL-10", 28, maker="t09", exp=900)]

    def test_04_phase_a_is_normal_activity(self):
        cands, pl, _ = run(world(boards=self.sale_board()), "19:00")
        self.assertEqual(pl["phase"]["phase"], "A")
        buy = pick(cands, page_campaign="MAL-10", type="accept")
        self.assertTrue(buy and not buy[0]["blockers"])

    def test_05_profitable_purchase_incompatible_with_closing_treasury(self):
        board = self.sale_board()
        cands, pl, _ = run(world(cash=120, boards=board), "22:10")      # transición: 120 P − 28 P < w×150
        buy = pick(cands, page_campaign="MAL-10", type="accept")[0]
        self.assertTrue(any("TRANSICIÓN" in b for b in buy["blockers"]), buy["blockers"])
        self.assertGreater(buy["du"], 0, "es rentable, pero compite con la meta de efectivo")
        cands2, _, _ = run(world(cash=120, boards=board), "22:10", treasury_allow_campaign=True)
        self.assertFalse(pick(cands2, page_campaign="MAL-10", type="accept")[0]["blockers"],
                         "el operador decide que la campaña compita en B")
        cands3, _, _ = run(world(cash=400, boards=board), "22:10")      # caja de sobra: la meta no se compromete
        self.assertFalse(pick(cands3, page_campaign="MAL-10", type="accept")[0]["blockers"])
        cands4, _, _ = run(world(cash=400, boards=board), "22:45", treasury_allow_campaign=True)
        self.assertTrue(any("TESORERÍA" in b for b in pick(cands4, page_campaign="MAL-10", type="accept")[0]["blockers"]),
                        "en C nunca se compra, ni con el permiso")

    def test_06_treasury_prioritises_profitable_sales_and_cancels_open_bids(self):
        mine = [bid(900, "v02", "RET-02", 25, maker=TEAM, created=900, exp=2000)]
        cands, pl, _ = run(world(cash=200, boards=self.sale_board(), mine=mine), "22:40")
        sale = [c for c in cands if ph.is_sale(c) and not c["blockers"]]
        self.assertTrue(sale, "la venta rentable sigue permitida")
        can = pick(cands, module="tesorería")
        self.assertEqual([c["offer"] for c in can], [900], "se cancela la puja que gastaría mañana el capital")
        self.assertFalse(can[0]["blockers"])
        self.assertIn("confirma", can[0]["reason"])
        ordinary = [c for c in cands if ph.purchase_cost(c) > 0 and not c.get("blockers")]
        self.assertEqual(ordinary, [], "sin compras ordinarias en tesorería")
        a_cands, _, _ = run(world(cash=200, boards=self.sale_board(), mine=mine), "19:00")
        self.assertFalse(pick(a_cands, module="tesorería"), "fuera de C no se cancela por tesorería")

    def test_07_last_ticks_block_new_posts_but_not_accepts_or_cancels(self):
        mine = [bid(900, "v02", "RET-02", 25, maker=TEAM, created=900, exp=2000)]
        cands, _, _ = run(world(cash=200, boards=self.sale_board(), mine=mine), at("22:59").replace(second=10))
        for c in cands:
            if c["type"] in ("list", "bid", "swap_list"):
                self.assertTrue(c["blockers"], c)
        self.assertTrue(pick(cands, module="tesorería"))
        acc = [c for c in cands if c["type"] == "accept" and ph.is_sale(c)]
        self.assertTrue(all(not c["blockers"] for c in acc), "aceptar una venta aún liquida en el tick siguiente")

    def test_08_open_obligations_reduce_free_cash_simultaneously(self):
        mine = [bid(900, "v02", "RET-02", 25, maker=TEAM, created=900, exp=2000),
                bid(901, "v02", "RET-03", 35, maker=TEAM, created=900, exp=2000)]
        _, pl, _ = run(world(cash=150, mine=mine), "19:00")
        self.assertEqual(pl["phase"]["scenarios"]["confirmed"], 150 - 60, "las dos pujas pueden aceptarse a la vez")

    def test_09_pending_sales_are_not_spendable_cash(self):
        mine = [ask(910, "v02", 502, "LAV-02", 30, maker=TEAM, created=900, exp=2000)]
        _, pl, _ = run(world(cash=100, mine=mine), "19:00")
        sc = pl["phase"]["scenarios"]
        self.assertEqual(sc["confirmed"], 100)
        self.assertEqual(sc["with_published_sales"], 130)

    def test_10_completed_pages_stay_protected_in_treasury(self):
        s = world(cash=100, boards=[bid(803, "v02", "MAL-04", 60, maker="t07", exp=900)], owned=("MAL-08", "MAL-09", "MAL-10"))
        cands, _, _ = run(s, "22:40")
        pg.apply_guard(cands, s, set())
        sells = [c for c in cands if c["type"] == "accept" and (c.get("deliver") or {}).get("MAL-04")]
        self.assertTrue(sells and all(c["blockers"] for c in sells), "MAL completa: su copia única no se vende por tesorería")

    def test_11_expiry_capped_by_remaining_time(self):
        s = world(cash=100)
        a, _, _ = run(s, "19:00")
        b, pl, _ = run(s, "22:50")
        la = [c.get("expires_in") for c in a if c["type"] == "list" and c.get("expires_in")]
        lb = [c.get("expires_in") for c in b if c["type"] in ("list", "bid") and c.get("expires_in")]
        remaining = 10 * 2 * 1.0
        self.assertTrue(all(x <= int(remaining * max(1.0, 2.0)) for x in lb), lb)
        if la and lb:
            self.assertLess(max(lb), max(la))

    def test_12_disabled_changes_nothing(self):
        board = self.sale_board()
        base, pl0, _ = run(world(cash=120, boards=board), "22:45", phases=False)
        self.assertFalse(pl0["phase"]["enabled"])
        self.assertFalse(pick(base, module="tesorería"))
        self.assertFalse(pick(base, page_campaign="MAL-10", type="accept")[0]["blockers"])


class ResaleAndCooldown(unittest.TestCase):
    def test_13_resale_needs_a_real_discounted_exit_and_time(self):
        cfg = ph.PhaseConfig(enabled=True)
        c = {"type": "accept", "cash": -30, "receive": {"MAL-04": 1}, "ref": "MAL-04"}

        class St:  # best_bid mínimo
            def __init__(self, price): self.best_bid = type("Q", (), {"price": price})()

        ok, why = ph.resale_backed(c, {"MAL-04": St(60)}, 20, cfg, 1.0)
        self.assertTrue(ok, why)
        no, why = ph.resale_backed(c, {"MAL-04": St(35)}, 20, cfg, 1.0)
        self.assertFalse(no)
        self.assertIn("insuficiente", why)
        none, why = ph.resale_backed(c, {}, 20, cfg, 1.0)
        self.assertFalse(none)
        self.assertIn("no es una salida", why)
        self.assertFalse(ph.resale_backed(c, {"MAL-04": St(60)}, 2, cfg, 1.0)[0], "sin tiempo para dos operaciones")

    def test_14_cooldown_blocks_resend_after_failure_but_not_after_waiting(self):
        c = [{"type": "bid", "to": "t07", "ref": "MAL-10", "blockers": []}]
        led = new_led(actions=[{"type": "bid", "to": "t07", "ref": "MAL-10", "status": "released", "tick": 100}])
        co.apply_cooldowns(c, led, 103, 6)
        self.assertTrue(c[0]["blockers"] and "cooldown" in c[0]["blockers"][0])
        c2 = [{"type": "bid", "to": "t07", "ref": "MAL-10", "blockers": []}]
        co.apply_cooldowns(c2, led, 107, 6)
        self.assertFalse(c2[0]["blockers"], "pasado el cooldown se puede reintentar")
        c3 = [{"type": "bid", "to": "t09", "ref": "MAL-10", "blockers": []}]
        co.apply_cooldowns(c3, led, 103, 6)
        self.assertFalse(c3[0]["blockers"], "otra contraparte no queda afectada")
        pend = new_led(actions=[{"type": "bid", "to": "t07", "ref": "MAL-10", "status": "submitted", "tick": 100}])
        c4 = [{"type": "bid", "to": "t07", "ref": "MAL-10", "blockers": []}]
        co.apply_cooldowns(c4, pend, 101, 6)
        self.assertFalse(c4[0]["blockers"], "una propuesta pendiente (silencio) no cuenta como rechazo")

    def test_15_restart_does_not_duplicate_treasury_cancels(self):
        mine = [bid(900, "v02", "RET-02", 25, maker=TEAM, created=900, exp=2000)]
        s = world(cash=200, mine=mine)
        c1, _, _ = run(s, "22:40")
        c2, _, _ = run(s, "22:40")
        ids = lambda cs: sorted(c["offer"] for c in cs if c["type"] == "cancel" and c.get("module") == "tesorería")
        self.assertEqual((ids(c1), ids(c2)), ([900], [900]))
        self.assertEqual(len([c for c in c1 if c["type"] == "cancel" and c.get("offer") == 900]), 1)


if __name__ == "__main__":
    unittest.main()
