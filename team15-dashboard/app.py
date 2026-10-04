#!/usr/bin/env python3
"""Live, read-only Team 15 trade desk built on PR #2's public feed dashboard.

Run from the repository: BAZAAR_KEY=... python3 team15-dashboard/app.py
Only GET requests reach Bazaar. The key stays server-side; bind is localhost.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent / "bazaar-kit"
sys.path.append(str(KIT))  # los módulos locales (radio.py, scoring.py) deben ganar a los del kit
import dashboard as public_dashboard

import charts
import duel_history
import history
import operations
import scoring
import strategy_health
from planner import build_rank

# El kit también tiene radio.py; cargar el módulo del panel por ruta evita que
# una importación previa del kit sustituya sus funciones en un proceso largo.
_radio_spec = importlib.util.spec_from_file_location("team15_dashboard_radio", HERE / "radio.py")
_radio = importlib.util.module_from_spec(_radio_spec)
_radio_spec.loader.exec_module(_radio)
interpret, news_item = _radio.interpret, _radio.news_item
radio_summary, radio_event = _radio.radio_summary, _radio.radio_event

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai").rstrip("/")


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ") if isinstance(value, (float, int)) else esc(value)


RARITY_LABELS = {
    "common": "Común", "uncommon": "Poco común", "rare": "Rara",
    "epic": "Épica", "legendary": "Legendaria",
}


def rarity_label(value) -> str:
    return RARITY_LABELS.get(str(value).lower(), str(value) if value else "—")


class Reader:
    """GET only. Authentication is sent only to the game's origin."""
    def __init__(self, url: str, key: str | None, timeout: float = 9):
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout
        self._pace_lock = threading.Lock()
        self._last_request = None
        self._opener = urllib.request.build_opener(NoRedirect())

    def get(self, path: str, *, private: bool = False) -> dict:
        if private and not self.key:
            raise ValueError("Falta BAZAAR_KEY")
        headers = {"Accept": "application/json"}
        if private:
            headers["X-Team-Key"] = self.key
        req = urllib.request.Request(self.url + path, headers=headers, method="GET")
        # Public feed and concurrent venue reads share the same request budget.
        with self._pace_lock:
            now = time.monotonic()
            if self._last_request is not None:
                time.sleep(max(0, .3 - (now - self._last_request)))
            self._last_request = time.monotonic()
        with self._opener.open(req, timeout=self.timeout) as response:
            return json.load(response)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a private team header to a redirected origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def settled_by_dealer(stores, team: str) -> dict:
    """{dealer: [precio, ...]} de nuestras liquidaciones con vendedores, leídas del feed.

    Es una COTA INFERIOR: el almacén local sólo tiene los ticks recogidos, así que una
    casilla puede estar llena sin que aquí se vea. `GET /api/me` manda sobre esto; el
    contador sirve para ver *dónde* faltan huecos cuando esa cifra no está disponible."""
    out: dict[str, list] = {}
    for path in stores or ():
        p = Path(path)
        if not p.exists():
            continue
        with p.open() as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "settlement":
                    continue
                sides = [event.get("seller"), event.get("buyer"), event.get("with"), event.get("actor")]
                sides = [x.get("id") if isinstance(x, dict) else x for x in sides]
                if team not in sides:
                    continue
                dealer = next((x for x in sides if isinstance(x, str) and not x.startswith("t")), None)
                price = event.get("price") or event.get("cash")
                if dealer and isinstance(price, (int, float)):
                    out.setdefault(dealer, []).append(price)
    return out


class Model:
    def __init__(self, reader: Reader, team: str = "t15", reserve: int = 100,
                 feed_root: Path = KIT):
        self.reader, self.team, self.reserve = reader, team, reserve
        self.public = public_dashboard.Builder(reader, team=team, root=feed_root)
        self.history_path = Path(feed_root) / "data" / "score_history.jsonl"
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0
        self.catalog = None
        self.radio_tail = public_dashboard.Tail()
        self.radio_news = {}
        self._dealer_cache = None
        self.done_cache = []
        self.done_at = 0.0

    def _dealers(self) -> dict:
        if self._dealer_cache is None:
            try:
                self._dealer_cache = {p['id']: p for p in (self.reader.get('/api/dealers')
                                                           .get('personas') or []) if p.get('id')}
            except Exception:
                self._dealer_cache = {}
        return self._dealer_cache

    def _read_radio(self) -> list[dict]:
        """GET /api/news es la fuente completa: las tres fuentes y `at_hours` para los plazos.

        El feed se sigue leyendo porque llega en vivo, pero sólo trae `news.posted` de los
        ticks recogidos: con el almacén parado daba 1 noticia de 8."""
        try:
            for raw in self.reader.get('/api/news').get('news') or []:
                item = news_item(raw)
                if item['id'] is not None:
                    self.radio_news[item['id']] = item
        except Exception:
            pass  # el historial local sigue disponible durante un fallo de red
        events = []
        for path in self.public.stores:
            events.extend(self.radio_tail.read(path))
        try:
            events.extend(self.reader.get('/api/feed?limit=500').get('events') or [])
        except Exception:
            pass
        for event in events:
            item = radio_event(event)
            if item and item['id'] is not None:
                self.radio_news.setdefault(item['id'], item)
        return list(self.radio_news.values())

    def snapshot(self, max_age: float = 12.0) -> dict:
        with self.lock:
            if self.cached and time.time() - self.cached_at < max_age:
                return self.cached
            self.cached = self._build()
            self.cached_at = time.time()
            return self.cached

    def _build(self) -> dict:
        warnings = []
        public = self.public.build()
        if not self.catalog:
            try:
                self.catalog = self.reader.get("/api/catalog")
            except Exception as exc:
                warnings.append(f"Catálogo: {type(exc).__name__}")
        me, own_offers, live_duels, duel_error = {}, [], None, None
        if self.reader.key:
            try:
                me = self.reader.get("/api/me", private=True)
                if me.get("id") != self.team:
                    warnings.append(f"La clave corresponde a {me.get('id')}, no a {self.team}; vista privada bloqueada.")
                    me = {}
                else:
                    own_offers = self.reader.get("/api/me/offers", private=True).get("offers") or []
                    try:
                        live_duels = self.reader.get("/api/duels", private=True).get("duels") or []
                    except Exception as exc:
                        duel_error = f"{type(exc).__name__}: /api/duels"
                        warnings.append("Duelos privados no disponibles; la cola de cierre queda desactivada.")
            except Exception as exc:
                warnings.append(f"Equipo 15: {type(exc).__name__}. Comprueba BAZAAR_KEY.")
                me = {}
        else:
            warnings.append("Sin BAZAAR_KEY: el feed está en vivo, pero la mano y el ranking privado requieren la clave del equipo 15.")
        venues = public.venues.get("venues") or []
        open_venues = [v for v in venues if v.get("status") == "open"]
        boards = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            tasks = {pool.submit(self.reader.get, "/api/venues/" + urllib.parse.quote(v["venue"], safe="") + "/offers"): v["venue"]
                     for v in open_venues if v.get("venue")}
            for task in as_completed(tasks):
                venue = tasks[task]
                try:
                    boards[venue] = task.result().get("offers") or []
                except Exception:
                    warnings.append(f"Libro de {venue} no disponible")
        agg = self.public.aggs.get("rivals")
        rivals = agg.to_json() if agg else {"teams": []}
        listing_teams = {oid: listing.team for oid, listing in (agg.listings.items() if agg else [])}
        if agg:
            for rv in rivals["teams"]:
                view = agg.view(rv["team"])
                rv["best_bids"] = {ref: sought.best_bid for ref, sought in view.sought.items() if sought.best_bid}
        rank = build_rank(me, self.catalog or {}, public.clock, venues, boards, own_offers,
                          rivals, listing_teams, reserve=self.reserve,
                          market_refs={ref: {"fair": card.fair, "confidence": card.confidence}
                                       for ref, card in public.oracle.cards.items()})
        enrich_catalog_market(rank.get('catalog_rows') or [], self.catalog or {}, public.oracle.cards)
        holdings = {r['ref']: r.get('stock') for r in (rank.get('catalog_rows') or [])
                    if r.get('ref') and r.get('stock')}
        rank['radio'] = interpret(self._read_radio(), self.catalog or {}, rank.get('sale_guide') or [],
                                  int(public.clock.get('tick') or 0),
                                  t_hours=public.clock.get('t_hours'), holdings=holdings,
                                  dealers=self._dealers())
        rank['radio_summary'] = radio_summary(rank['radio'])
        rank["warnings"] = warnings + public.errors + rank["warnings"]
        rank["board_count"] = sum(map(len, boards.values()))
        rank["venue_count"] = len(boards)
        rank["built_at"] = time.time()
        rank["clock"] = public.clock
        rank["live"] = bool(me)
        rank["leaderboard"] = public.leaderboard
        tick_now = int(public.clock.get("tick") or 0) or None
        rank["scoring"] = scoring.scoring_block(rank.get("score") or {}, public.leaderboard, self.team)
        rank["peers"] = scoring.peers_block(public.leaderboard, self.team)
        rank["feed_health"] = scoring.feed_health(self.public.stores, tick_now)
        rank["operations"] = operations.build(duels=live_duels, duel_error=duel_error,
                                                tick=tick_now, cash=rank.get("cash"),
                                                offers=own_offers, reserve=self.reserve,
                                                venues=venues, feed_health=rank["feed_health"],
                                                verified=bool(rank.get("verified")), team=self.team)
        if me and time.time() - self.done_at >= 60:
            try:
                self.done_cache = self.reader.get('/api/duels?done=true', private=True).get('duels') or []
                self.done_at = time.time()
            except Exception:
                warnings.append('Historial de duelos: lectura privada no disponible; se conserva la última muestra.')
        rank['strategy_health'] = strategy_health.build(
            self.done_cache, (self.public.root, KIT), tick_now)
        rank['duel_history'] = self.done_cache
        rank["ladder"] = scoring.ladder_block(self._dealers(), settled_by_dealer(self.public.stores, self.team),
                                              me.get("unlocked") or [])
        if tick_now:
            try:
                history.append(self.history_path,
                               history.sample(tick_now, public.clock.get("t_hours"),
                                              rank.get("score") or {}, public.leaderboard,
                                              cash=rank.get("cash"),
                                              collection_value=rank.get("collection_value"),
                                              team=self.team, round_number=public.clock.get("round")))
            except OSError as exc:
                warnings.append(f"Historia: {type(exc).__name__}")
        rank["history"] = history.load(self.history_path)
        rank["history_summary"] = history.summary(rank["history"], self.team)
        rank["impact_events"] = history.impact_events(rank["history"])
        rank["rank_race"] = history.rank_race(rank["history"], self.team)
        if rank["feed_health"].get("status") != "fresh":
            rank["warnings"].append(
                "El almacén del feed no está al día: los precios, los rivales y el playbook se calculan sobre él. "
                "Arranca python3 feed_stream.py --collect")
        if not me:
            public_team = next((row for row in public.leaderboard.get("teams", [])
                                if row.get("team") == self.team), None)
            if public_team:
                rank["score"] = {"score": public_team.get("score")}
        return rank


def enrich_catalog_market(rows: list[dict], catalog: dict, market_cards: dict) -> None:
    """Attach only published mint counts and confirmed single-card sale prices."""
    published = {c.get('id'): c for st in catalog.get('sets') or [] for c in st.get('cards') or []}
    for row in rows:
        card = published.get(row['ref']) or {}
        market = market_cards.get(row['ref'])
        sold = list(market.settled) if market else []
        row['minted'] = card.get('minted')
        row['print_run'] = card.get('print_run')
        row['sold_prices'] = sold
        row['sold_median'] = statistics.median(sold) if sold else None


