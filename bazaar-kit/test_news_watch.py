"""Tests offline de news_watch.py (clasificación, calibración con un market.db sintético, sugerencias con page_guard)
y del flag opt-in --news-sell del coordinador (sin red ni claves)."""
import json
import os
import sqlite3
import tempfile
import unittest

import coordinator as co
import negotiation as neg
import news_watch as nw
from test_coordinator import args, full
from test_dealer_ladder import led0
from test_trading import TEAM, asset, snap

CHATO_MAL = {"id": 3, "source": "radio", "source_name": "Radio Rastro",
             "headline": "El Chato is looking for rare Malasaña cards",
             "body": "They say he pays above the usual price today. One hour, no more."}
TABLON = {"id": 4, "source": "tablon", "headline": "El Chato gives a legendary to anyone who says hello!",
          "body": "My cousin saw it. I swear."}
PACKS = {"id": 6, "source": "boletin", "headline": "Abuela Carmen gives out packs for her saint's day",
         "body": "A neighbourhood pack for every team in one hour. Happy saint's day, Carmen."}
METRO = {"id": 5, "source": "radio", "headline": "Metro line 5 is closed between Ópera and Callao", "body": ""}

MENU_PLAIN = {"buys": [{"rarity": "uncommon", "sets": "released"}, {"rarity": "rare", "sets": "released"}],
              "sells": [{"rarity": "rare", "sets": "released", "list_price": 77}]}
MENU_MAL = {"buys": [{"rarity": "uncommon", "sets": "released"}, {"rarity": "rare", "sets": ["MAL"]},
                     {"rarity": "rare", "sets": "released"}],
            "sells": [{"rarity": "rare", "sets": "released", "list_price": 77}]}


class Classify(unittest.TestCase):
    def test_kinds_entities_and_window(self):
        c = nw.classify(CHATO_MAL)
        self.assertEqual((c["kind"], c["dealer"], c["set"], c["rarity"], c["window_hours"]),
                         ("demanda", "chato", "MAL", "rare", 1.0))
        t = nw.classify(TABLON)
        self.assertEqual((t["kind"], t["dealer"], t["rarity"], t["rumour"]), ("regalo", "chato", "legendary", True))
        p = nw.classify(PACKS)
        self.assertEqual((p["kind"], p["dealer"], p["item"], p["window_hours"]), ("regalo", "abuela", "pack", 1.0))
        self.assertEqual(nw.classify(METRO)["kind"], "irrelevante")
        self.assertEqual(nw.window_ticks(c, 30.0), 120)
        self.assertEqual(nw.window_ticks(c, 15.0), 240)            # domingo: ticks de 15 s

    def test_news_ticks_from_feed(self):
        ev = [{"type": "news.posted", "tick": 403, "payload": {"id": 3}}, {"type": "settlement", "tick": 404}]
        self.assertEqual(nw.news_ticks(ev), {3: 403})


