"""Libro de liquidaciones propias con valoración observada sólo cuando se puede aislar."""
from __future__ import annotations

import bisect
import html
import json
from pathlib import Path


def settlements(paths, team: str = 't15') -> list[dict]:
    found = {}
    for path in paths or []:
        try:
            lines = Path(path).open(encoding='utf-8')
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') != 'settlement':
                    continue
                p = event.get('payload') or {}
                if team not in (p.get('parties') or []):
                    continue
                sid = p.get('settlement') or event.get('id')
                if sid is not None:
                    found[sid] = p
    return sorted(found.values(), key=lambda x: (x.get('tick') or 0, x.get('settlement') or 0))


def ledger(raw: list[dict], history: list[dict], team: str = 't15') -> list[dict]:
    valid = sorted((r for r in history if isinstance(r.get('tick'), int)
                    and isinstance(r.get('cash'), (int, float))
                    and isinstance(r.get('collection_value'), (int, float))), key=lambda r: r['tick'])
    ticks = [r['tick'] for r in valid]
    trade_ticks = [p.get('tick') for p in raw if isinstance(p.get('tick'), int)]
    out = []
    for p in raw:
        tick = p.get('tick')
        items = [x for x in (p.get('items') or []) if isinstance(x, dict)]
        given = [x.get('ref') or x.get('kind') for x in items if x.get('frm') == team]
        received = [x.get('ref') or x.get('kind') for x in items if x.get('to') == team]
        direction = 'Compra' if received and not given else 'Venta' if given and not received else 'Trueque' if received and given else 'Otra'
        parties = p.get('parties') or []
        peer = next((x for x in parties if x != team), None)
        row = {'settlement': p.get('settlement'), 'tick': tick, 'direction': direction,
               'counterparty': peer, 'venue': p.get('venue'), 'given': given, 'received': received,
               'price': p.get('price'), 'fee': p.get('fee'), 'kind': p.get('kind'),
               'net': None, 'cash_delta': None, 'collection_delta': None,
               'quality': 'Sin valoración aislada', 'evidence': 'Sin muestras privadas suficientes alrededor de la liquidación.'}
        if isinstance(tick, int) and ticks:
            idx = bisect.bisect_left(ticks, tick)
            before = valid[idx - 1] if idx > 0 else None
            after = valid[idx] if idx < len(valid) else None
            one_trade = sum(before['tick'] < t <= after['tick'] for t in trade_ticks) == 1 if before and after else False
            deals_match = (before and after and isinstance(before.get('deals'), (int, float))
                           and isinstance(after.get('deals'), (int, float))
                           and after['deals'] - before['deals'] == 1)
            if before and after and one_trade and deals_match and after['tick'] - before['tick'] <= 10:
                cash_delta = round(after['cash'] - before['cash'], 2)
                collection_delta = round(after['collection_value'] - before['collection_value'], 2)
                price = p.get('price')
                expected = (-price if direction == 'Compra' else price if direction == 'Venta' else None)
                # Exact cash movement supports an isolated attribution; unknown fees or grants do not.
                if isinstance(expected, (int, float)) and abs(cash_delta - expected) < .011:
                    net = round(cash_delta + collection_delta, 2)
                    row.update(net=net, cash_delta=cash_delta, collection_delta=collection_delta,
                               quality='Buena' if net > 0 else 'Mala' if net < 0 else 'Neutra',
                               evidence=f'Caja y colección observadas entre ticks {before["tick"]} y {after["tick"]}; '
                                        'una sola liquidación propia en ese intervalo.')
                else:
                    row['evidence'] = 'Hubo cambios de caja adicionales o una comisión no atribuible; no se aísla el resultado.'
            elif before and after and not one_trade:
                row['evidence'] = 'Varias liquidaciones propias entre muestras privadas; no se reparte el resultado.'
            elif before and after and not deals_match:
                row['evidence'] = 'El contador de tratos no aumentó exactamente en uno entre muestras privadas.'
        out.append(row)
    return list(reversed(out))