CSS = """
:root{--ink:#182b39;--paper:#edf2f5;--surface:#fff;--line:#ccd8df;--muted:#627683;
--teal:#0d6b74;--saffron:#bb7900;--red:#ad4f45;--blue:#ddeef2}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 Arial,Helvetica,sans-serif}
button{font:inherit;cursor:pointer}a{color:var(--teal)}.wrap{max-width:1600px;margin:auto;padding:24px 28px 70px}
header{display:flex;justify-content:space-between;gap:24px;align-items:end;border-bottom:3px solid var(--ink);padding-bottom:20px}
h1{font:700 38px/1.05 Georgia,serif;letter-spacing:-.035em;margin:0}h2{font:700 25px/1.15 Georgia,serif;margin:0 0 12px}
h3{font-size:17px;margin:0 0 5px}.sub{color:var(--muted);max-width:70ch;margin:9px 0 0}
.status{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.flag{background:var(--ink);color:white;padding:5px 10px;font-weight:700}
.flag.live{background:var(--teal)}.flag.warn{background:var(--red)}.clock{font-size:20px;font-weight:700}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;border-bottom:1px solid var(--line);margin:20px 0 26px}
.metric{padding:8px 22px 17px 0}.metric+.metric{border-left:1px solid var(--line);padding-left:22px}.metric small{display:block;color:var(--muted)}
.metric strong{display:block;font:700 28px/1.2 Georgia,serif;margin-top:4px}
.warning{background:#fff1e9;border-left:4px solid var(--red);padding:10px 14px;margin:10px 0}
.jump{display:flex;gap:16px;flex-wrap:wrap;margin:-9px 0 23px;font-size:13px;font-weight:700}.jump a{text-decoration:none;border-bottom:1px solid var(--teal)}
.guide{margin:0 0 30px}.guide-head{display:flex;justify-content:space-between;gap:18px;align-items:baseline;margin-bottom:13px}.guide-head p{margin:0;color:var(--muted)}
.sale-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:10px}.sale-card{background:var(--surface);border:1px solid var(--line);border-top:4px solid var(--teal);padding:16px 18px}
.sale-card.no-buyer{border-top-color:var(--muted)}.sale-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px}.sale-top h3{font:700 24px Georgia,serif;margin:0}.sale-top small{color:var(--muted)}
.sale-numbers{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:15px 0}.sale-numbers>div+div{border-left:1px solid var(--line);padding-left:12px}.sale-numbers strong{display:block;font:700 22px Georgia,serif;white-space:nowrap}
.sale-buyers{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}.buyer-chip{background:var(--blue);color:var(--teal);padding:3px 7px;font-size:12px}.buyer-chip.proposed{background:#f8edd5;color:#865b02}
.sale-note{color:var(--muted);font-size:13px;margin:0}.guide-empty{padding:15px;border:1px dashed var(--line);color:var(--muted)}
.radio{border-top:2px solid var(--ink);padding-top:14px;margin:5px 0 30px}.radio-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:10px}.radio-item{background:#fff;border:1px solid var(--line);padding:14px 16px}.radio-item h3{margin:4px 0}.radio-item p{margin:6px 0}.radio-item small{color:var(--muted)}.radio-action{border-left:3px solid var(--saffron);padding-left:9px;font-weight:700}
.catalog{border-top:2px solid var(--ink);padding-top:18px;margin-top:32px;scroll-margin-top:18px}.catalog-head{display:flex;justify-content:space-between;gap:20px;align-items:start}.catalog-head h2{font-size:31px;margin-bottom:8px}.catalog-head p{color:var(--muted);margin:0;max-width:75ch}.catalog-head .eyebrow{font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--teal);margin-bottom:5px}.catalog-brief{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);background:var(--surface);margin:20px 0}.catalog-brief>div{padding:15px 18px}.catalog-brief>div+div{border-left:1px solid var(--line)}.catalog-brief strong{display:block;font:700 28px/1.1 Georgia,serif;margin:4px 0}.catalog-brief small{display:block;color:var(--muted);font-size:12px}.catalog-brief .brief-action{color:var(--teal)}.catalog-brief .brief-alert{color:var(--saffron)}.catalog-map-head{display:flex;justify-content:space-between;gap:14px;align-items:baseline;margin:0 0 10px}.catalog-map-head h3{margin:0}.catalog-map-head p{margin:0;color:var(--muted);font-size:12px}.catalog-map{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:9px}.set-card{border:1px solid var(--line);background:var(--surface);padding:12px 14px;text-align:left;color:var(--ink);min-width:0;overflow:hidden;transition:background .18s,border-color .18s,transform .18s}.set-card:hover,.set-card:focus-visible,.set-card[aria-pressed=true]{border-color:var(--teal);background:var(--blue);transform:translateY(-1px)}.set-card-top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.set-card b{font-size:15px}.set-card small{font-size:12px;color:var(--muted)}.set-track{height:6px;background:var(--paper);margin:10px 0 7px}.set-track span{display:block;height:100%;background:var(--teal)}.set-cells{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:3px}.set-cells span{display:block;height:13px;background:var(--line)}.set-cells .owned{background:var(--teal)}.set-cells .free{background:var(--saffron)}.set-cells .unreleased{background:var(--paper);border:1px solid var(--line)}.catalog-legend{display:flex;align-items:center;flex-wrap:wrap;gap:14px;margin:11px 0 18px;padding:9px 11px;border:1px solid var(--line);background:var(--surface);font-size:12px;color:var(--muted)}.catalog-legend span{display:inline-flex;align-items:center;white-space:nowrap}.catalog-legend span:before{content:"";display:inline-block;flex:0 0 9px;width:9px;height:9px;margin-right:5px;background:var(--line)}.catalog-legend .l-owned:before{background:var(--teal)}.catalog-legend .l-free:before{background:var(--saffron)}.catalog-legend .l-unreleased:before{background:var(--paper);border:1px solid var(--line)}.catalog-toolbar{position:sticky;top:0;z-index:3;display:flex;flex-wrap:wrap;align-items:end;gap:8px;border:1px solid var(--line);padding:12px;background:rgba(237,242,245,.96);backdrop-filter:blur(8px)}.catalog-toolbar label{display:grid;gap:4px;color:var(--muted);font-size:12px;font-weight:700}.catalog-toolbar input,.catalog-toolbar select{height:38px;background:var(--surface);border:1px solid var(--line);color:var(--ink);padding:7px 9px;font:14px Arial,Helvetica,sans-serif}.catalog-toolbar input{min-width:min(100%,230px)}.catalog-toolbar button{height:38px;background:var(--ink);border:1px solid var(--ink);color:#fff;padding:7px 13px;font-weight:700}.catalog-result{margin:10px 0;color:var(--muted);font-size:13px}.catalog-table{overflow-x:auto;border:1px solid var(--line);background:var(--surface)}#catalog-table{min-width:1210px}#catalog-table th{position:sticky;top:62px;background:var(--ink);color:#fff;z-index:2;padding:10px 12px;font-weight:700}#catalog-table td{padding:11px 12px;vertical-align:top}#catalog-table tbody tr{border-left:3px solid transparent}#catalog-table tbody tr[data-state="free"]{border-left-color:var(--saffron)}#catalog-table tbody tr[data-state="missing"]{border-left-color:var(--teal)}#catalog-table tbody tr:hover{background:var(--blue)}#catalog-table .dim{color:var(--muted)}#catalog-table .strong{font-weight:700;color:var(--teal)}#catalog-table .blocked{color:var(--red)}#catalog-table .cell-main{font-weight:700}.catalog-state{display:inline-block;padding:3px 8px;background:var(--paper);font-size:12px;font-weight:700;white-space:nowrap}.catalog-state.free{background:#f8edd5;color:#865b02}.catalog-state.missing{background:var(--blue);color:var(--teal)}.catalog-state.unreleased{color:var(--muted)}.catalog-count{font-weight:700}.catalog-signals{display:flex;gap:10px;flex-wrap:wrap;font-size:12px}.catalog-signals b{color:var(--teal)}.catalog-hint{margin:12px 0 0;color:var(--muted);font-size:12px}.catalog-empty{padding:22px;text-align:center;color:var(--muted)}
.scarcity-track{display:block;height:5px;background:var(--paper);margin-top:5px;max-width:110px}.scarcity-track span{display:block;height:100%;background:var(--saffron)}.catalog-quick{display:flex;flex-wrap:wrap;gap:7px;margin:11px 0 0}.catalog-quick button{border:1px solid var(--line);background:var(--surface);color:var(--teal);padding:5px 9px;font-size:12px;font-weight:700}.catalog-quick button:hover,.catalog-quick button:focus-visible{border-color:var(--teal);background:var(--blue)}#catalog-table{min-width:1210px}.layout{display:grid;grid-template-columns:240px minmax(0,1fr);gap:26px}.sidebar{border-right:1px solid var(--line);padding-right:20px}
.team-button{display:flex;justify-content:space-between;width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);background:transparent;padding:10px 5px;color:var(--ink)}
.team-button[aria-pressed=true]{background:var(--blue);font-weight:700;color:var(--teal)}.team-button small{color:var(--muted)}
.section-head{display:flex;justify-content:space-between;gap:16px;align-items:baseline;margin:3px 0 14px}.section-head p{color:var(--muted);margin:0}
.rank{display:grid;gap:9px}.trade{display:grid;grid-template-columns:34px minmax(0,1.7fr) 105px 130px;gap:14px;align-items:start;background:var(--surface);border:1px solid var(--line);padding:15px 17px}
.trade.live,.trade.public-live{border-left:5px solid var(--teal)}.trade.proposal,.trade.public-proposal{border-left:5px solid var(--saffron)}.index{font:700 18px Georgia,serif;color:var(--muted)}
.trade p{margin:5px 0;color:var(--muted)}.trade .title{font-weight:700;font-size:17px}.trade .why{color:var(--ink);font-size:13px}
.tag{display:inline-block;font-size:12px;padding:2px 6px;background:var(--blue);color:var(--teal);margin-left:5px}.proposal .tag,.public-proposal .tag{background:#f8edd5;color:#865b02}
.money{font:700 23px Georgia,serif;white-space:nowrap}.gain{color:var(--teal)}.label{display:block;color:var(--muted);font-size:12px}
.below{display:grid;grid-template-columns:1fr 1fr;gap:25px;margin-top:30px}.panel{border-top:2px solid var(--ink);padding-top:13px}
.team-profile{background:var(--blue);border-left:4px solid var(--teal);padding:12px 16px;margin-top:16px}.team-profile p{margin:5px 0}
table{border-collapse:collapse;width:100%}th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-size:12px;font-weight:400}td:last-child,th:last-child{text-align:right}
.empty{padding:22px;border:1px dashed var(--line);color:var(--muted)}.foot{border-top:1px solid var(--line);margin-top:30px;padding-top:15px;color:var(--muted);font-size:13px}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--saffron);outline-offset:2px}@media(max-width:850px){.wrap{padding:18px}.layout{grid-template-columns:1fr}.sidebar{border:0;padding:0}.teams{display:flex;overflow-x:auto}.team-button{min-width:100px}.summary{grid-template-columns:1fr 1fr}.metric:nth-child(3){border-left:0;padding-left:0}.trade{grid-template-columns:25px 1fr 95px}.trade .surplus{grid-column:3}.trade .price{grid-column:3;grid-row:1}.below{grid-template-columns:1fr}.catalog-brief{grid-template-columns:1fr 1fr}.catalog-brief>div:nth-child(3){border-left:0;border-top:1px solid var(--line)}.catalog-brief>div:nth-child(4){border-top:1px solid var(--line)}}@media(max-width:440px){.catalog-head{display:block}.catalog-toolbar label,.catalog-toolbar input,.catalog-toolbar select{width:100%}.catalog-toolbar label:first-child{flex:1 1 100%}.catalog-brief>div{padding:12px}.catalog-brief strong{font-size:23px}}

.monitor{margin:28px 0;padding:22px;border:1px solid var(--line,#2a2f3a);border-radius:14px;background:var(--panel,#151a23)}
.comps{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));margin:18px 0}
.comp-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;font-size:.92rem}
.comp-head small{opacity:.55;font-weight:400}
.bar{position:relative;height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:visible}
.bar-fill{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#3d7dff,#58e3b0)}
.bar-best{position:absolute;top:-4px;width:2px;height:18px;background:#ffb347;border-radius:2px}
.breakdown{width:100%;border-collapse:collapse;margin:18px 0;font-size:.9rem}
.breakdown caption{text-align:left;font-weight:600;padding-bottom:8px;opacity:.75}
.breakdown th{text-align:left;font-weight:500;padding:7px 10px 7px 0;border-top:1px solid rgba(255,255,255,.07)}
.breakdown td{padding:7px 0;border-top:1px solid rgba(255,255,255,.07);vertical-align:baseline}
.breakdown .num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;padding-right:14px;white-space:nowrap}
.breakdown .why{opacity:.6;font-size:.84rem}
.breakdown .missing{opacity:.45;font-style:italic;font-weight:400}
.ladder{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:12px 0 4px}
.slot{padding:14px;border:1px solid rgba(255,255,255,.1);border-radius:11px;background:rgba(255,255,255,.02)}
.slot.open{border-color:#ffb347}
.slot.done{border-color:#58e3b0}
.slot.off{opacity:.5}
.slot-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.slot h4{margin:0;font-size:.95rem}
.slot-top small{opacity:.6;white-space:nowrap}
.pips{display:flex;gap:5px;margin:10px 0 8px}
.pip{width:100%;height:7px;border-radius:999px;background:rgba(255,255,255,.12)}
.pip.on{background:#58e3b0}
.slot .sub{margin:0;font-size:.8rem}
.waste{display:inline-block;margin-top:6px;color:#ffb347;font-size:.78rem}
@media (max-width:640px){.monitor{padding:16px}.comps{grid-template-columns:1fr}}

.charts{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin:18px 0}
.chart{margin:0;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
.chart figcaption{font-weight:700;font-size:13px;margin-bottom:8px;color:var(--ink-2)}
.chart svg{width:100%;height:170px;display:block}
.chart .grid{stroke:#e3eaec;stroke-width:1}
.chart .baseline{stroke:#d98c1f;stroke-width:1.4;stroke-dasharray:4 3}
.chart .tick{font-size:9.5px;fill:var(--muted)}
.chart .tick.base{fill:#d98c1f;font-weight:700}
.chart-keys{display:flex;gap:12px;flex-wrap:wrap;margin-top:9px;font-size:11px;color:var(--muted)}
.chart-keys .key i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.chart-keys b{color:var(--ink-2)}
.chart-note{margin:8px 0 0;font-size:11px;color:var(--muted);line-height:1.45}
.chart.empty{opacity:.65}
.attrib{width:100%;border-collapse:collapse;font-size:12px;margin:6px 0 0}
.attrib th{text-align:left;font-weight:600;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);color:var(--muted)}
.attrib td{padding:6px 10px 6px 0;border-bottom:1px solid #eef3f4;font-variant-numeric:tabular-nums}
.attrib .up{color:#1f8a76;font-weight:700}
.attrib .down{color:#c0392b;font-weight:700}

.verdict-row{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.verdict-chip{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.02em}
.verdict-chip.act{background:#d8f3ec;color:#116b58}
.verdict-chip.verify{background:#fdf0d5;color:#8a5d06}
.verdict-chip.ignore{background:#f8e0dd;color:#a3271b}
.verdict-chip.expired{background:#eceff1;color:#5b6770}
.verdict-chip.noise{background:#eef2f3;color:#6b767d}
.radio-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:2px}
.radio-item.act{border-left:3px solid #1f8a76}
.radio-item.verify{border-left:3px solid #d98c1f}
.radio-item.ignore{border-left:3px solid #c0392b;opacity:.82}
.radio-item.expired,.radio-item.noise{opacity:.62}
.radio-refs{margin:6px 0 0;font-size:12px}
.radio-refs code{background:#eef3f4;border-radius:5px;padding:1px 6px;margin-right:5px;font-size:11px}
.attrib caption{text-align:left;font-weight:700;font-size:13px;padding:12px 0 4px;color:var(--ink-2)}
"""

CSS += r"""
/* Decision desk redesign */
:root{--ink:#10212b;--ink-2:#1b3440;--paper:#f4f7f8;--surface:#ffffff;--line:#d7e1e5;--muted:#6b7d86;--teal:#087f82;--teal-2:#d9f1ef;--saffron:#e2a52b;--red:#d06154;--violet:#6a61c7;--shadow:0 10px 30px rgba(16,33,43,.06)}
*{box-sizing:border-box}body{background:#f5f8f9;color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;letter-spacing:-.01em}.wrap{max-width:1680px;padding:28px 34px 80px}h1,h2,h3{font-family:Inter,ui-sans-serif,system-ui,sans-serif;letter-spacing:-.035em}h1{font-size:42px;font-weight:800;line-height:1.02}h2{font-size:25px;font-weight:750}h3{font-size:16px;font-weight:750}.sub{font-size:14px;line-height:1.55}.status{align-items:center}.flag{border-radius:999px;padding:7px 12px;font-size:12px;letter-spacing:.02em}.clock{font-size:14px;color:var(--muted);font-weight:700}
header{border:0;border-radius:22px;background:linear-gradient(120deg,#102a35 0%,#134c55 52%,#0a7775 100%);color:#fff;padding:30px 34px 28px;align-items:center;box-shadow:var(--shadow)}header .sub{color:#bfe1e0;max-width:58ch}.summary{gap:12px;border:0;margin:18px 0 16px}.metric{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:17px 19px;box-shadow:0 4px 18px rgba(16,33,43,.035)}.metric+.metric{border:1px solid var(--line);padding-left:19px}.metric small{font-size:12px;color:var(--muted);font-weight:650}.metric strong{font-family:Inter,sans-serif;font-size:26px;font-weight:800;letter-spacing:-.04em}.jump{background:#e7eff1;border-radius:12px;padding:8px 11px;margin:0 0 25px;gap:6px}.jump a{border:0;padding:7px 11px;color:var(--ink-2);border-radius:8px}.jump a:hover{background:#fff;color:var(--teal)}
.dashboard-overview{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(330px,.75fr);gap:16px;margin:0 0 30px}.viz-card{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow)}.viz-card h2{margin:0 0 4px}.viz-card .viz-caption{margin:0 0 17px;color:var(--muted);font-size:12px}.viz-head{display:flex;justify-content:space-between;gap:16px;align-items:start}.viz-head .mini-stat{text-align:right}.mini-stat strong{display:block;font-size:25px;font-weight:800;color:var(--teal)}.mini-stat span{font-size:11px;color:var(--muted)}.bar-chart{display:grid;gap:10px}.bar-row{display:grid;grid-template-columns:110px minmax(60px,1fr) 66px;align-items:center;gap:9px;font-size:12px}.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2);font-weight:650}.bar-track{height:9px;background:#edf2f3;border-radius:99px;overflow:hidden}.bar-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),#45b8ad)}.bar-value{text-align:right;font-weight:800;color:var(--ink-2)}.chart-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:16px;color:var(--muted);font-size:11px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;background:var(--teal)}.dot.gold{background:var(--saffron)}.dot.violet{background:var(--violet)}.coverage-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}.coverage-item{display:grid;grid-template-columns:92px 1fr 37px;gap:7px;align-items:center;font-size:12px}.coverage-item b{font-size:12px}.coverage-item small{text-align:right;color:var(--muted);font-weight:700}.coverage-track{height:8px;border-radius:99px;background:#edf2f3;overflow:hidden}.coverage-track i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--violet),#9891eb)}.action-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 30px}.action-card{border:1px solid var(--line);border-radius:15px;padding:14px 16px;background:var(--surface);display:flex;gap:12px;align-items:flex-start;box-shadow:0 4px 18px rgba(16,33,43,.03)}.action-card .action-icon{width:31px;height:31px;border-radius:10px;background:var(--teal-2);display:grid;place-items:center;color:var(--teal);font-weight:800;flex:0 0 auto}.action-card.warn .action-icon{background:#fff1d5;color:#9a6b06}.action-card.hot .action-icon{background:#f6e4e2;color:var(--red)}.action-card strong{display:block;font-size:14px}.action-card span{display:block;font-size:12px;color:var(--muted);margin-top:3px}.section-shell{border-top:0;margin-top:22px;padding:22px;border-radius:18px;background:rgba(255,255,255,.52);border:1px solid rgba(215,225,229,.75)}.guide,.radio,.catalog{margin-top:20px}.guide-head{margin-bottom:16px}.sale-card,.radio-item,.trade,.panel,.team-profile{border-radius:15px;box-shadow:0 5px 22px rgba(16,33,43,.04);border:1px solid var(--line)}.sale-card{border-top:3px solid var(--teal)}.radio{border-top:0}.radio-list{gap:12px}.radio-item{padding:16px}.layout{gap:20px}.sidebar{border:0;background:var(--surface);border:1px solid var(--line);padding:15px;border-radius:16px;height:max-content;position:sticky;top:18px}.team-button{border-radius:9px;border:0;margin:2px 0}.trade{padding:16px 18px}.panel{padding:18px;background:var(--surface);border-top:1px solid var(--line)}
.catalog{border:0;padding:24px;border-radius:18px;background:var(--surface);box-shadow:var(--shadow)}.catalog-head h2{font-size:30px}.catalog-brief{border:0;gap:10px;background:transparent}.catalog-brief>div{border:1px solid var(--line)!important;border-radius:15px;background:#fbfcfc;box-shadow:none}.catalog-map{gap:10px}.set-card{border-radius:13px}.catalog-toolbar{border-radius:14px;top:12px;box-shadow:0 8px 24px rgba(16,33,43,.08)}.catalog-table{border-radius:14px;overflow:auto}.catalog-table table{font-size:13px}.catalog-table th{top:74px}.catalog-table tbody tr{border-left-width:4px}.foot{border:0;text-align:center}

.analytics-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 28px}.analytics-card{background:var(--ink-2);color:#fff;border-radius:15px;padding:16px 18px;position:relative;overflow:hidden}.analytics-card:nth-child(2){background:#13666b}.analytics-card:nth-child(3){background:#4d478d}.analytics-card:nth-child(4){background:#835f20}.analytics-card small{display:block;color:#c5d4d8;font-size:11px;font-weight:650}.analytics-card strong{display:block;font-size:25px;letter-spacing:-.04em;margin-top:5px}.analytics-card span{display:block;color:#d8e4e6;font-size:11px;margin-top:4px}.analytics-card:after{content:"";position:absolute;width:80px;height:80px;border:1px solid rgba(255,255,255,.18);border-radius:50%;right:-28px;bottom:-35px}.priority-panel{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:0 0 30px}.priority-card{background:var(--surface);border:1px solid var(--line);border-radius:15px;padding:15px 17px}.priority-card h3{margin:0 0 5px}.priority-card p{font-size:12px;color:var(--muted);margin:0;line-height:1.45}.priority-card .priority-value{font-size:20px;font-weight:800;color:var(--teal);margin-bottom:5px;display:block}.priority-card.buy .priority-value{color:var(--violet)}.priority-card.hold .priority-value{color:#9a6b06}
@media(max-width:1100px){.analytics-grid{grid-template-columns:1fr 1fr}.priority-panel{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.analytics-grid{grid-template-columns:1fr 1fr}.priority-panel{grid-template-columns:1fr}.analytics-card strong{font-size:21px}}
@media(max-width:1100px){.dashboard-overview{grid-template-columns:1fr}.action-strip{grid-template-columns:1fr 1fr}.coverage-grid{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.wrap{padding:16px 12px 48px}header{padding:23px 20px;border-radius:18px}h1{font-size:32px}.summary{grid-template-columns:1fr 1fr}.metric{padding:14px}.metric strong{font-size:22px}.dashboard-overview{gap:12px}.action-strip{grid-template-columns:1fr}.coverage-grid{grid-template-columns:1fr}.viz-card{padding:16px}.catalog{padding:16px}.catalog-brief{grid-template-columns:1fr 1fr}.bar-row{grid-template-columns:88px 1fr 56px}}
"""

