"""Vista compacta de resultados confirmados por /api/duels?done=true."""
from __future__ import annotations

import html
from collections import defaultdict


def offer_surplus(duel: dict, offer: dict) -> float | None:
    """Excedente previo al decaimiento para una oferta observada; sólo formatos verificables."""
    price, limit = offer.get('price'), duel.get('your_limit')
    if not isinstance(price, (int, float)) or not isinstance(limit, (int, float)):
        return None
    role = duel.get('role')
    if role not in ('buyer', 'seller'):
        return None
    margin = limit - price if role == 'buyer' else price - limit
    if margin < 0:
        return round(margin, 2)  # los días no hacen válida una oferta fuera del límite de precio
    if 'days' not in (duel.get('issues') or []):
        return round(margin, 2)
    days, weight = offer.get('days'), duel.get('your_days_weight')
    meaning = str(duel.get('days_meaning') or '').lower()
    if not isinstance(days, (int, float)) or not isinstance(weight, (int, float)):
        return None
    if 'cost' in meaning:
        return round(margin - abs(weight) * days, 2)
    if 'add' in meaning:
        return round(margin + abs(weight) * days, 2)
    return None


def analysis(duels: list[dict]) -> dict:
    scored = [d for d in duels if d.get('session') != 1 and d.get('status') in ('deal', 'no_deal')]
    groups = defaultdict(list)
    reviews = []
    for d in scored:
        groups[(d.get('session'), d.get('role'))].append(d)
        result = d.get('result')
        if d['status'] == 'deal' and isinstance(result, (int, float)) and result < 0:
            price_margin = offer_surplus({**d, 'issues': ['price']}, {'price': d.get('price')})
            full_margin = offer_surplus(d, {'price': d.get('price'), 'days': d.get('days')})
            reviews.append({'duel': d.get('duel'), 'reason': 'Cierre negativo', 'priority': 0,
                            'result': result, 'price_margin': price_margin,
                            'days_effect': round(full_margin - price_margin, 1)
                            if full_margin is not None and price_margin is not None else None})
        elif d['status'] == 'no_deal':
            offers = [(offer_surplus(d, m), m.get('tick')) for m in (d.get('messages') or [])
                      if isinstance(m, dict) and m.get('from') == d.get('rival')]
            positive = [(v, tick) for v, tick in offers if isinstance(v, (int, float)) and v > 0]
            if positive:
                best, tick = max(positive)
                reviews.append({'duel': d.get('duel'), 'reason': 'Sin acuerdo con oferta positiva observada',
                                'priority': 1, 'best_offer_surplus': best, 'best_offer_tick': tick})
    cells = []
    for (session, role), ds in sorted(groups.items()):
        deals = [d for d in ds if d['status'] == 'deal']
        results = [d['result'] for d in deals if isinstance(d.get('result'), (int, float))]
        cells.append({'session': session, 'role': role, 'total': len(ds), 'deals': len(deals),
                      'negative': sum(v < 0 for v in results), 'no_deal': len(ds) - len(deals),
                      'net_result': round(sum(results), 1),
                      'mean_result_per_deal': round(sum(results) / len(results), 1) if results else None})
    rival_rows = []
    for rival in sorted({d.get('rival') for d in scored if d.get('rival')}):
        ds = [d for d in scored if d.get('rival') == rival]
        deals = [d for d in ds if d['status'] == 'deal']
        rival_rows.append({'rival': rival, 'total': len(ds), 'deals': len(deals),
                           'negative': sum((d.get('result') or 0) < 0 for d in deals),
                           'net_result': round(sum(d.get('result') or 0 for d in deals), 1)})
    reviews.sort(key=lambda r: (r['priority'], r.get('result', 0), -r.get('best_offer_surplus', 0)))
    return {'scored': len(scored), 'deals': sum(d['status'] == 'deal' for d in scored),
            'negative': sum(d['status'] == 'deal' and (d.get('result') or 0) < 0 for d in scored),
            'buyer_day_losses': sum(d['status'] == 'deal' and (d.get('result') or 0) < 0
                                    and d.get('role') == 'buyer' and 'days' in (d.get('issues') or [])
                                    for d in scored),
            'negative_sum': round(sum(d.get('result') or 0 for d in scored if (d.get('result') or 0) < 0), 1),
            'no_deal': sum(d['status'] == 'no_deal' for d in scored),
            'positive_offer_no_deal': sum(r['priority'] == 1 for r in reviews),
            'groups': cells, 'rivals': rival_rows, 'reviews': reviews}


