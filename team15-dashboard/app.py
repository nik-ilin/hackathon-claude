#!/usr/bin/env python3
"""Live, read-only Team 15 trade desk built on PR #2's public feed dashboard.

Run from the repository: BAZAAR_KEY=... python3 team15-dashboard/app.py
Only GET requests reach Bazaar. The key stays server-side; bind is localhost.
"""
from __future__ import annotations

import argparse
import html
import json
import os
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
sys.path.insert(0, str(KIT))
import dashboard as public_dashboard

from planner import build_rank

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai").rstrip("/")


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ") if isinstance(value, (float, int)) else esc(value)


class Reader:
    """GET only. Authentication is sent only to the game's origin."""
    def __init__(self, url: str, key: str | None, timeout: float = 9):
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def get(self, path: str, *, private: bool = False) -> dict:
        if private and not self.key:
            raise ValueError("Falta BAZAAR_KEY")
        headers = {"Accept": "application/json"}
        if private:
            headers["X-Team-Key"] = self.key
        req = urllib.request.Request(self.url + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return json.load(response)


class Model:
    def __init__(self, reader: Reader, team: str = "t15", reserve: int = 100,
                 feed_root: Path = KIT):
        self.reader, self.team, self.reserve = reader, team, reserve
        self.public = public_dashboard.Builder(public_dashboard.ReadOnlyClient(reader.url),
                                               team=team, root=feed_root)
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0
        self.catalog = None

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
        me, own_offers = {}, []
        if self.reader.key:
            try:
                me = self.reader.get("/api/me", private=True)
                if me.get("id") != self.team:
                    warnings.append(f"La clave corresponde a {me.get('id')}, no a {self.team}; vista privada bloqueada.")
                    me = {}
                else:
                    own_offers = self.reader.get("/api/me/offers", private=True).get("offers") or []
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
        rank["warnings"] = warnings + public.errors + rank["warnings"]
        rank["board_count"] = sum(map(len, boards.values()))
        rank["venue_count"] = len(boards)
        rank["built_at"] = time.time()
        rank["clock"] = public.clock
        rank["live"] = bool(me)
        rank["leaderboard"] = public.leaderboard
        if not me:
            public_team = next((row for row in public.leaderboard.get("teams", [])
                                if row.get("team") == self.team), None)
            if public_team:
                rank["score"] = {"score": public_team.get("score")}
        return rank


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
.layout{display:grid;grid-template-columns:240px minmax(0,1fr);gap:26px}.sidebar{border-right:1px solid var(--line);padding-right:20px}
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
button:focus-visible{outline:3px solid var(--saffron);outline-offset:2px}@media(max-width:850px){.wrap{padding:18px}.layout{grid-template-columns:1fr}.sidebar{border:0;padding:0}.teams{display:flex;overflow-x:auto}.team-button{min-width:100px}.summary{grid-template-columns:1fr 1fr}.metric:nth-child(3){border-left:0;padding-left:0}.trade{grid-template-columns:25px 1fr 95px}.trade .surplus{grid-column:3}.trade .price{grid-column:3;grid-row:1}.below{grid-template-columns:1fr}}
"""


def render(data: dict) -> str:
    tick = data.get("tick")
    live = data.get("live")
    score = data.get("score") or {}
    teams = data.get("teams") or []
    trades = data.get("trades") or []
    parts = ['<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Team 15 · mesa de trades</title><style>', CSS, '</style></head><body><div class="wrap">',
             '<header><div><h1>Vender duplicados · Team 15</h1><p class="sub">Compradores concretos, precio razonado y valor neto para nuestra colección.</p></div>',
             '<div class="status"><span class="flag ', 'live' if live else 'warn', '">',
             'Equipo conectado' if live else 'Sólo feed público', '</span><span class="clock">Tick ', esc(tick), '</span></div></header>',
             '<div class="summary">',
             f'<div class="metric"><small>Efectivo nuestro</small><strong>{fmt(data.get("cash"))} P</strong></div>',
             f'<div class="metric"><small>Valor de colección</small><strong>{fmt(data.get("collection_value"))} P</strong></div>',
             f'<div class="metric"><small>{"Puntos propios en vivo" if live else "Puntos del leaderboard (con retraso)"}</small><strong>{fmt(score.get("score"))}</strong></div>',
             f'<div class="metric"><small>Ofertas visibles · venues</small><strong>{data.get("board_count",0)} · {data.get("venue_count",0)}</strong></div>',
             '</div>']
    for warning in data.get("warnings") or []:
        parts.append('<div class="warning">' + esc(warning) + '</div>')
    parts += ['<div class="layout"><aside class="sidebar"><h2>Compradores</h2><div class="teams">',
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
              '</section></div><div class="foot">Sólo lecturas GET · datos renovados cada 15 s · ',
              time.strftime('%H:%M:%S', time.localtime(data.get('built_at') or time.time())),
              ' · clave nunca enviada al navegador</div></div><script>',
              "const buttons=document.querySelectorAll('.team-button');const rows=document.querySelectorAll('.trade,.team-row,.team-profile');"
              "let selected=sessionStorage.getItem('t15.team')||'all';function choose(t){selected=t;sessionStorage.setItem('t15.team',t);"
              "buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.team===t)));"
              "rows.forEach(r=>r.hidden=r.classList.contains('team-profile')?(t==='all'||r.dataset.team!==t):(t!=='all'&&r.dataset.team!==t));"
              "document.getElementById('filter-empty').hidden=t==='all'||!!document.querySelector('.trade:not([hidden])')}"
              "buttons.forEach(b=>b.addEventListener('click',()=>choose(b.dataset.team)));choose(selected);"
              "const pause=document.getElementById('pause');pause.checked=sessionStorage.getItem('t15.pause')==='1';"
              "pause.addEventListener('change',()=>sessionStorage.setItem('t15.pause',pause.checked?'1':'0'));"
              "setInterval(()=>{if(!pause.checked)location.reload()},15000);",
              '</script></body></html>']
    return ''.join(parts)


class Handler(BaseHTTPRequestHandler):
    model: Model = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            body, typ, status = b"ok\n", "text/plain", 200
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