CSS += r"""
.rank-strategy{margin:0 0 30px;background:linear-gradient(135deg,#102a35,#174f56);border-radius:18px;padding:21px;color:#fff;box-shadow:var(--shadow)}.rank-strategy-head{display:flex;justify-content:space-between;gap:18px;align-items:end;margin-bottom:15px}.rank-strategy h2{margin:0;color:#fff}.rank-strategy .sub{color:#c4dfe0;margin:4px 0 0}.rank-gap{display:flex;gap:9px;flex-wrap:wrap}.rank-pill{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);border-radius:10px;padding:9px 12px}.rank-pill strong{display:block;font-size:20px}.rank-pill small{color:#c4dfe0;font-size:11px}.rank-opportunities{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.rank-opportunity{background:#fff;color:var(--ink);border-radius:12px;padding:12px}.rank-opportunity .op-index{color:var(--teal);font-weight:800;font-size:12px}.rank-opportunity b{display:block;margin-top:4px;font-size:14px}.rank-opportunity span{display:block;color:var(--muted);font-size:11px;margin-top:5px}.rank-opportunity .op-gain{color:var(--teal);font-weight:800;font-size:15px}.refresh-diff{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.14);border-radius:99px;padding:5px 9px;font-size:11px;color:#e3f4f3;margin-left:8px}.freshness-note{margin:-18px 0 22px;color:var(--muted);font-size:11px}@media(max-width:1100px){.rank-opportunities{grid-template-columns:repeat(3,1fr)}}@media(max-width:700px){.rank-strategy{padding:16px}.rank-strategy-head{display:block}.rank-gap{margin:13px 0}.rank-opportunities{grid-template-columns:1fr 1fr}.rank-opportunity:last-child{display:none}}

"""


def render_dashboard_overview(data: dict) -> str:
    """Compact decision visualizations using the same live rows as the detailed views."""
    rows = data.get('catalog_rows') or []
    verified = bool(data.get('verified'))
    released = [r for r in rows if r.get('released')]
    sold = sorted([r for r in released if r.get('sold_median') is not None], key=lambda r: r.get('sold_median') or 0, reverse=True)[:7]
    max_sold = max([r.get('sold_median') or 0 for r in sold] or [1])
    sold_bars = ''.join(
        f'<div class="bar-row"><span class="bar-label">{esc(r["ref"])}</span><span class="bar-track"><i class="bar-fill" style="width:{max(5, (r["sold_median"] or 0)/max_sold*100):.1f}%"></i></span><b class="bar-value">{fmt(r["sold_median"])} P</b></div>'
        for r in sold
    ) or '<p class="sub">Todavía no hay liquidaciones individuales suficientes.</p>'
    priced_missing = sorted(
        (r for r in released if verified and r.get('stock') == 0
         and isinstance(r.get('buy_ceiling'), (int, float))
         and isinstance(r.get('sold_median'), (int, float))),
        key=lambda r: r['buy_ceiling'] - r['sold_median'], reverse=True)
    favorable = sum(r['buy_ceiling'] > r['sold_median'] for r in priced_missing)
    compare_max = max((max(r['buy_ceiling'], r['sold_median']) for r in priced_missing[:6]), default=1)
    headroom_rows = ''.join(
        f'<div class="headroom-row"><b>{esc(r["ref"])}</b>'
        f'<div class="headroom-bars"><span style="width:{r["buy_ceiling"] / compare_max * 100:.1f}%" '
        f'title="Nuestro tope: {fmt(r["buy_ceiling"])} P"></span>'
        f'<i style="width:{r["sold_median"] / compare_max * 100:.1f}%" '
        f'title="Mediana liquidada: {fmt(r["sold_median"])} P"></i></div>'
        f'<strong class="{"positive" if r["buy_ceiling"] > r["sold_median"] else "negative"}">'
        f'{r["buy_ceiling"] - r["sold_median"]:+.0f} P</strong></div>'
        for r in priced_missing[:6])
    sets = []
    for name, cards in _group_rows(rows, 'set').items():
        available = [r for r in cards if r.get('released')]
        owned = sum(1 for r in available if isinstance(r.get('stock'), int) and r['stock'] > 0)
        pct = round(owned / len(available) * 100) if available else 0
        sets.append((name, pct, owned, len(available)))
    sets.sort(key=lambda x: x[1], reverse=True)
    coverage = (''.join(f'<div class="coverage-item"><b>{esc(name)}</b><span class="coverage-track"><i style="width:{pct}%"></i></span><small>{owned}/{total}</small></div>' for name,pct,owned,total in sets[:8])
                if verified else '<p class="sub">Conecta la clave y verifica la valoración para medir cobertura real.</p>')
    holders = sorted([r for r in released if r.get('held_by')], key=lambda r: len(r.get('held_by') or []), reverse=True)[:6]
    max_holders = max([len(r.get('held_by') or []) for r in holders] or [1])
    holder_bars = ''.join(f'<div class="bar-row"><span class="bar-label">{esc(r["ref"])}</span><span class="bar-track"><i class="bar-fill" style="width:{max(5, len(r.get("held_by") or [])/max_holders*100):.1f}%;background:linear-gradient(90deg,var(--saffron),#f0c96b)"></i></span><b class="bar-value">{len(r.get("held_by") or [])} equipos</b></div>' for r in holders) or '<p class="sub">Aún no hay posesiones observadas.</p>'
    free = [r for r in released if isinstance(r.get('free'), int) and r['free'] > 0]
    missing = [r for r in released if r.get('stock') == 0] if verified else []
    scarce = sorted([r for r in released if r.get('minted') and r.get('print_run')], key=lambda r: r['minted']/r['print_run'])[:1]
    top_scarce = scarce[0] if scarce else None
    trades = data.get('trades') or []
    best_trade = trades[0] if trades else None
    missing_value = sum((r.get('buy_ceiling') or 0) for r in missing) if verified else None
    sell_value = sum((r.get('sold_median') or 0) for r in free) if verified else None
    sold_values = [r.get('sold_median') for r in released if r.get('sold_median') is not None]
    avg_sold = statistics.mean(sold_values) if sold_values else None
    demand_total = sum(len(r.get('wanted_by') or []) for r in released)
    held_total = sum(len(r.get('held_by') or []) for r in released)
    def action(icon, title, detail, kind=''):
        return f'<article class="action-card {kind}"><span class="action-icon">{icon}</span><div><strong>{title}</strong><span>{detail}</span></div></article>'
    analytics = ('<section class="analytics-grid" aria-label="KPIs estratégicos">'
        f'<article class="analytics-card"><small>Capital potencial en faltantes</small><strong>{fmt(missing_value)} P</strong><span>{"Suma de topes privados de compra" if verified else "Requiere valoración privada verificada"}</span></article>'
        f'<article class="analytics-card"><small>Liquidez de duplicados</small><strong>{fmt(sell_value)} P</strong><span>{"Medianas vendidas de cartas libres" if verified else "Requiere inventario privado verificado"}</span></article>'
        f'<article class="analytics-card"><small>Precio mediano del mercado</small><strong>{fmt(avg_sold)} P</strong><span>Entre referencias con venta confirmada</span></article>'
        f'<article class="analytics-card"><small>Presión de demanda</small><strong>{demand_total} / {held_total}</strong><span>Pedidos frente a posesiones observadas</span></article></section>')
    priorities = ('<section class="priority-panel" aria-label="Lectura estratégica">'
        f'<article class="priority-card"><span class="priority-value">{len(free) if verified else "—"} cartas</span><h3>Vender primero</h3><p>{"Duplicados libres detectados; priorizar comprador real." if verified else "Inventario privado necesario para confirmar copias libres."}</p></article>'
        f'<article class="priority-card buy"><span class="priority-value">{len(missing) if verified else "—"} cartas · {fmt(missing_value)} P</span><h3>Comprar con criterio</h3><p>{"Ordena por valor al recibir y respeta la caja disponible." if verified else "La ausencia en el feed no prueba que falte en tu mano."}</p></article>'
        f'<article class="priority-card hold"><span class="priority-value">{len(released)-len(free)-len(missing) if verified else "—"} cartas</span><h3>Proteger</h3><p>{"Últimas copias y páginas completas tienen coste marginal alto." if verified else "Conecta valoración privada para proteger últimas copias."}</p></article></section>')
    overview = (analytics + priorities + '<section class="dashboard-overview" aria-label="Resumen visual">'

        '<article class="viz-card"><div class="viz-head"><div><h2>Precios que ya se han pagado</h2><p class="viz-caption">Mediana de ventas individuales confirmadas en el feed</p></div>'
        f'<div class="mini-stat"><strong>{len(sold)}</strong><span>referencias con venta</span></div></div><div class="bar-chart">{sold_bars}</div><div class="chart-legend"><span><i class="dot"></i>Precio mediano ejecutado</span><span>Ordenado de mayor a menor</span></div></article>'
        '<article class="viz-card"><div class="viz-head"><div><h2>Cobertura de colección</h2><p class="viz-caption">Cartas publicadas que ya tenemos por colección</p></div></div><div class="coverage-grid">'+(coverage or '<p class="sub">Sin colecciones publicadas.</p>')+'</div><div class="chart-legend"><span><i class="dot violet"></i>Más cobertura</span><span>La vista requiere inventario privado verificado</span></div></article>'
        '<article class="viz-card"><div class="viz-head"><div><h2>Cartas más comunes</h2><p class="viz-caption">Equipos distintos en los que hemos observado cada referencia</p></div></div><div class="bar-chart">'+holder_bars+'</div><div class="chart-legend"><span><i class="dot gold"></i>Posesión observada</span><span>Más equipos = menos exclusividad</span></div></article>'
        '<article class="viz-card"><div class="viz-head"><div><h2>Margen de compra observado</h2><p class="viz-caption">Cartas faltantes: tope privado menos mediana histórica liquidada</p></div>'
        f'<div class="mini-stat"><strong>{favorable}/{len(priced_missing)}</strong><span>con margen positivo</span></div></div>'
        '<div class="headroom-chart">'+(headroom_rows or '<p class="sub">Aún no hay ventas individuales comparables con faltantes valorados.</p>')+'</div>'
        '<div class="chart-legend"><span><i class="dot"></i>Tope nuestro</span><span><i class="dot gold"></i>Precio histórico</span></div>'
        '<p class="chart-note">Una venta pasada no es una oferta activa. Antes de comprar, confirma precio, comisión, valor marginal y caja.</p></article></section>')
    actions = '<section class="action-strip" aria-label="Siguientes decisiones">'
    actions += action('↗', f'{len(free) if verified else "—"} duplicados libres', 'Revisa el precio de venta sugerido en Venta rápida' if verified else 'Requiere inventario privado', 'hot' if free else '')
    actions += action('＋', f'{len(missing) if verified else "—"} cartas faltantes', 'Prioriza las que tengan mayor valor al recibirlas' if verified else 'Requiere valoración privada', 'warn' if missing else '')
    actions += action('◎', f'{"Escasez: "+top_scarce["ref"] if top_scarce else "Sin dato de tirada"}', f'{fmt(top_scarce["minted"])} de {fmt(top_scarce["print_run"])} acuñadas' if top_scarce else 'El feed aún no publica tiradas completas', '')
    actions += '</section>'
    return overview + actions


def _group_rows(rows, key):
    groups = {}
    for row in rows:
        groups.setdefault(row.get(key) or 'Sin colección', []).append(row)
    return groups

def render_rank_strategy(data: dict) -> str:
    leaderboard = (data.get('leaderboard') or {}).get('teams') or []
    ordered = sorted([r for r in leaderboard if isinstance(r.get('score'), (int, float))], key=lambda r: r.get('score', 0), reverse=True)
    pos = next((i for i, r in enumerate(ordered, 1) if r.get('team') == 't15'), None)
    score = next((r.get('score') for r in ordered if r.get('team') == 't15'), data.get('score', {}).get('score'))
    next_score = ordered[pos - 2].get('score') if pos and pos > 1 else None
    leader = ordered[0].get('score') if ordered else None
    gap_next = round(next_score - score, 2) if next_score is not None and score is not None else None
    gap_leader = round(leader - score, 2) if leader is not None and score is not None else None
    pills = (f'<span class="rank-pill"><strong>{pos or "—"}</strong><small>posición actual</small></span>'
             f'<span class="rank-pill"><strong>{fmt(score)}</strong><small>puntos</small></span>'
             f'<span class="rank-pill"><strong>{fmt(gap_next) if gap_next is not None else "—"}</strong><small>para superar al siguiente</small></span>'
             f'<span class="rank-pill"><strong>{fmt(gap_leader) if gap_leader is not None else "—"}</strong><small>para alcanzar al líder</small></span>')
    ops = []
    for i, trade in enumerate((data.get('trades') or [])[:5], 1):
        gain = trade.get('surplus')
        if gain is None:
            gain = trade.get('rank_signal')
        gain_text = f'+{fmt(gain)} P' if gain is not None and trade.get('kind') != 'public-live' else f'{fmt(gain)} P' if gain is not None else '—'
        ops.append(f'<article class="rank-opportunity"><span class="op-index">#{i} · {esc(trade.get("confidence") or "señal")}</span><b>{esc(trade.get("action") or "Acción")} · {esc(trade.get("team") or "")}</b><span>{esc(", ".join(trade.get("give") or []) or "—")} → {esc(", ".join(trade.get("receive") or []) or "—")}</span><span class="op-gain">{gain_text}</span></article>')
    if not ops:
        ops.append('<div class="rank-opportunity">Sin oportunidades ordenadas en este tick.</div>')
    return ('<section class="rank-strategy" id="estrategia-ranking" aria-label="Estrategia para subir posiciones">'
            '<div class="rank-strategy-head"><div><h2>Ruta para subir posiciones</h2><p class="sub">Prioriza acciones por valor neto observado; los puntos futuros dependen de una fórmula que el servidor no publica.</p></div>'
            f'<div class="rank-gap">{pills}</div></div><div class="rank-opportunities">{"".join(ops)}</div></section>')