def rows(duels: list[dict]) -> list[dict]:
    out = []
    for d in duels:
        if d.get('status') not in ('deal', 'no_deal'):
            continue
        result = d.get('result')
        status = d['status']
        practice = d.get('session') == 1
        kind = ('practice' if practice else 'missed' if status == 'no_deal' else
                'positive' if isinstance(result, (int, float)) and result > 0 else
                'negative' if isinstance(result, (int, float)) and result < 0 else 'flat')
        out.append({
            'duel': d.get('duel'), 'session': d.get('session'), 'status': status,
            'kind': kind, 'role': d.get('role'), 'rival': d.get('rival'),
            'item': d.get('item'), 'price': d.get('price'), 'days': d.get('days'),
            'result': result, 'limit': d.get('your_limit'),
            'days_weight': d.get('your_days_weight'), 'deadline_tick': d.get('deadline_tick'),
            'rounds': d.get('rounds'), 'messages': len(d.get('messages') or []),
        })
    return sorted(out, key=lambda x: (x['session'] or 0, x['deadline_tick'] or 0, x['duel'] or 0))


def render(duels: list[dict]) -> str:
    records = rows(duels)
    stats = analysis(duels)
    source = {d.get('duel'): d for d in duels}
    if not records:
        return ('<section id="duelos-historico" class="monitor"><h2>Histórico de duelos</h2>'
                '<p>Esperando la lectura privada de duelos cerrados.</p></section>')
    scored = [r for r in records if r['kind'] != 'practice']
    good = sum(r['kind'] == 'positive' for r in scored)
    bad = sum(r['kind'] == 'negative' for r in scored)
    missed = sum(r['kind'] == 'missed' for r in scored)
    vals = [abs(r['result']) for r in scored if isinstance(r['result'], (int, float))]
    max_abs = max(vals or [1])
    cell = max(5, min(10, 1050 / max(len(records), 1)))
    width = max(750, int(len(records) * cell + 80))
    mid, height = 112, 222
    bars = []
    for i, r in enumerate(records):
        result = r['result'] if isinstance(r['result'], (int, float)) else 0
        x = 45 + i * cell
        bar_h = max(3, abs(result) / max_abs * 87) if r['status'] == 'deal' else 3
        y = mid - bar_h if result > 0 else mid
        title = (f"Duelo {r['duel']} · sesión {r['session']} · {r['kind']} · "
                 f"resultado {result:+.1f} P · {r['rival']} · precio {r['price']} · días {r['days']}")
        bars.append(f'<rect class="{r["kind"]}" data-duel="{html.escape(str(r["duel"]))}" '
                    f'data-session="{html.escape(str(r["session"]))}" '
                    f'data-role="{html.escape(str(r["role"]))}" '
                    f'data-kind="{r["kind"]}" data-rival="{html.escape(str(r["rival"]), quote=True)}" '
                    f'data-review="{("yes" if any(q["duel"] == r["duel"] for q in stats["reviews"]) else "no")}" '
                    f'x="{x:.1f}" y="{y:.1f}" '
                    f'width="{max(3, cell - 2):.1f}" height="{bar_h:.1f}"><title>{html.escape(title)}</title></rect>')
    review_by_id = {r['duel']: r for r in stats['reviews']}
    rows_html = []
    for r in reversed(records):
        raw = source.get(r['duel']) or {}
        review = review_by_id.get(r['duel'])
        value = r['result'] if isinstance(r['result'], (int, float)) else None
        result = f'{value:+.1f} P' if value is not None else '—'
        status = ('Práctica' if r['kind'] == 'practice' else 'Sin trato' if r['kind'] == 'missed'
                  else 'Ganancia' if r['kind'] == 'positive' else 'Pérdida' if r['kind'] == 'negative' else 'Neutro')
        def fmt(v): return html.escape(str(v)) if v is not None else '—'
        messages = ''.join(
            f'<li><b>tick {fmt(m.get("tick"))} · {fmt(m.get("from"))}</b> '
            f'{fmt(m.get("text"))} <small>precio {fmt(m.get("price"))} · '
            f'días {fmt(m.get("days"))}</small></li>'
            for m in (raw.get('messages') or []) if isinstance(m, dict))
        def offer(label, value):
            if not isinstance(value, dict):
                return f'<p>{label}: sin oferta registrada</p>'
            return (f'<p>{label}: #{fmt(value.get("id"))} · {fmt(value.get("price"))} P · '
                    f'{fmt(value.get("days"))} días · tick {fmt(value.get("tick"))}</p>')
        detail = (f'<details class="duel-detail"><summary>#{fmt(r["duel"])} · ver secuencia</summary>'
                  f'<div><p>Cuestiones: {fmt(", ".join(raw.get("issues") or []))}. '
                  f'Límite propio: {fmt(r["limit"])} P; {fmt(raw.get("limit_meaning"))}. '
                  f'Valor/día: {fmt(r["days_weight"])}; {fmt(raw.get("days_meaning"))}. '
                  f'Decaimiento/ronda: {fmt(raw.get("decay_per_round"))}.</p>'
                  f'{offer("Nuestra oferta", raw.get("your_offer"))}'
                  f'{offer("Oferta rival", raw.get("rival_offer"))}'
                  f'<ol>{messages or "<li>Sin mensajes registrados.</li>"}</ol></div></details>')
        rows_html.append(
            f'<tr class="{r["kind"]}" data-duel="{fmt(r["duel"])}" '
            f'data-session="{fmt(r["session"])}" data-role="{fmt(r["role"])}" '
            f'data-kind="{fmt(r["kind"])}" data-rival="{fmt(r["rival"])}" '
            f'data-review="{("yes" if review else "no")}"><td>{detail}</td><td>{fmt(r["session"])}</td>'
            f'<td>{fmt(r["rival"])}</td><td>{fmt(r["item"])}</td><td>{fmt(r["role"])}</td>'
            f'<td>{status}</td><td>{fmt(r["price"])}</td><td>{fmt(r["days"])}</td>'
            f'<td class="result">{result}</td><td>{fmt(r["limit"])}</td>'
            f'<td>{fmt(r["days_weight"])}</td><td>{fmt(r["rounds"])}</td>'
            f'<td>{fmt(r["deadline_tick"])}</td><td>{fmt(r["messages"])}</td>'
            f'<td>{fmt(review["reason"]) if review else "—"}</td></tr>')
    group_cards = ''.join(
        f'<article class="duel-split"><b>Sesión {g["session"]} · {"Compra" if g["role"] == "buyer" else "Venta"}</b>'
        f'<strong>{g["net_result"]:+.1f} P</strong>'
        f'<span>{g["deals"]}/{g["total"]} acuerdos · {g["negative"]} pérdidas · '
        f'{g["no_deal"]} sin trato</span></article>' for g in stats['groups'])
    rival_cards = ''.join(
        f'<div class="duel-rival"><span>{html.escape(str(g["rival"]))}</span>'
        f'<b>{g["net_result"]:+.1f} P</b><small>{g["deals"]}/{g["total"]} acuerdos · '
        f'{g["negative"]} pérdidas</small></div>' for g in stats['rivals'])
    review_cards = ''.join(
        f'<button type="button" class="duel-review" data-jump-duel="{html.escape(str(q["duel"]))}">'
        f'<b>#{html.escape(str(q["duel"]))}</b><span>{html.escape(q["reason"])}</span>'
        f'<strong>{(f"{q["result"]:+.1f} P" if q["priority"] == 0 else f"hasta {q["best_offer_surplus"]:+.1f} P observado")}</strong>'
        f'</button>' for q in stats['reviews'])
    controls = ('<div class="duel-controls"><label>Sesión <select id="duel-filter-session">'
                '<option value="">Todas</option><option value="2">II</option><option value="3">III</option>'
                '<option value="1">Práctica</option></select></label>'
                '<label>Resultado <select id="duel-filter-kind"><option value="">Todos</option>'
                '<option value="positive">Ganancia</option><option value="negative">Pérdida</option>'
                '<option value="missed">Sin trato</option><option value="practice">Práctica</option></select></label>'
                '<label>Rol <select id="duel-filter-role"><option value="">Ambos</option>'
                '<option value="buyer">Compra</option><option value="seller">Venta</option></select></label>'
                '<label>Rival <select id="duel-filter-rival"><option value="">Todos</option>'
                + ''.join(f'<option value="{html.escape(str(g["rival"]), quote=True)}">'
                          f'{html.escape(str(g["rival"]))}</option>' for g in stats['rivals'])
                + '</select></label><label class="duel-review-only"><input id="duel-filter-review" type="checkbox">'
                  ' Solo revisar</label><span id="duel-visible-count"></span></div>')
    return (
        '<section id="duelos-historico" class="monitor duel-history">'
        '<div class="section-head"><div><h2>Histórico de duelos cerrados</h2>'
        '<p class="sub">Cada barra es un duelo; pasa el cursor para ver su resultado. '
        'La tabla muestra todos los detalles disponibles en la API.</p></div>'
        f'<span class="duel-history-count">{len(scored)} puntuables · {good} positivos · {bad} negativos · {missed} sin trato</span></div>'
        '<div class="duel-insight-grid">'
        f'<article><small>Pérdidas confirmadas</small><strong>{stats["negative_sum"]:+.1f} P</strong>'
        f'<span>{stats["negative"]} acuerdos negativos; {stats["buyer_day_losses"]} son compras con días.</span></article>'
        f'<article><small>Sin cierre con oferta positiva</small><strong>{stats["positive_offer_no_deal"]}</strong>'
        '<span>Hubo una oferta rival positiva observada; no confirma que siguiera disponible al cierre.</span></article>'
        f'<article><small>Acuerdos puntuables</small><strong>{stats["deals"]}/{stats["scored"]}</strong>'
        '<span>Un no-trato da cero; cerrar con excedente negativo resta.</span></article></div>'
        '<div class="duel-analysis-columns"><div><h3>Sesión y rol</h3><div class="duel-splits">'
        f'{group_cards}</div></div><div><h3>Rivales</h3><div class="duel-rivals">{rival_cards}</div></div></div>'
        '<div class="duel-review-head"><div><h3>Revisar primero</h3>'
        '<p>Prioridad: pérdidas confirmadas; después, no-tratos con una oferta favorable observada.</p></div>'
        '<span>Abre el duelo en la tabla</span></div>'
        f'<div class="duel-review-list">{review_cards or "Sin casos para revisar."}</div>'
        '<p class="chart-note">El excedente de una oferta no aceptada es una oportunidad observada, no una ganancia perdida demostrada. '
        'El historial no identifica quién operó cada cierre si falta el log del ejecutor.</p>'
        f'{controls}'
        '<div class="duel-legend"><span class="positive">Ganancia</span><span class="negative">Pérdida</span>'
        '<span class="missed">Sin trato</span><span class="practice">Práctica</span></div>'
        '<div class="duel-plot"><svg role="img" aria-label="Resultado de cada duelo cerrado, ordenado por sesión y tick" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f'<line x1="40" x2="{width - 10}" y1="{mid}" y2="{mid}" stroke="#8298a0" stroke-width="1"/>'
        f'{"".join(bars)}</svg></div>'
        '<p class="chart-note">Resultado = excedente del duelo publicado por la API, no puntos directos de leaderboard. '
        'La práctica se muestra en gris y queda fuera del recuento puntuable.</p>'
        '<div class="duel-table-wrap"><table class="duel-table"><thead><tr>'
        '<th>Duelo</th><th>Sesión</th><th>Rival</th><th>Objeto</th><th>Rol</th><th>Estado</th>'
        '<th>Precio</th><th>Días</th><th>Resultado</th><th>Límite</th><th>Valor/día</th>'
        '<th>Rondas</th><th>Tick límite</th><th>Mensajes</th><th>Revisión</th></tr></thead><tbody>'
        f'{"".join(rows_html)}</tbody></table></div>'
        '<script>(function(){const root=document.getElementById("duelos-historico");if(!root)return;'
        'const ids=["session","kind","role","rival"];'
        'const field=k=>document.getElementById("duel-filter-"+k).value;'
        'const review=document.getElementById("duel-filter-review");'
        'const items=root.querySelectorAll(".duel-table tbody tr,.duel-plot rect[data-duel]");'
        'function apply(){let n=0;items.forEach(el=>{const ok=ids.every(k=>!field(k)||el.dataset[k]===field(k))'
        '&&(!review.checked||el.dataset.review==="yes");el.style.display=ok?"":"none";'
        'if(ok&&el.tagName==="TR")n++});document.getElementById("duel-visible-count").textContent=n+" visibles";}'
        'ids.forEach(k=>document.getElementById("duel-filter-"+k).addEventListener("change",apply));'
        'review.addEventListener("change",apply);'
        'root.querySelectorAll("[data-jump-duel],.duel-plot rect[data-duel]").forEach(el=>el.addEventListener("click",()=>{'
        'ids.forEach(k=>document.getElementById("duel-filter-"+k).value="");review.checked=false;apply();'
        'const id=el.dataset.jumpDuel||el.dataset.duel;const row=root.querySelector(`tr[data-duel="${id}"]`);'
        'if(row){row.scrollIntoView({behavior:"smooth",block:"center"});row.querySelector("details").open=true;'
        'row.classList.add("focused");setTimeout(()=>row.classList.remove("focused"),1800)}}));apply()})()</script>'
        '</section>')