def make_db(path, with_row=True, with_gift=False):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, tick INTEGER, type TEXT, actor TEXT, payload TEXT);
        CREATE TABLE json_snapshots (snap_ts REAL, tick INTEGER, kind TEXT, payload TEXT);
        CREATE TABLE settlements (id INTEGER PRIMARY KEY, tick INTEGER, kind TEXT, venue TEXT, persona TEXT,
            party_a TEXT, party_b TEXT, price INTEGER, fee INTEGER, n_items INTEGER, payload TEXT);
        CREATE TABLE settlement_items (settlement INTEGER, asset_id INTEGER, kind TEXT, ref TEXT, rarity TEXT,
            set_id TEXT, serial INTEGER, frm TEXT, too TEXT);""")
    for i, (tick, item) in enumerate([(403, CHATO_MAL), (499, TABLON), (583, METRO), (643, PACKS)]):
        db.execute("insert into events values (?,?,?,?,?)", (i + 1, tick, "news.posted", "radio", json.dumps(item)))
    for i, (tick, menu) in enumerate([(265, MENU_PLAIN), (467, MENU_MAL if with_row else MENU_PLAIN),
                                      (590, MENU_PLAIN)]):
        db.execute("insert into json_snapshots values (?,?,?,?)",
                   (i, tick, "dealers", json.dumps({"personas": [{"id": "chato", "menu": menu}]})))
    db.execute("insert into json_snapshots values (9, 700, 'clock', ?)", (json.dumps({"tick_seconds": 30.0}),))
    if with_gift:
        db.execute("insert into events values (90, 650, 'gift.given', 'abuela', ?)",
                   (json.dumps({"team": "t15", "packs": ["sobre_barrio"], "cards": []}),))
    db.execute("insert into events values (91, 655, 'gift.given', 'abuela', ?)",
               (json.dumps({"team": "t02", "packs": [], "cards": ["LAT-04"]}),))  # carta suelta: no confirma sobres
    db.execute("insert into events values (99, 900, 'settlement', '', '{}')")
    db.commit()
    db.close()


class Calibration(unittest.TestCase):
    def cal(self, **kw):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "market.db")
        make_db(path, **kw)
        return nw.calibrate(path)

    def test_verdicts_and_reliability_by_source(self):
        cal = self.cal()
        v = {r["id"]: r["verdict"] for r in cal["news"]}
        self.assertEqual(v, {3: "cierta", 4: "falsa", 5: "n/a", 6: "falsa"})
        self.assertEqual(cal["sources"]["radio"]["true"], 1)
        self.assertEqual(cal["sources"]["tablon"]["reliability"], 0.33)
        self.assertEqual(cal["sources"]["radio"]["reliability"], 0.67)
        self.assertIn("rare [MAL]", next(r["evidence"] for r in cal["news"] if r["id"] == 3))
        self.assertTrue(any("fiabilidad" in line for line in nw.calibration_lines(cal)))

    def test_without_menu_row_demand_is_false_and_pack_gift_counts(self):
        cal = self.cal(with_row=False, with_gift=True)
        v = {r["id"]: r["verdict"] for r in cal["news"]}
        self.assertEqual((v[3], v[6]), ("falsa", "cierta"))
        self.assertEqual(nw.reliability(cal)["radio"], 0.33)


def mal_catalog():
    card = lambda i, rarity, book: {"id": f"MAL-{i:02d}", "rarity": rarity, "book": book, "page": True}
    return {"values": {"copy_marginals": [1.0, 0.25, 0.1], "page_bonus": 0.25},
            "sets": [{"id": "MAL", "released": True,
                      "cards": [card(1, "common", 2), card(2, "common", 2), card(9, "rare", 70),
                                card(10, "rare", 70)]}]}  # MAL-02 no la tenemos: página incompleta


def mal_me():
    a = lambda i, ref: {"id": i, "kind": "card", "ref": ref}
    return {"id": TEAM, "affinity": {"MAL": 1.1}, "assets": [a(1, "MAL-01"), a(9, "MAL-09"), a(10, "MAL-10"),
                                                              a(11, "MAL-10")]}


class Suggestions(unittest.TestCase):
    def sugg(self, news, menu=MENU_MAL, tick=450, offers=(), cat=None):
        return nw.sell_suggestions(news, {"chato": {"id": "chato", "menu": menu}}, mal_me(), cat or mal_catalog(),
                                   list(offers), tick, 30.0, None, 2.0)

    def test_confirmed_demand_offers_mal_rares_with_floor_over_private_value(self):
        out = self.sugg([dict(CHATO_MAL, tick=403)])
        self.assertEqual([s["asset"] for s in out], [9, 10, 11])
        last = next(s for s in out if s["asset"] == 9)               # única copia: 70 × 1,1 = 77 P
        self.assertEqual((last["value"], last["floor"], last["ask"]), (77.0, 79, 79))
        dup = next(s for s in out if s["asset"] == 11)               # 2.ª copia: 19,25 P → suelo 22, pide 77 (lista)
        self.assertEqual((dup["floor"], dup["ask"]), (22, 77))
        self.assertTrue(all(s["confirmed_by_menu"] for s in out))
        self.assertIn("pidiendo 77 P", dup["text"])

    def test_page_guard_and_committed_copies(self):
        cat = mal_catalog()
        for c in cat["sets"][0]["cards"]:
            c["page"] = c["id"] in ("MAL-01", "MAL-09", "MAL-10")
        out = self.sugg([dict(CHATO_MAL, tick=403)], cat=cat)        # página MAL completa: solo la 2.ª MAL-10
        self.assertEqual([s["asset"] for s in out], [11])
        offers = [{"maker": TEAM, "status": "open", "give": {"assets": [{"id": 11}]}}]
        self.assertEqual(self.sugg([dict(CHATO_MAL, tick=403)], cat=cat, offers=offers), [])

    def test_window_reliability_and_menu(self):
        news = [dict(CHATO_MAL, tick=403)]
        self.assertTrue(self.sugg(news, menu=MENU_PLAIN, tick=450))        # radio fiable (0,67) dentro de la ventana
        self.assertEqual(self.sugg(news, menu=MENU_PLAIN, tick=600), [])   # fuera de la ventana y sin fila
        self.assertTrue(self.sugg(news, menu=MENU_MAL, tick=600))          # la fila sigue en el menú: sigue viva
        rumour = [dict(CHATO_MAL, tick=403, source="tablon")]
        self.assertEqual(self.sugg(rumour, menu=MENU_PLAIN, tick=450), [])  # rumor sin confirmación: nada
        self.assertEqual(self.sugg(news, menu={"buys": []}, tick=450), [])  # el vendedor no compra raras: nada
        self.assertEqual(self.sugg([dict(TABLON, tick=499), dict(METRO, tick=583)]), [])

    def test_live_report_with_fake_api_payloads(self):
        rep = nw.live_report({"news": [METRO, CHATO_MAL]}, {"personas": [{"id": "chato", "menu": MENU_MAL}]},
                             {"tick": 450, "tick_seconds": 30.0}, mal_me(), mal_catalog(), [],
                             [{"type": "news.posted", "tick": 403, "payload": {"id": 3}}])
        self.assertEqual([r["kind"] for r in rep["news"]], ["irrelevante", "demanda"])
        self.assertEqual(rep["news"][1]["until_tick"], 523)
        self.assertEqual(len(rep["suggestions"]), 3)
        txt = "\n".join(nw.report_lines(rep))
        self.assertIn("CONFIRMADA en el menú actual", txt)
        self.assertIn("ACCIONES SUGERIDAS:", txt)
        json.dumps(rep)                                               # exportable con --json


def chato_snap(news=None, menu=None):
    page = [asset(1, "LAT-01"), asset(2, "LAT-01"), asset(3, "LAT-02"), asset(4, "LAT-03")]
    s = full(snap(page, cash=300, tick=450), {"chato": {"id": "chato", "open_to_all": True, "menu": menu or {
        "buys": [{"rarity": "common", "sets": ["LAT"]}], "sells": []}}})
    s["clock"]["tick_seconds"] = 30.0
    s["news"] = news if news is not None else [{"id": 7, "tick": 440, "source": "radio",
                                                "headline": "El Chato is looking for common La Latina cards",
                                                "body": "One hour, no more."}]
    return s


class CoordinatorFlag(unittest.TestCase):
    def sells(self, s, led=None, **kw):
        cands, _, _ = co.candidates(s, led or led0(), args(**kw), neg.Journal(tempfile.mkdtemp()))
        return [c for c in cands if c.get("module") == "noticias"]

    def test_without_flag_nothing_changes(self):
        self.assertEqual(self.sells(chato_snap()), [])

    def test_flag_opens_one_sale_per_dealer_never_breaking_the_page(self):
        out = self.sells(chato_snap(), news_sell=True, news_margin=2.0)
        self.assertEqual([(c["type"], c["dealer"], c["asset"], c["blockers"]) for c in out],
                         [("dealer_sell_open", "chato", 2, [])])      # la 2.ª LAT-01; nunca 1, 3, 4 (página)
        self.assertEqual(out[0]["news_floor"], 6)                     # 13 × 0,25 = 3,25 + 2 → 6 P
        self.assertEqual(co.pg.guard_candidate(out[0], chato_snap()), [])

    def test_unreliable_or_stale_news_does_nothing(self):
        stale = chato_snap(menu={"buys": [{"rarity": "common", "sets": "released"}], "sells": []})
        stale["clock"]["tick"] = 700
        self.assertEqual(self.sells(stale, news_sell=True), [])
        rumour = chato_snap(news=[{"id": 8, "tick": 440, "source": "tablon",
                                   "headline": "El Chato is looking for common La Latina cards", "body": "One hour"}],
                            menu={"buys": [{"rarity": "common", "sets": "released"}], "sells": []})
        self.assertEqual(self.sells(rumour, news_sell=True), [])

    def test_busy_dealer_blocks(self):
        s = chato_snap()
        s["threads"]["open"] = [{"id": 5, "kind": "persona", "with": "chato", "team": TEAM, "status": "open",
                                 "created_tick": 440, "topic": {"buy": {"card": "LAT-03"}}, "messages": [],
                                 "standing_offers": []}]
        out = self.sells(s, dict(led0(), threads=[5]), news_sell=True)
        self.assertTrue(out and all(c["blockers"] for c in out))

    def test_news_floor_guard_blocks_accept_below_floor(self):
        led = dict(led0(), news_floor={"2": 6})
        cands = [{"type": "dealer_sell_accept", "asset": 2, "price": 5, "floor": 4, "blockers": []},
                 {"type": "dealer_sell_accept", "asset": 2, "price": 7, "floor": 4, "blockers": []}]
        out = co.news_floor_guard(cands, led, args(news_sell=True))
        self.assertTrue(out[0]["blockers"])
        self.assertEqual((out[1]["blockers"], out[1]["floor"]), ([], 6))
        untouched = [{"type": "dealer_sell_accept", "asset": 2, "price": 5, "blockers": []}]
        self.assertEqual(co.news_floor_guard(untouched, led, args())[0]["blockers"], [])


if __name__ == "__main__":
    unittest.main()