def strategy_export(data: dict) -> dict:
    """Agent-friendly, evidence-preserving strategy payload."""
    rows = data.get('catalog_rows') or []
    released = [r for r in rows if r.get('released')]
    sold = [r for r in released if r.get('sold_median') is not None]
    free = [r for r in released if isinstance(r.get('free'), int) and r['free'] > 0]
    missing = [r for r in released if r.get('stock') == 0]
    sold_values = [r['sold_median'] for r in sold]
    leaderboard_rows = (data.get('leaderboard') or {}).get('teams') or []
    board_sorted = sorted([r for r in leaderboard_rows if isinstance(r.get('score'), (int, float))], key=lambda r: r.get('score', 0), reverse=True)
    rank_index = next((i for i, r in enumerate(board_sorted, 1) if r.get('team') == 't15'), None)
    current_score = next((r.get('score') for r in board_sorted if r.get('team') == 't15'), data.get('score', {}).get('score'))
    next_score = board_sorted[rank_index - 2].get('score') if rank_index and rank_index > 1 else None
    leader_score = board_sorted[0].get('score') if board_sorted else None
    ranking = {
        'position': rank_index, 'teams_count': len(board_sorted), 'score': current_score,
        'gap_to_next_position': round(next_score - current_score, 2) if next_score is not None and current_score is not None else None,
        'gap_to_leader': round(leader_score - current_score, 2) if leader_score is not None and current_score is not None else None,
        'leaderboard_order': [{'team': r.get('team'), 'score': r.get('score')} for r in board_sorted],
    }
    opportunities = []
    for i, trade in enumerate((data.get('trades') or [])[:20], 1):
        gain = trade.get('surplus') if trade.get('surplus') is not None else trade.get('rank_signal')
        opportunities.append({'rank': i, 'action': trade.get('action'), 'team': trade.get('team'), 'give': trade.get('give') or [], 'receive': trade.get('receive') or [], 'kind': trade.get('kind'), 'confidence': trade.get('confidence'), 'price': trade.get('price'), 'net_or_signal': gain})
    cards = []
    for row in rows:
        minted, print_run = row.get('minted'), row.get('print_run')
        scarcity_ratio = minted / print_run if isinstance(minted, int) and isinstance(print_run, int) and print_run > 0 else None
        holders = row.get('held_by') or []
        wanted = row.get('wanted_by') or []
        holder_count, demand_count = len(holders), len(wanted)
        if not row.get('released'):
            decision = 'unreleased'
        elif isinstance(row.get('free'), int) and row['free'] > 0:
            decision = 'sell_candidate'
        elif isinstance(row.get('stock'), int) and row['stock'] > 0:
            decision = 'hold'
        else:
            decision = 'buy_candidate'
        cards.append({
            'ref': row.get('ref'), 'set': row.get('set'), 'rarity': row.get('rarity'),
            'decision': decision, 'released': bool(row.get('released')),
            'stock': row.get('stock'), 'free': row.get('free'),
            'holder_count_observed': holder_count, 'holders_observed': holders,
            'demand_count_observed': demand_count, 'demanders_observed': wanted,
            'demand_to_holder_ratio': round(demand_count / max(holder_count, 1), 3),
            'minted': minted, 'print_run': print_run,
            'scarcity_ratio_minted_to_print_run': round(scarcity_ratio, 4) if scarcity_ratio is not None else None,
            'sold_median': row.get('sold_median'), 'sold_count': len(row.get('sold_prices') or []),
            'sold_prices': row.get('sold_prices') or [],
            'sell_floor': row.get('sell_floor'), 'buy_ceiling': row.get('buy_ceiling'),
        })
    return {
        'schema': 'team15.strategy.v2', 'schema_compatibility': 'team15.strategy.v1 fields retained', 'team': 't15', 'tick': data.get('tick'),
        'generated_at': data.get('built_at'), 'verified_private_data': bool(data.get('verified')),
        'freshness': {'snapshot_tick': data.get('tick'), 'built_at': data.get('built_at'), 'status': 'live' if data.get('live') else 'public_only', 'cache_max_age_seconds': 12},
        'ranking': ranking,
        'scoring': data.get('scoring') or {},
        'ladder': data.get('ladder') or {},
        'peers': data.get('peers') or {},
        'feed_health': data.get('feed_health') or {},
        'operations': data.get('operations') or {},
        'strategy_health': data.get('strategy_health') or {},
        'score_impacts': (data.get('impact_events') or [])[-20:],
        'duel_history': duel_history.rows(data.get('duel_history') or []),
        'opportunities': opportunities,
        'kpis': {
            'published_cards': len(released), 'catalog_cards': len(rows),
            'owned_references': sum(1 for r in released if isinstance(r.get('stock'), int) and r['stock'] > 0),
            'free_duplicate_references': len(free), 'missing_references': len(missing),
            'confirmed_sale_references': len(sold),
            'average_confirmed_sold_median': round(statistics.mean(sold_values), 2) if sold_values else None,
            'observed_demand_events': sum(len(r.get('wanted_by') or []) for r in released),
            'observed_holder_events': sum(len(r.get('held_by') or []) for r in released),
            'cash': data.get('cash'), 'collection_value': data.get('collection_value'),
            'buy_capacity_after_reserve': data.get('buy_capacity') if data.get('verified') else None,
        },
        'ratios': {
            'demand_pressure': round(sum(len(r.get('wanted_by') or []) for r in released) / max(sum(len(r.get('held_by') or []) for r in released), 1), 3),
            'collection_coverage': round(sum(1 for r in released if isinstance(r.get('stock'), int) and r['stock'] > 0) / max(len(released), 1), 4),
            'free_duplicate_rate': round(len(free) / max(len(released), 1), 4),
            'confirmed_sale_rate': round(len(sold) / max(len(released), 1), 4),
        },
        'definitions': {
            'holder_count_observed': 'Número de equipos distintos con posesión observada en el feed; no demuestra inventario completo.',
            'demand_count_observed': 'Número de equipos distintos que pidieron la referencia en el feed.',
            'scarcity_ratio_minted_to_print_run': 'minted / print_run. Menor proporción implica menos copias emitidas respecto de la tirada.',
            'demand_to_holder_ratio': 'demand_count_observed / max(holder_count_observed, 1). Señal comparativa, no probabilidad de venta.',
            'sold_median': 'Mediana de liquidaciones de una sola carta por efectivo; no incluye lotes ni ofertas sin liquidar.',
            'buy_ceiling': 'Tope de valoración privada de recibir la carta antes de caja y comisiones.',
            'sell_floor': 'Mínimo rentable para desprenderse de una copia libre según pérdida marginal + margen.',
            'ladder_points': 'Puntos de la escalera de vendedores: cuota del rango de precio capturada, 3 mejores tratos por nivel, casilla vacía = 0. Sólo con X-Team-Key.',
            'duel_points': 'Puntos de duelos: cuota del pastel capturada. Una sesión no jugada cuenta cero. Sólo con X-Team-Key.',
            'slots_empty': 'Casillas de escalera sin cubrir en ese nivel. Cota superior: el contador lee el feed local, y GET /api/me manda sobre él.',
            'deals_beyond_scoring': 'Tratos por encima de los 3 que puntúan en ese nivel: munición gastada sin efecto en el score.',
            'empty_slots_weighted_by_level': 'Suma de casillas vacías multiplicadas por el nivel del vendedor. Los niveles altos pesan más en la escalera.',
            'negotiating_per_deal': 'negotiating / deals. Mide rango capturado por trato, que es lo que paga la escalera; el número de tratos por sí solo correlaciona NEGATIVAMENTE con el score.',
            'feed_health.ticks_behind': 'Ticks entre el último evento recogido y el tick actual. Por encima de stale_after_ticks, todo lo derivado del feed describe otro momento del mercado.',
        },
        'cards': cards,
    }



def render_catalog(data: dict) -> str:
    """A decision view of the complete catalog; feed signals stay explicitly observational."""
    rows = data.get('catalog_rows') or []
    verified = bool(data.get('verified'))
    capacity = data.get('buy_capacity') if verified else None
    released = [r for r in rows if r['released']]
    owned = [r for r in released if isinstance(r['stock'], int) and r['stock'] > 0]
    free = [r for r in released if isinstance(r['free'], int) and r['free'] > 0]
    missing = [r for r in released if r['stock'] == 0]
    leads = [r for r in missing if r['held_by']]
    traded = [r for r in released if r.get('sold_prices')]
    sets = {}
    for row in rows:
        sets.setdefault(row['set'], []).append(row)
    parts = ['<section id="catalogo" class="catalog"><div class="catalog-head"><div>',
             '<p class="eyebrow">Inventario · valoración · señales del feed</p>',
             '<h2>Todo el catálogo · Team 15</h2>',
             '<p>Explora qué conservar, qué falta y dónde hay señales para iniciar una negociación. '
             '«Le vimos» y «pidió» son observaciones, no ofertas ni inventario confirmado del rival.</p>',
             '</div></div><div class="catalog-brief" aria-label="Resumen del catálogo">',
             f'<div><small>Cartas publicadas</small><strong>{len(released)} / {len(rows)}</strong><small>disponibles / catálogo total</small></div>',
             f'<div><small>{"En nuestra colección" if verified else "Observadas en t15"}</small><strong>{len(owned)}</strong><small>referencias distintas publicadas</small></div>',
             f'<div><small>{"Duplicados libres" if verified else "Duplicados por verificar"}</small><strong class="brief-alert">{len(free) if verified else "—"}</strong><small>referencias vendibles verificadas</small></div>',
             f'<div><small>Con venta confirmada</small><strong class="brief-action">{len(traded)}</strong><small>referencias con precio ejecutado</small></div>',
             '</div><div class="catalog-map-head"><h3>Mapa por colección</h3>',
             '<p>Selecciona una colección para filtrar la tabla</p></div><div class="catalog-map">']
    for name, cards in sets.items():
        available = [r for r in cards if r['released']]
        have = sum(isinstance(r['stock'], int) and r['stock'] > 0 for r in available)
        pct = round(100 * have / len(available)) if available else 0
        cells = ''.join(f'<span class="{"unreleased" if not r["released"] else "free" if isinstance(r["free"], int) and r["free"] > 0 else "owned" if isinstance(r["stock"], int) and r["stock"] > 0 else "missing"}" '
                        f'title="{esc(r["ref"])}"></span>' for r in cards)
        parts.append(f'<button type="button" class="set-card" data-set="{esc(name)}" aria-pressed="false" '
                     f'aria-label="Filtrar {esc(name)}: {have} de {len(available)} cartas publicadas observadas">'
                     f'<span class="set-card-top"><b>{esc(name)}</b><small>{have} / {len(available)}</small></span>'
                     f'<span class="set-track"><span style="width:{pct}%"></span></span>'
                     f'<span class="set-cells" aria-hidden="true">{cells}</span></button>')
    parts += ['</div><div class="catalog-legend" aria-label="Leyenda del mapa">',
              '<span class="l-owned">Tenemos</span><span class="l-free">Duplicado libre</span>',
              '<span>Falta</span><span class="l-unreleased">No publicada</span></div>',
              '<div class="catalog-toolbar">',
              '<label>Buscar<input id="catalog-search" type="search" placeholder="Carta, barrio o equipo"></label>',
              '<label>Situación<select id="catalog-status"><option value="all">Todas</option><option value="free">Duplicado libre</option><option value="owned">Conservar</option><option value="missing">Falta</option><option value="unreleased">No publicada</option></select></label>',
              '<label>Rareza<select id="catalog-rarity"><option value="all">Todas</option><option value="common">Común</option><option value="uncommon">Poco común</option><option value="rare">Rara</option><option value="epic">Épica</option><option value="legendary">Legendaria</option></select></label>',
              '<label>Ordenar<select id="catalog-sort"><option value="catalog">Catálogo</option><option value="sold">Mayor mediana vendida</option><option value="scarce">Menor proporción acuñada</option><option value="buy">Mayor valor al recibir</option><option value="interest">Más equipos interesados</option><option value="held">Más equipos observados</option><option value="holders">Más equipos que la tienen</option></select></label>',
              '<button type="button" id="catalog-reset">Limpiar</button></div>',
              '<div class="catalog-quick" aria-label="Filtros rápidos">'
              '<button type="button" data-preset="free">Ver duplicados libres</button>'
              '<button type="button" data-preset="missing">Ver faltantes</button>'
              '<button type="button" data-preset="sold">Ordenar por ventas</button>'
              '<button type="button" data-preset="scarce">Ordenar por escasez</button><button type="button" data-preset="holders">Más equipos la tienen</button></div>',
              '<p id="catalog-result" class="catalog-result" role="status"></p>',
              '<div class="catalog-table"><table id="catalog-table"><thead><tr>',
              '<th>Carta</th><th>Decisión</th><th>Escasez real</th><th>Vendida por</th><th>Equipos que la tienen</th><th>Stock / libre</th>',
              '<th>Vender desde</th><th>Comprar hasta</th><th>Señales del feed</th>',
              '</tr></thead><tbody>']
    for index, row in enumerate(rows):
        stock, free_count = row['stock'], row['free']
        if not row['released']:
            state, label = 'unreleased', 'No publicada'
        elif verified and isinstance(free_count, int) and free_count > 0:
            state, label = 'free', 'Vender posible'
        elif isinstance(stock, int) and stock > 0:
            state, label = 'owned', 'Conservar' if verified else 'Observada'
        else:
            state, label = 'missing', 'Falta' if verified else 'No observada'
        floor, ceiling = row['sell_floor'], row['buy_ceiling']
        buy_limit = min(ceiling, capacity) if ceiling is not None and capacity is not None else ceiling
        sell_text = f'<span class="cell-main">{fmt(floor)} P</span>' if floor is not None else '<span class="dim">—</span>'
        if floor is not None and state != 'free':
            sell_text += '<span class="label">Referencia; copia no libre</span>'
        buy_text = f'<span class="cell-main">{fmt(buy_limit)} P</span>' if buy_limit is not None else '<span class="dim">—</span>'
        if not row['released']:
            buy_text = '<span class="dim">No disponible</span>'
        elif ceiling is not None and capacity is not None and capacity < ceiling:
            buy_text += f'<span class="label">Valor {fmt(ceiling)} P · caja {fmt(capacity)} P</span>'
        elif ceiling is not None:
            buy_text += '<span class="label">Tope por valoración</span>'
        minted, print_run = row.get('minted'), row.get('print_run')
        scarcity = minted / print_run if isinstance(minted, int) and isinstance(print_run, int) and print_run > 0 else None
        scarcity_text = (f'<span class="cell-main">{fmt(minted)} / {fmt(print_run)}</span>'
                         f'<span class="label">{scarcity:.1%} de la tirada acuñada · {esc(rarity_label(row["rarity"]))}</span>'
                         f'<span class="scarcity-track"><span style="width:{max(0, min(100, scarcity * 100)):.1f}%"></span></span>') if scarcity is not None else f'<span class="dim">Sin tirada publicada</span><span class="label">{esc(rarity_label(row["rarity"]))}</span>'
        sold = row.get('sold_prices') or []
        sold_text = (f'<span class="cell-main">{fmt(row["sold_median"])} P</span>'
                     f'<span class="label">mediana · {len(sold)} venta{"s" if len(sold) != 1 else ""}</span>'
                     f'<span class="label">Rango {fmt(min(sold))}–{fmt(max(sold))} P · últimas {", ".join(fmt(p) for p in sold[-3:])} P</span>') if sold else '<span class="dim">Sin venta registrada</span>'
        held = ', '.join(row['held_by']) or 'Sin observación'
        wanted = ', '.join(row['wanted_by']) or 'Sin demanda'
        signals = (f'<span title="{esc(held)}"><b>{len(row["held_by"])}</b> le vimos</span>'
                   f'<span title="{esc(wanted)}"><b>{len(row["wanted_by"])}</b> pidieron</span>')
        parts.append(f'<tr data-index="{index}" data-set="{esc(row["set"])}" data-state="{state}" '
                     f'data-rarity="{esc(row["rarity"] or "")}" data-buy="{ceiling if ceiling is not None else -1}" '
                     f'data-interest="{len(row["wanted_by"])}" data-held="{len(row["held_by"])}" '
                     f'data-sold="{row["sold_median"] if sold else -1}" data-scarcity="{scarcity if scarcity is not None else 2}" '
                     f'data-search="{esc(" ".join([row["ref"], row["set"], *row["held_by"], *row["wanted_by"]]).lower())}">'
                     f'<td><b>{esc(row["ref"])}</b><span class="label">{esc(row["set"])}</span></td>'
                     f'<td><span class="catalog-state {state}">{label}</span></td><td>{scarcity_text}</td><td>{sold_text}</td>'
                     f'<td><span class="cell-main">{len(row["held_by"])} equipos</span><span class="label">{esc(held) if held != "Sin observación" else "Sin posesión observada"}</span></td>'
                     f'<td><span class="catalog-count">{esc(stock)}</span> / {esc(free_count)}</td>'
                     f'<td>{sell_text}</td><td>{buy_text}</td><td><div class="catalog-signals">{signals}</div>'
                     f'<span class="label">Le vimos: {esc(held)}<br>Pidieron: {esc(wanted)}</span></td></tr>')
    parts += ['</tbody></table><div id="catalog-empty" class="catalog-empty" hidden>Sin cartas para estos filtros.</div></div>',
              '<p class="catalog-hint">«Vendida por» usa sólo liquidaciones de una carta por efectivo observadas en el feed; los lotes no permiten atribuir un precio individual. Menor porcentaje acuñado significa menos copias emitidas respecto a la tirada, no menos cartas actualmente a la venta. Los topes son límites privados antes de posibles comisiones. '
              + (f'Capacidad con reserva: {fmt(capacity)} P.' if capacity is not None else 'Sin valoración privada verificada: topes desconocidos.')
              + '</p></section>']
    return ''.join(parts)