def summary(rows: list[dict]) -> dict:
    known = [r for r in rows if isinstance(r.get('net'), (int, float))]
    return {'settlements': len(rows), 'buys': sum(r['direction'] == 'Compra' for r in rows),
            'sales': sum(r['direction'] == 'Venta' for r in rows),
            'other': sum(r['direction'] not in ('Compra', 'Venta') for r in rows),
            'measured': len(known), 'unmeasured': len(rows) - len(known),
            'positive': sum(r['net'] > 0 for r in known), 'negative': sum(r['net'] < 0 for r in known),
            'observed_net': round(sum(r['net'] for r in known), 2)}


def render(rows: list[dict], unopened_packs: list[dict] | None = None) -> str:
    s = summary(rows)
    unopened_packs = unopened_packs or []
    esc = lambda x: html.escape(str(x)) if x is not None else '—'
    table = []
    for r in rows:
        verdict = r['quality']
        net = f'{r["net"]:+.2f} P' if r['net'] is not None else '—'
        table.append(f'<tr class="{("good" if verdict == "Buena" else "bad" if verdict == "Mala" else "unknown")}">'
                     f'<td>{esc(r["tick"])}</td><td>{esc(r["direction"])}</td>'
                     f'<td>{esc(", ".join(r["received"]) or "—")}</td><td>{esc(", ".join(r["given"]) or "—")}</td>'
                     f'<td>{esc(r["counterparty"])}</td><td>{esc(r["price"])}</td>'
                     f'<td>{esc(r["venue"])}</td><td>{net}</td><td>{esc(verdict)}</td>'
                     f'<td><details><summary>Fuente</summary>{esc(r["evidence"])}'
                     + (f' Caja {r["cash_delta"]:+.2f} P; colección {r["collection_delta"]:+.2f} P.'
                        if r['net'] is not None else '') + '</details></td></tr>')
    points = [(r['tick'], r['net']) for r in reversed(rows) if isinstance(r.get('tick'), int)
              and isinstance(r.get('net'), (int, float))]
    chart = '<p class="chart-note">Aún no hay liquidaciones con valoración aislada para dibujar.</p>'
    if points:
        bars = ''.join(f'<div class="trade-net-bar {("good" if v > 0 else "bad")}" '
                       f'title="tick {t}: {v:+.2f} P"><span style="height:{max(3, abs(v) / max(abs(x[1]) for x in points) * 82):.1f}px"></span>'
                       f'<small>{t}</small></div>' for t, v in points)
        chart = f'<div class="trade-net-chart" role="img" aria-label="Excedente observado por liquidación aislada">{bars}</div>'
    return ('<section id="compras-ventas" class="monitor trade-history">'
            '<div class="section-head"><div><h2>Compras y ventas realizadas</h2>'
            '<p class="sub">Cada fila es una liquidación capturada por el feed local, que puede estar incompleto. '
            'Buena o mala compara efectivo y cambio de valor de colección cuando una única operación queda aislada '
            'entre dos muestras privadas.</p></div></div>'
            '<div class="trade-history-kpis">'
            f'<article><small>Liquidaciones en el feed</small><strong>{s["settlements"]}</strong><span>{s["buys"]} compras · {s["sales"]} ventas · {s["other"]} trueques/otras</span></article>'
            f'<article><small>Resultado observado</small><strong>{s["observed_net"]:+.2f} P</strong>'
            f'<span>{s["measured"]} valoradas · {s["unmeasured"]} sin valoración aislada</span></article>'
            f'<article><small>Balance medido</small><strong>{s["positive"]} / {s["negative"]}</strong>'
            '<span>Buenas / malas; las demás no se juzgan.</span></article></div>'
            f'<p class="chart-note">Sobres sin abrir ahora: <b>{len(unopened_packs)}</b>'
            + (f' · {", ".join(esc(p.get("ref") or p.get("id")) for p in unopened_packs)}'
               if unopened_packs else '') + '.</p>'
            f'{chart}<p class="chart-note">Excedente económico observado, no puntos directos de leaderboard. '
            'La serie incompleta no representa el resultado de todas las operaciones.</p>'
            '<div class="trade-history-scroll"><table class="trade-history-table"><thead><tr>'
            '<th>Tick</th><th>Tipo</th><th>Recibimos</th><th>Entregamos</th><th>Contraparte</th>'
            '<th>Precio</th><th>Venue</th><th>Neto</th><th>Evaluación</th><th>Evidencia</th>'
            '</tr></thead><tbody>' + ''.join(table) + '</tbody></table></div></section>')