CATALOG_JS = """
const catalogTable=document.querySelector('#catalog-table tbody');
const catalogRows=Array.from(catalogTable.querySelectorAll('tr'));
const catalogSearch=document.getElementById('catalog-search');
const catalogStatus=document.getElementById('catalog-status');
const catalogRarity=document.getElementById('catalog-rarity');
const catalogSort=document.getElementById('catalog-sort');
const catalogSets=Array.from(document.querySelectorAll('.set-card'));
let catalogSet=sessionStorage.getItem('t15.catalog.set')||'';
catalogSearch.value=sessionStorage.getItem('t15.catalog.search')||'';
catalogStatus.value=sessionStorage.getItem('t15.catalog.status')||'all';
catalogRarity.value=sessionStorage.getItem('t15.catalog.rarity')||'all';
catalogSort.value=sessionStorage.getItem('t15.catalog.sort')||'catalog';
function updateCatalog(){
  const q=catalogSearch.value.trim().toLocaleLowerCase();
  const sort=catalogSort.value;
  catalogRows.sort((a,b)=>{
    if(sort==='catalog')return Number(a.dataset.index)-Number(b.dataset.index);
    const key=sort==='scarce'?'scarcity':sort;
    const av=Number(a.dataset[key]),bv=Number(b.dataset[key]);
    return (sort==='scarce'?av-bv:bv-av)||Number(a.dataset.index)-Number(b.dataset.index);
  });
  catalogRows.forEach(r=>catalogTable.appendChild(r));
  let shown=0;
  catalogRows.forEach(r=>{
    const match=(!q||r.dataset.search.includes(q))&&(!catalogSet||r.dataset.set===catalogSet)
      &&(catalogStatus.value==='all'||r.dataset.state===catalogStatus.value)
      &&(catalogRarity.value==='all'||r.dataset.rarity===catalogRarity.value);
    r.hidden=!match;if(match)shown++;
  });
  catalogSets.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.set===catalogSet)));
  document.getElementById('catalog-result').textContent=shown+' de '+catalogRows.length+' cartas · '+(catalogSet||'todas las colecciones');
  document.getElementById('catalog-empty').hidden=shown!==0;
  sessionStorage.setItem('t15.catalog.search',catalogSearch.value);
  sessionStorage.setItem('t15.catalog.status',catalogStatus.value);
  sessionStorage.setItem('t15.catalog.rarity',catalogRarity.value);
  sessionStorage.setItem('t15.catalog.sort',catalogSort.value);
  sessionStorage.setItem('t15.catalog.set',catalogSet);
}
catalogSets.forEach(b=>b.addEventListener('click',()=>{catalogSet=catalogSet===b.dataset.set?'':b.dataset.set;updateCatalog();}));
[catalogStatus,catalogRarity,catalogSort].forEach(el=>el.addEventListener('change',updateCatalog));
catalogSearch.addEventListener('input',updateCatalog);
document.getElementById('catalog-reset').addEventListener('click',()=>{
  catalogSet='';catalogSearch.value='';catalogStatus.value='all';catalogRarity.value='all';catalogSort.value='catalog';updateCatalog();catalogSearch.focus();
});
document.querySelectorAll('[data-preset]').forEach(button=>button.addEventListener('click',()=>{
  const preset=button.dataset.preset;
  if(preset==='free'||preset==='missing'){catalogStatus.value=preset;catalogSort.value='catalog';}
  if(preset==='sold'){catalogStatus.value='all';catalogSort.value='sold';}
  if(preset==='scarce'){catalogStatus.value='all';catalogSort.value='scarce';}
  if(preset==='holders'){catalogStatus.value='all';catalogSort.value='holders';}
  updateCatalog();
}));
updateCatalog();
"""


def _bar(value, total, label, best=None, best_team=None) -> str:
    """Barra de un componente del score. `best` marca dónde está el mejor del juego."""
    pct = max(0.0, min(100.0, (value or 0) / total * 100)) if total else 0.0
    mark = ''
    if best is not None and total:
        mark = (f'<span class="bar-best" style="left:{max(0.0, min(100.0, best / total * 100)):.1f}%" '
                f'title="mejor del juego: {esc(best_team)} con {fmt(best)}"></span>')
    return (f'<div class="comp"><div class="comp-head"><span>{esc(label)}</span>'
            f'<strong>{fmt(value)} <small>/ {fmt(total)}</small></strong></div>'
            f'<div class="bar"><span class="bar-fill" style="width:{pct:.1f}%"></span>{mark}</div></div>')


def render_monitor(data: dict) -> str:
    """Monitor de ranking: qué componente mueve puntos, qué casillas de escalera están
    vacías y si los datos de los que todo esto depende siguen vivos.

    Orden deliberado: primero de dónde salen los puntos, después qué hueco los da, y al
    final el aviso de frescura, porque una cifra de un almacén parado se lee igual de
    bien que una buena."""
    block = data.get('scoring') or {}
    ladder = data.get('ladder') or {}
    peers = data.get('peers') or {}
    health = data.get('feed_health') or {}
    comps = block.get('components') or {}
    parts = ['<section id="monitor" class="monitor"><div class="section-head"><div>',
             '<h2>Monitor de ranking</h2>',
             '<p class="sub">El servidor aporta 60 puntos: 30 de negociación y 30 de mercado '
             '(22,5 test + 7,5 tratos de terceros). Jueces aportan otros 40. '
             'El valor de colección y los bonos de página no puntúan por tenencia.</p>',
             '</div></div>']

    if health.get('status') and health['status'] != 'fresh':
        behind = health.get('ticks_behind')
        parts.append('<div class="warning">Almacén del feed '
                     + ('sin datos' if health['status'] == 'no_data'
                        else f'parado: último tick {esc(health.get("last_tick"))}, '
                             f'{esc(behind)} ticks por detrás')
                     + '. Los precios, los rivales y el playbook se calculan sobre este fichero. '
                     + 'Arranca el recolector: <code>python3 feed_stream.py --collect</code></div>')

    parts.append('<div class="comps">')
    for key, label in (('negotiating', 'Negociación'), ('market', 'Mercado')):
        c = comps.get(key) or {}
        parts.append(_bar(c.get('ours'), c.get('weight_in_total') or 30.0, label,
                          c.get('best'), c.get('best_team')))
    parts.append('</div>')

    rows = []
    for key, label, why in (
            ('ladder_points', 'Escalera de vendedores', 'cuota del rango de precio capturada, 3 mejores por nivel'),
            ('duel_points', 'Duelos', 'cuota del pastel; una sesión no jugada cuenta cero'),
            ('mm_points', 'Creación de mercado', 'valor creado entre otros equipos en nuestro venue'),
            ('neg_points', 'Valor en trades', 'con otros equipos, a valores privados')):
        value = block.get(key)
        rows.append(f'<tr><th>{esc(label)}</th><td class="num">'
                    + (f'{fmt(value)}' if value is not None else '<span class="missing">sin clave</span>')
                    + f'</td><td class="why">{esc(why)}</td></tr>')
    parts += ['<table class="breakdown"><caption>De dónde salen los puntos</caption>',
              '<tbody>', *rows, '</tbody></table>']
    if block.get('missing_from_api'):
        parts.append('<p class="sub">Sin <code>X-Team-Key</code> no llegan '
                     + esc(', '.join(block['missing_from_api']))
                     + ', y sin ellos no se distingue «la escalera está vacía» de «no jugamos duelos».</p>')

    if ladder.get('levels'):
        parts += ['<div class="section-head"><div><h3>Casillas de escalera</h3>',
                  f'<p class="sub">Hasta {esc(ladder.get("empty_slots_available"))} huecos sin liquidación observada '
                  f'(peso {esc(ladder.get("empty_slots_weighted_by_level"))} contando el nivel). '
                  'El feed local puede estar incompleto; confirmar con score privado. Los niveles altos pesan más.</p></div></div>',
                  '<div class="ladder">']
        for level in ladder['levels']:
            state = 'off' if not level['available'] else ('done' if not level['slots_empty'] else 'open')
            pips = ''.join(f'<span class="pip{" on" if i < level["slots_filled"] else ""}"></span>'
                           for i in range(level['slots_total']))
            extra = (f'<small class="waste">{level["deals_beyond_scoring"]} tratos de más</small>'
                     if level['deals_beyond_scoring'] else '')
            buys = ', '.join(level.get('buys_rarities') or []) or '—'
            parts.append(f'<article class="slot {state}"><div class="slot-top">'
                         f'<h4>{esc(level.get("name") or level["dealer"])}</h4>'
                         f'<small>nivel {esc(level["level"])}</small></div>'
                         f'<div class="pips">{pips}</div>'
                         f'<p class="sub">{esc(level["slots_filled"])}/{esc(level["slots_total"])} · compra {esc(buys)}'
                         + ('' if level['available'] else ' · no disponible aún') + f'</p>{extra}</article>')
        parts.append('</div>')

    ours = peers.get('ours') or {}
    if ours:
        best = (peers.get('efficiency_ranking') or [{}])[0]
        parts += ['<table class="breakdown"><caption>Puntos de negociación por trato</caption><tbody>',
                  f'<tr><th>Nosotros</th><td class="num">{fmt(ours.get("negotiating_per_deal"))}</td>'
                  f'<td class="why">{esc(ours.get("negotiating"))} puntos en {esc(ours.get("deals"))} tratos'
                  f' · puesto {esc(peers.get("our_efficiency_position"))} de {esc(peers.get("teams_measured"))}</td></tr>',
                  f'<tr><th>Mejor del juego</th><td class="num">{fmt(best.get("negotiating_per_deal"))}</td>'
                  f'<td class="why">{esc(best.get("team"))} con {esc(best.get("deals"))} tratos'
                  f' y {esc(best.get("album_filled"))} cartas de álbum</td></tr>',
                  '</tbody></table>',
                  '<p class="sub">Más tratos no es mejor: sobre los equipos activos la correlación de '
                  '<code>deals</code> con el score es negativa. Lo que paga es el rango capturado por trato.</p>']
    parts.append('</section>')
    return ''.join(parts)


VERDICT_LABEL = {"act": ("Actuar", "act"), "verify": ("Verificar", "verify"),
                 "ignore": ("Ignorar", "ignore"), "expired": ("Plazo vencido", "expired"),
                 "noise": ("Sin efecto", "noise")}


def render_trend(data: dict) -> str:
    """Trayectoria: de dónde venimos, qué subió los puntos y la carrera con los vecinos.

    Las gráficas están elegidas por lo que deciden, no por lo que se ve bien:
      1. score y puesto, para saber si vamos hacia arriba
      2. los dos componentes sobre su escala real de 0 a 30, donde se ve que 7.50 de
         mercado es el suelo y no una cifra normal
      3. negociación por trato, con la referencia del mejor del juego: es la métrica que
         paga la escalera, y la que nos tiene en el puesto 11
      4. la carrera contra el equipo de arriba y el de abajo, que son los que mueven el puesto
    """
    rows = data.get('history') or []
    summary = data.get('history_summary') or {}
    race = data.get('rank_race') or {}
    peers = data.get('peers') or {}
    if summary.get('status') != 'ok':
        return ('<section id="tendencia" class="monitor"><div class="section-head"><div>'
                '<h2>Trayectoria</h2><p class="sub">Hace falta más de una muestra. '
                'El panel guarda una por tick en <code>bazaar-kit/data/score_history.jsonl</code>; '
                'déjalo abierto y la serie se llena sola.</p></div></div></section>')

    best_eff = (peers.get('efficiency_ranking') or [{}])[0]
    cards = [
        charts.line_chart({'score': history.series(rows, 'score')},
                          title='Score', caption=(
                              f'De {fmt(summary.get("score_from"))} a {fmt(summary.get("score_to"))} '
                              f'en {esc(summary.get("span_ticks"))} ticks. '
                              f'Tendencia reciente: {fmt(summary.get("recent_trend"))} por muestra.')),
        charts.line_chart({'puesto': history.series(rows, 'rank')}, invert=True, value_fmt='{:.0f}',
                          title='Puesto en el ranking',
                          caption=f'Del {esc(summary.get("rank_from"))} al {esc(summary.get("rank_to"))}. '
                                  'Eje invertido: arriba es mejor.'),
        charts.line_chart({'negociación': history.series(rows, 'negotiating'),
                           'mercado': history.series(rows, 'market')}, lo=0, hi=30,
                          baseline=7.5, baseline_label='suelo de mercado',
                          title='Los dos componentes, sobre 30',
                          caption='30 puntos cada uno. Un venue sin trades puntúa 7.50 sea board o auto, '
                                  'así que una línea plana en 7.50 es el suelo, no un resultado.'),
        charts.line_chart({'duelos': history.series(rows, 'duel_points'),
                           'dealers': history.series(rows, 'ladder_points'),
                           'trades': history.series(rows, 'neg_points'),
                           'terceros en v15': history.series(rows, 'mm_points')},
                          title='Motores de puntos privados',
                          caption='Serie sólo cuando /api/me entrega el desglose. Comparar cambios entre ticks; '
                                  'un movimiento simultáneo no demuestra qué acción lo causó.'),
        charts.line_chart({'caja': history.series(rows, 'cash'),
                           'valor de colección': history.series(rows, 'collection_value')},
                          title='Caja y colección',
                          caption='Son recursos y riesgos operativos. Su nivel final no puntúa por sí mismo.'),
        charts.line_chart({'nuestra': history.efficiency_series(rows)}, value_fmt='{:.2f}',
                          baseline=best_eff.get('negotiating_per_deal'),
                          baseline_label=f'mejor: {esc(best_eff.get("team"))}',
                          title='Puntos de negociación por trato',
                          caption='Lo que paga la escalera es la cuota del rango capturada. '
                                  'Sube regateando mejor, no cerrando más tratos.'),
    ]
    if race.get('series'):
        cards.append(charts.line_chart(race['series'], title='Carrera con los vecinos',
                                       caption='El de arriba y el de abajo son los que mueven el puesto. '
                                               'Elegidos por el estado actual, no por el inicial.'))

    parts = ['<section id="tendencia" class="monitor"><div class="section-head"><div>',
             '<h2>Trayectoria</h2>',
             f'<p class="sub">{esc(summary.get("samples"))} muestras guardadas, una por tick, '
             'en <code>bazaar-kit/data/score_history.jsonl</code>.</p>',
             '</div></div><div class="charts">', *cards, '</div>']

    impacts = list(reversed(data.get('impact_events') or []))[:8]
    if impacts:
        parts += ['<div class="impact-head"><div><h3>Qué movió el score</h3>',
                  '<p>Lecturas entre ticks; la etiqueta describe la evidencia disponible, no atribuye una causa única.</p></div>',
                  '<span>Últimos 8 cambios</span></div><div class="impact-grid">']
        for event in impacts:
            delta = event['delta']
            amount = delta['score'] or 0
            cls = 'positive' if amount > 0 else 'negative'
            if event['label'] in ('Ponderación de ronda', 'Cambio de ronda'):
                cls = 'round-shift'
            drivers = []
            for key, label in (('negotiating', 'Negociación'), ('market', 'Mercado'),
                               ('duel_points', 'Duelos raw'), ('ladder_points', 'Dealers raw'),
                               ('neg_points', 'Tratos raw'), ('mm_points', 'Terceros raw'),
                               ('deals', 'Tratos')):
                value = delta.get(key)
                if isinstance(value, (int, float)) and value:
                    drivers.append(f'<span>{label} <b>{value:+.2f}</b></span>')
            parts.append(
                f'<article class="impact-card {cls}"><div class="impact-top">'
                f'<span class="impact-type">{esc(event["label"])}</span>'
                f'<strong>{amount:+.2f}</strong></div>'
                f'<div class="impact-ticks">tick {esc(event["from_tick"])} → {esc(event["to_tick"])}</div>'
                f'<div class="impact-drivers">{"".join(drivers) or "Sin desglose disponible"}</div>'
                f'<p>{esc(event["basis"])}</p></article>')
        parts.append('</div>')
    parts.append('</section>')
    return ''.join(parts)


CSS += """
.ops{margin:0 0 22px;background:#fff;border:1px solid var(--line);border-radius:19px;padding:20px 22px;box-shadow:var(--shadow)}
.ops-heading{display:flex;justify-content:space-between;gap:14px;align-items:baseline;border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px}
.ops-heading h2{margin:0}.ops-heading p{color:var(--muted);margin:4px 0 0}.ops-heading>span{font-weight:700;color:var(--teal);white-space:nowrap}
.ops-grid{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:18px}.ops-grid article+article{border-left:1px solid var(--line);padding-left:18px}
.ops-grid h3{margin:0 0 9px}.ops-grid p{color:var(--muted);font-size:12px;line-height:1.45;margin:8px 0}
.ops-alerts{list-style:none;padding:0;margin:0;display:grid;gap:8px}.ops-alert{display:grid;gap:3px;border-left:3px solid var(--teal);padding:7px 9px;background:#f2f7f7}
.ops-alert.critical{border-color:var(--red);background:#fff1ef}.ops-alert.high{border-color:var(--saffron);background:#fff8e9}
.ops-alert b{font-size:13px}.ops-alert span{font-size:11px;color:var(--muted)}.ops-empty{border:1px dashed var(--line);padding:12px}
.duel-lane{border-bottom:1px solid var(--line);padding:8px 0}.duel-lane>div:first-child{display:flex;justify-content:space-between;gap:8px;font-size:11px}.duel-lane b{font-size:12px}.duel-lane span,.duel-lane small{color:var(--muted)}
.duel-track{height:5px;background:#e4edf0;border-radius:9px;overflow:hidden;margin:6px 0}.duel-track i{display:block;height:100%;background:var(--teal)}.duel-lane.close_now .duel-track i{background:var(--red)}.duel-lane.unsafe .duel-track i{background:var(--saffron)}
.cash-stack{height:13px;background:#ccebe5;display:flex;border-radius:10px;overflow:hidden;margin:9px 0}.cash-stack i{height:100%;display:block}.cash-stack .committed{background:var(--red)}.cash-stack .reserved{background:var(--saffron)}
@media(max-width:1050px){.ops-grid{grid-template-columns:1fr 1fr}.ops-primary{grid-column:1/-1}.ops-grid article+article{border-left:0;padding-left:0}.ops-detail:last-child{border-left:1px solid var(--line);padding-left:18px}}
@media(max-width:680px){.ops-grid{display:block}.ops-grid article{padding:12px 0!important;border:0!important;border-bottom:1px solid var(--line)!important}.ops-heading{display:block}.ops-heading>span{display:block;margin-top:8px}}
"""

CSS += """
/* Hallmark · pre-emit critique: P5 H4 E4 S5 R4 V4
 * macrostructure: Operations ledger · genre: modern-minimal · theme: Cobalt · tone: focused
 * anchor hue: teal with restrained amber signal
 */
:root{--paper:#f3f5f4;--surface:#fff;--ink:#142b36;--ink-2:#294550;--line:#d7e0df;--muted:#5b7078;--teal:#087c7c;--saffron:#b77915;--red:#a8483c;--blue:#e7f1f0;--signal:#e9a83c;--night:#102d3a;--night-2:#1a4050;--soft:#eff5f4;--shadow:none}
html,body{overflow-x:clip;scroll-behavior:smooth}body{background:var(--paper);color:var(--ink);font-family:Arial,Helvetica,sans-serif;letter-spacing:0}.wrap{max-width:1520px;padding:20px 28px 72px}
header{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:0;padding:18px 24px;box-shadow:none;align-items:center}header .sub{color:var(--muted);margin:5px 0 0}h1,h2,h3{font-family:Arial,Helvetica,sans-serif;letter-spacing:-.025em}h1{font-size:26px;font-weight:800}h2{font-size:22px}header .flag{border-radius:3px}.flag.live{background:var(--teal)}.clock{font-size:13px}
.summary{display:none}.jump{position:sticky;top:0;z-index:8;margin:0 0 14px;padding:8px 2px;border-radius:0;background:var(--paper);border-bottom:1px solid var(--line);gap:2px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin}.jump a{display:inline-flex;align-items:center;white-space:nowrap;padding:8px 12px;border-radius:3px;font-size:12px}.jump a:hover,.jump a:focus-visible{background:var(--blue)}
.command-deck{background:var(--night);color:#fff;margin:0 0 16px;padding:25px 28px 20px;display:grid;gap:20px}.deck-lead{display:grid;grid-template-columns:minmax(260px,.75fr) minmax(330px,1.25fr);gap:28px;align-items:end}.deck-kicker{display:block;color:#b3d6d3;font-size:12px;font-weight:700;letter-spacing:.02em}.deck-score>strong{display:block;font-size:clamp(62px,8vw,112px);line-height:.94;letter-spacing:-.08em;font-variant-numeric:tabular-nums;margin:9px 0 3px}.deck-score-label{color:#b8d1d2;font-size:12px}.deck-rank{border-top:1px solid #42616d;margin-top:21px;padding-top:12px;font-size:14px}.deck-rank b{font-size:24px}.deck-rank span{display:block;color:#f3c775;font-size:12px;margin-top:3px}.deck-title{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:15px}.deck-title h2{font-size:16px;color:#fff;margin:0}.deck-title a{font-size:12px;color:#a8dbd5;text-decoration:underline;text-underline-offset:3px;white-space:nowrap}.deck-race{padding:5px 0 0}.race-row{display:grid;grid-template-columns:36px minmax(0,1fr) 45px;align-items:center;gap:11px;margin:10px 0;font-size:12px;font-variant-numeric:tabular-nums}.race-row b{text-align:right;font-size:13px}.race-row.self{color:#f3c775}.race-track{height:10px;background:#31515e}.race-track i{display:block;height:100%;background:#77949f}.race-row.self .race-track i{background:var(--signal)}.deck-race p{color:#a9c0c5;font-size:11px;margin:14px 0 0}.deck-lower{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(250px,.7fr);gap:28px;padding-top:18px;border-top:1px solid #42616d}.deck-sources{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}.deck-sources .deck-title{grid-column:1/-1}.deck-component{min-width:0}.deck-line{display:flex;justify-content:space-between;gap:10px;font-size:14px}.deck-line span small{display:block;color:#acc6c9;font-size:11px;margin-top:3px}.deck-line b{white-space:nowrap;font-variant-numeric:tabular-nums}.deck-line em{font-style:normal;color:#acc6c9;font-weight:400}.deck-track{height:9px;background:#31515e;margin:11px 0 0}.deck-track i{display:block;height:100%;background:#44aca2}.deck-component:nth-child(3) .deck-track i{background:var(--signal)}.deck-capital{border-left:1px solid #42616d;padding-left:24px}.deck-capital-value{font-size:31px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}.deck-capital-value span{display:block;color:#c1d5d5;font-size:11px;font-weight:400;margin-top:6px}.deck-capital p{color:#a9c0c5;font-size:11px;margin-bottom:0}.deck-decision{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:20px;align-items:center;background:var(--night-2);margin:0 -28px -20px;padding:18px 28px}.deck-decision h2{font-size:19px;margin:4px 0}.deck-decision p{color:#c5d9dc;font-size:12px;margin:0;max-width:74ch}.deck-decision>a{background:var(--signal);color:var(--night);font-weight:800;font-size:12px;padding:10px 14px;text-decoration:none;white-space:nowrap}.deck-fresh{font-size:11px;color:#a8dbd5;white-space:nowrap}
.ops,.monitor,.viz-card,.chart,.sale-card,.radio-item,.panel,.trade,.slot,.rank-strategy,.catalog-brief,.set-card{box-shadow:none;border-radius:3px}.ops,.monitor{padding:22px 25px;background:var(--surface);border:1px solid var(--line);margin-bottom:16px}.monitor .bar{background:var(--soft)}.monitor .bar-fill{background:var(--teal)}.monitor .bar-best{background:var(--signal)}.monitor .slot{background:var(--soft);border:1px solid var(--line)}.monitor .slot.open{border-color:var(--saffron)}.monitor .slot.done{border-color:var(--teal)}.monitor .pip{background:#d6e3e1}.monitor .pip.on{background:var(--teal)}.monitor .breakdown th,.monitor .breakdown td{border-color:var(--line)}.monitor .breakdown .why{opacity:1;color:var(--muted)}.chart{padding:18px 18px 12px}.chart figcaption{font-size:15px;color:var(--ink)}.chart svg{height:190px}.chart .grid{stroke:#dbe6e4}.chart .baseline{stroke:var(--saffron)}.chart .tick.base{fill:var(--saffron)}.charts{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.viz-card{padding:22px}.section-shell{border-radius:3px;background:var(--surface)}.action-card{box-shadow:none;border-radius:3px}.sidebar{border-radius:3px}.catalog-toolbar{top:47px}.sale-card{border-top-width:3px}
.headroom-chart{display:grid;gap:11px}.headroom-row{display:grid;grid-template-columns:65px minmax(0,1fr) 54px;gap:10px;align-items:center;font-size:12px}.headroom-row strong{text-align:right;font-variant-numeric:tabular-nums}.headroom-row strong.positive{color:var(--teal)}.headroom-row strong.negative{color:var(--red)}.headroom-bars{display:grid;gap:3px}.headroom-bars span,.headroom-bars i{height:5px;display:block;min-width:2px}.headroom-bars span{background:var(--teal)}.headroom-bars i{background:var(--saffron)}
@media(max-width:900px){.deck-lead{grid-template-columns:1fr 1fr}.deck-lower{grid-template-columns:1fr}.deck-capital{border:0;border-top:1px solid #42616d;padding:15px 0 0}.dashboard-overview,.charts{grid-template-columns:1fr}.deck-decision{grid-template-columns:1fr auto}.deck-fresh{grid-column:1/-1}}
@media(max-width:620px){.wrap{padding:10px 10px 45px}header{padding:15px;display:block}header .status{margin-top:12px}h1{font-size:22px}.command-deck{padding:20px 17px 17px}.deck-lead,.deck-sources,.deck-lower{display:block}.deck-race{border-top:1px solid #42616d;margin-top:18px;padding-top:17px}.deck-component+.deck-component{margin-top:18px}.deck-capital{margin-top:20px}.deck-decision{margin:0 -17px -17px;padding:18px 17px;display:block}.deck-decision>a{display:inline-block;margin-top:14px}.deck-fresh{display:block;margin-top:12px}.ops,.monitor{padding:16px}.coverage-grid{grid-template-columns:1fr}.dashboard-overview,.charts{display:block}.dashboard-overview>*+*,.charts>*+*{margin-top:12px}.viz-head{display:block}.viz-head .mini-stat{text-align:left;margin:9px 0}.bar-row{grid-template-columns:76px minmax(0,1fr) 60px}.catalog-toolbar{top:45px}.catalog-brief{grid-template-columns:1fr 1fr}}
@media(prefers-reduced-motion:reduce){html,body{scroll-behavior:auto}*{transition-duration:.01ms!important;animation-duration:.01ms!important}}
"""

CSS += """
/* Team 15 · Sunday visual pass: rounded instruments, restrained depth and one night-market gradient. */
:root{--canvas:#edf3f4;--canvas-end:#f8faf9;--card:#fff;--night:#143542;--night-2:#214d58;--market:#33a79b;--amber:#edb25b;--coral:#ca695a;--lavender:#8d89c5;--slate:#9bb1ba;--card-shadow:0 10px 28px rgba(24,59,71,.065)}
body{background:linear-gradient(180deg,var(--canvas),var(--canvas-end) 520px);background-attachment:fixed}.wrap{max-width:1580px}
header{border-radius:18px;background:var(--card);box-shadow:var(--card-shadow);padding:22px 28px}.jump{border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.96);margin:12px 0 16px;padding:6px;box-shadow:0 5px 18px rgba(24,59,71,.04);backdrop-filter:blur(12px)}.jump a{border-radius:9px}.jump a:hover,.jump a:focus-visible{background:var(--blue)}
.command-deck{border-radius:22px;background:linear-gradient(125deg,var(--night) 0%,var(--night-2) 60%,#246b6d 100%);box-shadow:0 18px 36px rgba(14,48,58,.17);overflow:hidden}.deck-decision{background:rgba(255,255,255,.075)}.deck-decision>a{border-radius:10px;background:var(--amber)}.deck-track,.race-track{border-radius:20px;overflow:hidden}.deck-track i,.race-track i{border-radius:20px}.deck-race p,.deck-capital p{color:#d3e4e4}
.pulse{margin:0 0 16px}.pulse-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:0 2px 13px}.pulse-head h2{margin:0 0 4px}.pulse-head p,.pulse-head>span{font-size:12px;color:var(--muted);margin:0}.pulse-head>span{text-align:right;max-width:42ch}.pulse-grid{display:grid;grid-template-columns:1.25fr .9fr .95fr;gap:14px}.pulse-card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:20px 21px;box-shadow:var(--card-shadow);display:flex;flex-direction:column}.pulse-card-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:17px}.pulse-card-head h3{font-size:16px;margin:0}.pulse-card-head>strong{font-size:22px;color:var(--ink);font-variant-numeric:tabular-nums}.pulse-card-head small{font-size:12px;color:var(--muted);font-weight:400}.session-row{margin:0 0 14px}.session-row>div:first-child{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:12px}.session-row b{font-size:13px}.session-row span{color:var(--muted);text-align:right}.session-track{height:10px;display:flex;border-radius:50px;overflow:hidden;background:var(--soft);margin-top:7px}.session-track span{height:100%;display:block}.session-track .won,.session-key i.won{background:var(--market)}.session-track .flat{background:var(--slate)}.session-track .lost,.session-key i.lost{background:var(--coral)}.session-track .missed,.session-key i.missed{background:var(--amber)}.session-key{display:flex;flex-wrap:wrap;gap:11px;color:var(--muted);font-size:11px;margin:5px 0}.session-key span{display:inline-flex;align-items:center;gap:5px}.session-key i{width:8px;height:8px;display:inline-block;border-radius:2px}.pulse-note{font-size:12px;color:var(--muted);line-height:1.5;margin:auto 0 0;padding-top:13px}.pulse-empty{font-size:12px;color:var(--muted)}.market-pulse{background:linear-gradient(160deg,#fff 48%,#eaf6f4 100%)}.market-hero{display:flex;align-items:baseline;gap:10px;margin:0 0 12px}.market-hero b{font-size:47px;line-height:1;color:var(--teal);letter-spacing:-.06em}.market-hero span{font-size:12px;color:var(--muted);max-width:16ch}.market-pulse>a{font-size:12px;margin-top:12px;font-weight:700;text-underline-offset:3px}.system-pulse{background:linear-gradient(160deg,#fff 55%,#f1f4fb 100%)}.system-mark{color:var(--market)!important;font-size:13px!important}.system-line{display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);font-size:12px}.system-line b{text-align:right;color:var(--ink-2);font-size:11px}.system-line span{color:var(--muted)}
.ops,.monitor,.viz-card,.chart,.sale-card,.radio-item,.panel,.trade,.slot,.rank-strategy,.catalog-brief,.set-card,.action-card,.sidebar{border-radius:17px;box-shadow:var(--card-shadow)}.ops,.monitor{padding:23px 26px}.viz-card,.chart{border-radius:18px}.chart svg{border-radius:10px}.sale-card{border-top-width:4px}.radio-item{border-left-width:4px}.rank-strategy{border-radius:20px}.rank-opportunity{border-radius:12px}.catalog-toolbar{border-radius:12px}.set-card:hover,.set-card:focus-visible{transform:translateY(-2px)}.bar-track,.coverage-track,.headroom-bars span,.headroom-bars i,.cash-stack,.duel-track,.set-track{border-radius:50px}.chart .baseline{stroke:var(--amber)}
@media(max-width:1040px){.pulse-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.system-pulse{grid-column:1/-1}}
@media(max-width:700px){.pulse-head{display:block}.pulse-head>span{display:block;text-align:left;margin-top:5px}.pulse-grid{grid-template-columns:1fr}.system-pulse{grid-column:auto}}
@media(max-width:620px){header{padding:18px;border-radius:15px}.command-deck{border-radius:17px}.pulse-card{padding:18px;border-radius:16px}.pulse-head{padding:0 3px}.ops,.monitor{border-radius:16px}.session-row>div:first-child{display:block}.session-row span{display:block;text-align:left;margin-top:2px}}
.impact-head{display:flex;justify-content:space-between;align-items:end;gap:14px;margin:22px 0 12px}.impact-head h3{margin:0 0 4px}.impact-head p{margin:0;color:var(--muted);font-size:12px}.impact-head>span{font-size:11px;color:var(--muted);white-space:nowrap}.impact-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.impact-card{background:#f7faf9;border:1px solid var(--line);border-left:4px solid var(--teal);padding:13px 15px;border-radius:12px}.impact-card.negative{border-left-color:var(--red)}.impact-card.round-shift{border-left-color:#8196a2;background:#f5f7f8}.impact-top{display:flex;justify-content:space-between;gap:9px;align-items:baseline}.impact-top strong{font-size:23px;font-variant-numeric:tabular-nums}.impact-card.positive .impact-top strong{color:var(--teal)}.impact-card.negative .impact-top strong{color:var(--red)}.impact-card.round-shift .impact-top strong{color:#536b77}.impact-type{font-weight:700;font-size:13px}.impact-ticks{font-size:11px;color:var(--muted);margin:3px 0 9px}.impact-drivers{display:flex;gap:5px;flex-wrap:wrap}.impact-drivers span{background:#e8f0f0;border-radius:20px;padding:3px 7px;font-size:10px}.impact-card p{font-size:11px;color:var(--muted);margin:10px 0 0;line-height:1.4}
.duel-history-count{font-size:12px;color:var(--muted);font-weight:700}.duel-legend{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}.duel-legend span{font-size:11px;padding:4px 9px;border-radius:20px;background:#f1f5f4}.duel-legend span:before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:var(--teal)}.duel-legend .negative:before{background:var(--red)}.duel-legend .missed:before{background:#d39d50}.duel-legend .practice:before{background:#aebbc1}.duel-plot,.duel-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}.duel-plot{background:linear-gradient(#f7faf9,#fff);padding:5px}.duel-plot svg{display:block}.duel-plot rect.positive{fill:var(--teal)}.duel-plot rect.negative{fill:var(--red)}.duel-plot rect.missed{fill:#d39d50}.duel-plot rect.practice{fill:#aebbc1}.duel-plot rect.flat{fill:#718b94}.duel-plot rect:hover{opacity:.65}.duel-table{border-collapse:collapse;width:100%;font-size:11px}.duel-table th,.duel-table td{text-align:left;white-space:nowrap;border-bottom:1px solid #e9efee;padding:8px 10px;font-variant-numeric:tabular-nums}.duel-table th{position:sticky;top:0;background:#eff5f4;color:var(--ink-2)}.duel-table .result{font-weight:800}.duel-table .negative .result{color:var(--red)}.duel-table .positive .result{color:var(--teal)}.duel-table .practice{color:#74848a}.duel-table-wrap{max-height:520px}
@media(max-width:700px){.impact-grid{grid-template-columns:1fr}.impact-head{display:block}.impact-head>span{display:block;margin-top:7px}}
"""


CSS += """
.duel-detail summary{cursor:pointer;color:var(--teal);font-weight:700;white-space:nowrap}.duel-detail>div{max-width:440px;white-space:normal;background:#f7faf9;padding:12px;border:1px solid var(--line);border-radius:9px;line-height:1.4}.duel-detail p{margin:4px 0 8px}.duel-detail ol{padding-left:18px;margin:8px 0;max-height:250px;overflow:auto}.duel-detail li{padding:5px 0;border-top:1px solid var(--line)}.duel-detail small{display:block;color:var(--muted)}
"""


def render_operations(data: dict) -> str:
    op = data.get('operations') or {}
    duel, capital, market = (op.get('duels') or {}), (op.get('capital') or {}), (op.get('market') or {})
    queue = op.get('queue') or []
    alerts = ''.join(f'<li class="ops-alert {esc(item.get("priority"))}"><b>{esc(item.get("action"))}</b>'
                     f'<span>{esc(item.get("evidence"))}</span></li>' for item in queue[:6])
    if not alerts:
        alerts = '<li class="ops-alert"><b>Sin alertas prioritarias</b><span>Seguir el reloj y confirmar la frescura de las fuentes.</span></li>'
    duel_rows = []
    for row in (duel.get('rows') or [])[:12]:
        width = min(100, max(3, row['ticks_left'] / 12 * 100))
        duel_rows.append(f'<div class="duel-lane {esc(row["state"])}">'
                         f'<div><b>Duelo {esc(row["duel"])}</b><span>{esc(row["role"])} · '
                         f'{esc(row["ticks_left"])} ticks · margen {fmt(row["total_margin"])} P</span></div>'
                         f'<div class="duel-track"><i style="width:{width:.1f}%"></i></div>'
                         f'<small>{esc(row["action"])}</small></div>')
    if not duel_rows:
        duel_rows.append('<p class="ops-empty">'
                         + ('Lectura privada de duelos no disponible.' if duel.get('status') == 'unavailable'
                            else 'No hay duelos vivos en este tick.') + '</p>')
    cash = capital.get('cash')
    cash_parts = ''
    if isinstance(cash, (int, float)) and cash > 0:
        committed = min(100, 100 * capital.get('open_bid_commitments', 0) / cash)
        reserve = min(100 - committed, 100 * capital.get('operating_reserve', 0) / cash)
        cash_parts = (f'<div class="cash-stack" role="img" aria-label="Caja comprometida {fmt(capital.get("open_bid_commitments"))} P, '
                      f'reserva {fmt(capital.get("operating_reserve"))} P, gastable {fmt(capital.get("spendable_after_reserve"))} P">'
                      f'<i class="committed" style="width:{committed:.1f}%"></i>'
                      f'<i class="reserved" style="width:{reserve:.1f}%"></i></div>')
    return ('<section id="operacion" class="ops"><div class="ops-heading"><div><h2>Mesa de mando</h2>'
            '<p>Decisiones del tick actual. El panel observa; los agentes ejecutan con sus propios límites.</p></div>'
            f'<span>Tick {esc(op.get("source_tick"))}</span></div><div class="ops-grid">'
            '<article class="ops-primary"><h3>Atender ahora</h3><ul class="ops-alerts">' + alerts + '</ul></article>'
            '<article class="ops-detail"><h3>Duelos vivos</h3><p>Una aceptación por tick. Releer antes de aceptar.</p>'
            + ''.join(duel_rows) + '</article>'
            '<article class="ops-detail"><h3>Capital y mercado</h3>'
            f'<p><strong>{fmt(cash)} P</strong> en caja · {fmt(capital.get("open_bid_commitments"))} P comprometidas</p>'
            + cash_parts + f'<p>Gastable tras reserva: <b>{fmt(capital.get("spendable_after_reserve"))} P</b></p>'
            f'<p>Venue {esc(market.get("venue"))} · {esc(market.get("mechanism"))}; '
            f'broker vivo: {"por verificar" if market.get("broker_required") else "no requerido"}</p>'
            f'<p>Board: {"caja suficiente para preflight" if capital.get("board_cash_ready") else "caja insuficiente o privada no disponible"}. '
            'La decisión final requiere autotest y supervisor.</p></article></div></section>')


def render_command_deck(data: dict) -> str:
    """First-screen decision view; every number comes from the current snapshot."""
    board = sorted((r for r in (data.get('leaderboard') or {}).get('teams') or []
                    if isinstance(r.get('score'), (int, float))),
                   key=lambda r: r['score'], reverse=True)
    idx = next((i for i, r in enumerate(board) if r.get('team') == 't15'), None)
    mine = (data.get('score') or {}).get('score')
    if not isinstance(mine, (int, float)) and idx is not None:
        mine = board[idx]['score']
    public_mine = board[idx]['score'] if idx is not None else None
    above = board[idx - 1] if idx is not None and idx > 0 else None
    below = board[idx + 1] if idx is not None and idx + 1 < len(board) else None
    gap = max(0, above['score'] - public_mine) if above and public_mine is not None else None
    score = data.get('scoring') or {}
    components = score.get('components') or {}
    op = data.get('operations') or {}
    capital = op.get('capital') or {}
    queue = op.get('queue') or []
    trades = data.get('trades') or []
    if queue:
        action = queue[0].get('action')
    elif trades:
        first_trade = trades[0]
        action = (f'{first_trade.get("action") or "Operar"} · {first_trade.get("team") or ""} '
                  f'{", ".join(first_trade.get("give") or [])}').strip()
    else:
        action = None
    evidence = queue[0].get('evidence') if queue else (trades[0].get('why') if trades else None)
    action_target = '#operacion' if queue else '#ranking'
    spendable = capital.get('spendable_after_reserve')
    cash = capital.get('cash')
    feed = data.get('feed_health') or {}
    source_label = ('Feed al día' if feed.get('status') == 'fresh' else
                    'Feed pendiente de verificar')
    bars = []
    for key, label, note in (
            ('negotiating', 'Negociación', 'Duelos, dealers y trades'),
            ('market', 'Mercado', 'Test y valor entre terceros')):
        value = (components.get(key) or {}).get('ours')
        pct = max(0, min(100, value / 30 * 100)) if isinstance(value, (int, float)) else 0
        bars.append(f'<div class="deck-component"><div class="deck-line"><span>{label}<small>{note}</small></span>'
                    f'<b>{fmt(value)} <em>/ 30</em></b></div><div class="deck-track" role="img" '
                    f'aria-label="{label}: {fmt(value)} de 30 puntos"><i style="width:{pct:.1f}%"></i></div></div>')
    comparison = []
    for row, kind in ((above, 'above'), ({'team': 't15', 'score': public_mine}, 'self'), (below, 'below')):
        if not row or not isinstance(row.get('score'), (int, float)):
            continue
        pct = max(2, min(100, row['score'] / max(40, board[0]['score'] if board else 40) * 100))
        comparison.append(f'<div class="race-row {kind}"><span>{esc(row["team"])}</span>'
                          f'<div class="race-track"><i style="width:{pct:.1f}%"></i></div>'
                          f'<b>{fmt(row["score"])}</b></div>')
    freshness = ('Dato privado verificado' if data.get('verified') else 'Sólo lectura pública')
    return ('<section class="command-deck" aria-label="Situación actual">'
            '<div class="deck-lead"><div class="deck-score"><span class="deck-kicker">Situación actual</span>'
            f'<strong>{fmt(mine)}</strong><span class="deck-score-label">puntos de servidor · {freshness}</span>'
            f'<div class="deck-rank">Puesto <b>{idx + 1 if idx is not None else "—"}</b> de {len(board) or "—"}'
            f'<span>{("Faltan " + fmt(gap) + " puntos para superar a " + esc(above["team"])) if gap is not None else "Sin rival superior medido"}</span></div>'
            '</div><div class="deck-race"><div class="deck-title"><h2>Carrera inmediata</h2>'
            '<a href="#estrategia-ranking">Ver ruta</a></div>' + ''.join(comparison) +
            '<p>Comparación del leaderboard público. El score privado puede ir por delante.</p></div></div>'
            '<div class="deck-lower"><div class="deck-sources"><div class="deck-title"><h2>De dónde vienen los puntos</h2>'
            '<a href="#monitor">Ver desglose</a></div>' + ''.join(bars) + '</div>'
            '<div class="deck-capital"><div class="deck-title"><h2>Capital para actuar</h2>'
            '<a href="#operacion">Ver operación</a></div>'
            f'<div class="deck-capital-value">{fmt(spendable)} <span>P disponibles tras reserva</span></div>'
            f'<p>Caja {fmt(cash)} P · reserva {fmt(capital.get("operating_reserve"))} P · '
            f'comprometido {fmt(capital.get("open_bid_commitments"))} P</p></div></div>'
            '<div class="deck-decision"><div><span class="deck-kicker">Siguiente decisión</span>'
            f'<h2>{esc(action or "Revisar oportunidades nuevas")}</h2>'
            f'<p>{esc(evidence or "No hay una alerta prioritaria en este tick. Vigila duelos y demandas nuevas.")}</p></div>'
            f'<a href="{action_target}">Abrir detalle</a><span class="deck-fresh">{source_label}</span></div>'
            '</section>')


def render_strategy_health(data: dict) -> str:
    state = data.get('strategy_health') or {}
    duels = state.get('duels') or {}
    memory = state.get('memory') or {}
    learner = state.get('duel_learning') or {}
    execution = state.get('execution') or {}
    market = ((data.get('operations') or {}).get('market') or {})
    score = data.get('scoring') or {}
    sessions = []
    for row in duels.get('sessions') or []:
        total = max(1, row['total'])
        segments = ''.join(
            f'<span class="{cls}" style="width:{row[key] / total * 100:.1f}%"></span>'
            for key, cls in (('positive', 'won'), ('zero_deal', 'flat'),
                             ('negative', 'lost'), ('no_deal', 'missed')))
        sessions.append(f'<div class="session-row"><div><b>Sesión {esc(row["session"])}</b>'
                        f'<span>{row["positive"]} rentables · {row["negative"]} negativas · '
                        f'{row["no_deal"]} sin trato</span></div>'
                        f'<div class="session-track" role="img" aria-label="Sesión {row["session"]}: '
                        f'{row["positive"]} rentables, {row["negative"]} negativas, '
                        f'{row["no_deal"]} sin trato">{segments}</div></div>')
    memory_text = {
        'fresh': 'Evidencia reciente', 'stale': 'Memoria atrasada',
        'database_only': 'Base creada; falta informe', 'not_started': 'Aún no integrada',
    }.get(memory.get('status'), 'Sin confirmar')
    if execution.get('mode') != 'coordinator' and memory.get('last_tick') is not None:
        memory_text = 'Historial guardado · tick ' + esc(memory['last_tick'])
    execution_text = ({'duels': 'Duelos en ejecución', 'coordinator': 'Coordinador en ejecución'}
                      .get(execution.get('mode'), 'Agente operativo detenido')
                      if execution.get('status') == 'running' else 'Agente operativo detenido')
    learner_text = ('Informe guardado: ' + fmt(learner.get('duels_done')) + ' duelos'
                    if learner.get('status') == 'saved_report' else 'Sin informe de aprendizaje')
    return ('<section id="pulso" class="pulse"><div class="pulse-head"><div>'
            '<h2>Seguimiento de la estrategia</h2><p>Resultados confirmados y estado real de los agentes.</p>'
            '</div><span>La API decide el resultado; el historial local explica la ejecución</span></div>'
            '<div class="pulse-grid"><article class="pulse-card duel-pulse"><div class="pulse-card-head">'
            '<h3>Duelos cerrados</h3><strong>' + fmt(duels.get('settled')) + '</strong></div>'
            + (''.join(sessions) or '<p class="pulse-empty">Aún no hay resultados privados de duelos.</p>') +
            '<div class="session-key"><span><i class="won"></i>Rentable</span><span><i class="lost"></i>Pérdida</span>'
            '<span><i class="missed"></i>Sin trato</span></div>'
            f'<p class="pulse-note">{fmt(duels.get("negative"))} cierres negativos y '
            f'{fmt(duels.get("no_deal"))} sin acuerdo en sesiones puntuables. '
            'La práctica queda fuera.</p></article>'
            '<article class="pulse-card market-pulse"><div class="pulse-card-head"><h3>Mercado propio</h3>'
            f'<strong>{fmt(score.get("market"))}<small> / 30</small></strong></div>'
            f'<div class="market-hero"><b>{fmt(market.get("trades"))}</b><span>tratos de terceros en '
            f'{esc(market.get("venue") or "nuestro venue")}</span></div>'
            f'<p class="pulse-note">Valor creado entre terceros: {fmt(score.get("mm_points"))} P. '
            'Invitar parejas con demanda real a publicar y cerrar aquí; el tráfico bruto no puntúa.</p>'
            '<a href="#ranking">Ver oportunidades de negociación</a></article>'
            '<article class="pulse-card system-pulse"><div class="pulse-card-head"><h3>Sistema de decisión</h3>'
            '<strong class="system-mark">●</strong></div>'
            f'<div class="system-line"><span>Proceso operativo</span><b>{execution_text}</b></div>'
            f'<div class="system-line"><span>Memoria del coordinador</span><b>{memory_text}</b></div>'
            f'<div class="system-line"><span>Evidencia reciente</span><b>{fmt(memory.get("events_in_window"))} eventos</b></div>'
            f'<div class="system-line"><span>Aprendizaje de duelos</span><b>{learner_text}</b></div>'
            '<p class="pulse-note">El historial de duelos ajusta aperturas al ejecutar. Cambiar aceptar o esperar requiere '
            '<code>--learn</code>; el replay temporal aún no ha demostrado mejora. Un informe guardado no confirma '
            'que el modelo esté activo.</p></article></div></section>')


def render(data: dict) -> str:
    tick = data.get("tick")
    live = data.get("live")
    teams = data.get("teams") or []
    trades = data.get("trades") or []
    parts = ['<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Team 15 · mesa de trades</title><style>', CSS, '</style></head><body data-tick="' + esc(tick) + '"><div class="wrap">',
             '<header><div><h1>Mesa de mando · Team 15</h1><p class="sub">Puntos, duelos, caja y mercado para decidir durante el último día.</p></div>',
             '<div class="status"><span class="flag ', 'live' if live else 'warn', '">',
             'Equipo conectado' if live else 'Sólo feed público', '</span><span class="clock">Tick ', esc(tick), '</span></div></header>',
             '<nav class="jump" aria-label="Secciones"><a href="#pulso">Seguimiento</a><a href="#duelos-historico">Duelos cerrados</a><a href="#operacion">Operación</a><a href="#monitor">Puntos</a><a href="#tendencia">Trayectoria</a><a href="#guide">Ventas</a><a href="#radio">Señales</a><a href="#ranking">Oportunidades</a><a href="#estrategia-ranking">Ranking</a><a href="#catalogo">Catálogo</a></nav>', render_command_deck(data), render_strategy_health(data), render_operations(data), render_monitor(data), render_trend(data), duel_history.render(data.get('duel_history') or []), render_dashboard_overview(data), render_rank_strategy(data)]
    for warning in data.get("warnings") or []:
        parts.append('<div class="warning">' + esc(warning) + '</div>')
    guide = data.get("sale_guide") or []
    parts += ['<section id="guide" class="guide"><div class="guide-head"><div><h2>¿En cuánto vender cada carta?</h2>',
              '<p>Mínimo al proponer una venta que el rival acepte: al menos +2 P sin comisión nuestra. El valor ganado en trades cuenta para negociación; los puntos exactos por venta no se publican.</p></div></div><div class="sale-grid">']
    if not guide:
        parts.append('<div class="guide-empty">No hay duplicados libres verificados para vender ahora.</div>')
    for card in guide:
        best = card["best"]
        public_only = card["loss"] is None
        target = f'{fmt(card["suggested"])} P' if best else '—'
        gain = f'+{fmt(best["net"])} P' if best and best["net"] is not None else '—'
        note = (f'Perdemos {fmt(card["loss"])} P de colección al dar una. '
                + (f'La venta indicada deja {gain} de valor neto para t15.' if best else 'Aún no hay un comprador detectado.')) if not public_only else 'Duplicado observado en el feed; confirma copias libres y valor privado antes de vender.'
        if card.get('concession') is not None:
            note += f' Puedes ceder hasta {fmt(card["concession"])} P frente al precio sugerido y aún conservar al menos +2 P netos, si el rival acepta nuestra propuesta.'
        free_label = f'{card["free"]} libre' if card["free"] == 1 else f'{card["free"]} libres' if isinstance(card["free"], int) else str(card["free"])
        parts += [f'<article class="sale-card{"" if best else " no-buyer"}"><div class="sale-top"><h3>{esc(card["ref"])}</h3><small>{esc(free_label)}</small></div>',
                  '<div class="sale-numbers">',
                  f'<div><span class="label">Mínimo rentable</span><strong>{fmt(card["floor"])}{ " P" if card["floor"] is not None else ""}</strong></div>',
                  f'<div><span class="label">Pedir al comprador</span><strong>{target}</strong></div>',
                  f'<div><span class="label">Valor neto estimado</span><strong class="gain">{gain}</strong></div></div>',
                  f'<p class="sale-note">{esc(note)}</p><div class="sale-buyers">']
        for buyer in card["buyers"]:
            state = 'puja activa' if buyer["active"] else 'propuesta'
            gain_text = f' · +{fmt(buyer["net"])} P netos' if buyer["net"] is not None else ''
            parts.append(f'<span class="buyer-chip{"" if buyer["active"] else " proposed"}">{esc(buyer["team"])} · {fmt(buyer["price"])} P{gain_text} · {state}</span>')
        if not card["buyers"]:
            parts.append('<span class="label">Ningún equipo pidió esta carta en el feed reciente.</span>')
        parts.append('</div></article>')
    parts.append('</div></section>')
    summary = data.get('radio_summary') or {}
    counts = summary.get('by_verdict') or {}
    parts += ['<section id="radio" class="radio"><div class="section-head"><div>',
              '<h2>Radio: qué hacer con cada señal</h2>',
              '<p class="sub">Tres fuentes con fiabilidad distinta. El <b>Boletín del Bazar</b> es oficial: '
              'lo que anuncia ocurre. <b>Radio Rastro</b> son rumores, normalmente con plazo. '
              '<b>El Tablón</b> es cebo, y actuar sobre él puede provocar un enfriamiento con el vendedor.</p>',
              '</div></div>']
    if counts:
        chips = []
        for key in ('act', 'verify', 'expired', 'noise', 'ignore'):
            if counts.get(key):
                label, cls = VERDICT_LABEL[key]
                chips.append(f'<span class="verdict-chip {cls}">{esc(label)} · {esc(counts[key])}</span>')
        parts.append('<div class="verdict-row">' + ''.join(chips) + '</div>')
    parts.append('<div class="radio-list">')
    if not data.get('radio'):
        parts.append('<div class="guide-empty">Aún no hay noticias en /api/news.</div>')
    for news in data.get('radio') or []:
        label, cls = VERDICT_LABEL.get(news.get('verdict'), VERDICT_LABEL['noise'])
        win = news.get('window') or {}
        meta = [esc(news['source']), esc(news.get('reliability'))]
        if news.get('at_hours') is not None:
            meta.append(f'hora {news["at_hours"]:.2f}')
        if win.get('declared'):
            meta.append(('plazo vencido' if news.get('expired') else f'plazo {esc(win["declared"])}')
                        + (f' (hasta {win["expires_at_hours"]:.2f})'
                           if win.get('expires_at_hours') is not None else ''))
        ours = news.get('ours') or []
        parts += [f'<article class="radio-item {cls}"><div class="radio-top">'
                  f'<small>{" · ".join(meta)}</small>'
                  f'<span class="verdict-chip {cls}">{esc(label)}</span></div>',
                  f'<h3>{esc(news["headline"])}</h3>']
        if news.get('body'):
            parts.append(f'<p>{esc(news["body"])}</p>')
        if ours:
            parts.append('<p class="radio-refs">Toca nuestras: '
                         + ''.join(f'<code>{esc(r)}</code>' for r in ours) + '</p>')
        parts.append(f'<p class="radio-action">{esc(news["action"])}</p></article>')
    parts += ['</div></section><div id="ranking" class="layout"><aside class="sidebar"><h2>Compradores</h2><div class="teams">',
              '<button class="team-button" data-team="all" aria-pressed="true">Todos <small>↗</small></button>']
    for team in teams:
        code = team["team"]
        count = len(team["declared_wants"])
        parts.append(f'<button class="team-button" data-team="{esc(code)}" aria-pressed="false">{esc(code)} <small>{count} {"pedido" if count == 1 else "pedidos"}</small></button>')
    parts += ['</div></aside><main><div class="section-head"><div><h2>Ranking de ventas</h2>',
              '<p>Ventas de duplicados primero; los canjes aparecen como alternativa. Las propuestas requieren respuesta.</p></div>',
              '<label><input id="pause" type="checkbox"> Pausar refresco</label></div><div class="rank">']
    if not trades:
        parts.append('<div class="empty">No hay compradores o canjes rentables para los duplicados libres en este tick.</div>')
    for i, trade in enumerate(trades, 1):
        code = trade["team"]
        give = ", ".join(trade["give"]) or "—"
        receive = ", ".join(trade["receive"]) or "—"
        offer = f' · oferta #{trade["offer_id"]}' if trade["offer_id"] else ""
        public_only = trade.get("surplus") is None
        value_label = "Cobro tras comisión" if trade["kind"] == "public-live" else "Valor neto para t15"
        value_text = (f'{fmt(trade["rank_signal"])} P' if trade["kind"] == "public-live" else "—") if public_only else f'+{fmt(trade["surplus"])} P'
        parts += [f'<article class="trade {esc(trade["kind"])}" data-team="{esc(code)}">',
                  f'<span class="index">{i}</span><div><div class="title">{esc(trade["action"])} · {esc(code)}<span class="tag">{esc(trade["confidence"])}</span></div>',
                  f'<p>Dar {esc(give)} · recibir {esc(receive)}{esc(offer)}</p>',
                  f'<div class="why">{esc(trade["why"])}</div></div>',
                  f'<div class="price"><span class="label">Precio</span><span class="money">{fmt(trade["price"])} P</span><span class="label">Comisión nuestra: {fmt(trade["fee"])} P</span></div>',
                  f'<div class="surplus"><span class="label">{value_label}</span><span class="money gain">{value_text}</span>',
                  '<span class="label">Puntos futuros: no calculables</span></div></article>']
    parts += ['</div><div id="filter-empty" class="empty" hidden>No hay ventas o canjes para este equipo en este tick. Selecciona Todos para ver las demás oportunidades.</div>']
    for team in teams:
        parts.append(f'<section class="team-profile" data-team="{esc(team["team"])}" hidden><h3>{esc(team["team"])} · señales del feed</h3>'
                     f'<p><b>Le vimos:</b> {esc(", ".join(team["observed_held"]) or "Sin evidencia")}</p>'
                     f'<p><b>Pidió:</b> {esc(", ".join(team["declared_wants"]) or "Sin demanda declarada")}</p>'
                     f'<p><b>Ofrece ahora:</b> {esc(", ".join(team["offered"]) or "Sin oferta identificada")}</p></section>')
    parts += ['</main></div><div class="below"><section class="panel"><h2>',
              'Nuestra mano real' if live else 'Nuestra mano observada en el feed', '</h2>',
              '<table><thead><tr><th>Carta</th><th>Copias</th><th>Libres para dar</th><th>Valor perdido al dar una</th></tr></thead><tbody>']
    for card in data.get("inventory") or []:
        parts.append(f'<tr><td>{esc(card["ref"])}</td><td>{card["copies"]}</td><td>{card["free_surplus"]}</td><td>{fmt(card["loss"])} P</td></tr>')
    parts += ['</tbody></table></section><section class="panel"><h2>',
              'Cartas que nos aportan más' if live else 'Cartas que t15 pidió en el feed', '</h2>',
              '<table><thead><tr><th>Carta que falta</th><th>Valor al recibirla</th><th>Página</th></tr></thead><tbody>']
    for card in (data.get("needs") or [])[:20]:
        parts.append(f'<tr><td>{esc(card["ref"])}</td><td>{fmt(card["gain"])} P</td><td>{esc(card["page"] or "")}</td></tr>')
    parts += ['</tbody></table></section></div><div class="below"><section class="panel"><h2>Qué sabemos de cada equipo</h2>',
              '<p class="sub">Posesiones observadas y demandas declaradas en el feed. Una carta no observada no es una carta ausente.</p>',
              '<table><thead><tr><th>Equipo</th><th>Le vimos</th><th>Pidió</th><th>Ofrece ahora</th></tr></thead><tbody>']
    for t in teams:
        parts.append(f'<tr class="team-row" data-team="{esc(t["team"])}"><td>{esc(t["team"])}</td><td>{esc(", ".join(t["observed_held"][:12]))}</td><td>{esc(", ".join(t["declared_wants"][:12]))}</td><td>{esc(", ".join(t["offered"][:12]))}</td></tr>')
    parts += ['</tbody></table></section><section class="panel"><h2>Cómo leer los puntos</h2>',
              '<p>El <b>valor neto (+P)</b> suma el efectivo y el cambio en nuestra colección, después de la comisión. Es la medida privada usada para ordenar trades. El servidor transforma el valor de negociación en puntos de la ronda; la fórmula exacta por operación no está publicada, así que el panel no inventa puntos futuros.</p>',
              '<p>«Oferta activa» significa que hay una estructura aceptable visible en un venue. «Proponer venta» usa una demanda observada; el precio es nuestro mínimo rentable o una puja histórica y requiere que el rival acepte.</p>',
              '</section></div>']
    parts += [render_catalog(data),
              '<div class="foot">Sólo lecturas GET · datos renovados cada 15 s · ',
              time.strftime('%H:%M:%S', time.localtime(data.get('built_at') or time.time())),
              ' · clave nunca enviada al navegador</div></div><script>',
              "const buttons=document.querySelectorAll('.team-button');const rows=document.querySelectorAll('.trade,.team-row,.team-profile');"
              "let selected=sessionStorage.getItem('t15.team')||'all';function choose(t){selected=t;sessionStorage.setItem('t15.team',t);"
              "buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.team===t)));"
              "rows.forEach(r=>r.hidden=r.classList.contains('team-profile')?(t==='all'||r.dataset.team!==t):(t!=='all'&&r.dataset.team!==t));"
              "document.getElementById('filter-empty').hidden=t==='all'||!!document.querySelector('.trade:not([hidden])')}"
              "buttons.forEach(b=>b.addEventListener('click',()=>choose(b.dataset.team)));choose(selected);"
              "const pause=document.getElementById('pause');pause.checked=sessionStorage.getItem('t15.pause')==='1';"
              "const tickNow=Number(document.body.dataset.tick||0),tickPrev=Number(sessionStorage.getItem('t15.last_tick')||0);"
              "if(tickPrev&&tickNow>tickPrev){const jump=document.querySelector('.jump');const note=document.createElement('span');note.className='refresh-diff';note.textContent='+'+(tickNow-tickPrev)+' ticks nuevos';jump.appendChild(note)}"
              "sessionStorage.setItem('t15.last_tick',String(tickNow));"
              "pause.addEventListener('change',()=>sessionStorage.setItem('t15.pause',pause.checked?'1':'0'));",
              CATALOG_JS,
              "setInterval(()=>{if(!pause.checked)location.reload()},15000);",
              '</script></body></html>']
    return ''.join(parts)


class Handler(BaseHTTPRequestHandler):
    model: Model = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            body, typ, status = b"ok\n", "text/plain", 200
        elif path == "/api/strategy":
            try:
                payload = json.dumps(strategy_export(self.model.snapshot()), ensure_ascii=False).encode()
                body, typ, status = payload, "application/json; charset=utf-8", 200
            except Exception as exc:
                body, typ, status = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json; charset=utf-8", 500
        elif path in ("/", "/index.html"):
            try:
                body, typ, status = render(self.model.snapshot()).encode(), "text/html; charset=utf-8", 200
            except Exception as exc:
                body, typ, status = f"Panel: {type(exc).__name__}: {exc}\n".encode(), "text/plain", 500
        else:
            body, typ, status = b"not found\n", "text/plain", 404
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8775)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--reserve", type=int, default=100)
    parser.add_argument("--feed-root", type=Path, default=KIT,
                        help="carpeta bazaar-kit con data/feed_history.jsonl")
    parser.add_argument("--env-file", type=Path, help="archivo .env existente para leer BAZAAR_KEY")
    args = parser.parse_args()
    key = os.environ.get("BAZAAR_KEY")
    if not key and args.env_file:
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "BAZAAR_KEY":
                key = value.strip().strip("\"'")
                break
    model = Model(Reader(args.url, key), reserve=args.reserve, feed_root=args.feed_root)
    handler = type("Team15Handler", (Handler,), {"model": model})
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Mesa de trades en http://127.0.0.1:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
