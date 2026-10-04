"""Vista compacta de resultados confirmados por /api/duels?done=true."""
from __future__ import annotations

import html


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
        bars.append(f'<rect class="{r["kind"]}" x="{x:.1f}" y="{y:.1f}" '
                    f'width="{max(3, cell - 2):.1f}" height="{bar_h:.1f}"><title>{html.escape(title)}</title></rect>')
    rows_html = []
    for r in reversed(records):
        raw = source.get(r['duel']) or {}
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
            f'<tr class="{r["kind"]}"><td>{detail}</td><td>{fmt(r["session"])}</td>'
            f'<td>{fmt(r["rival"])}</td><td>{fmt(r["item"])}</td><td>{fmt(r["role"])}</td>'
            f'<td>{status}</td><td>{fmt(r["price"])}</td><td>{fmt(r["days"])}</td>'
            f'<td class="result">{result}</td><td>{fmt(r["limit"])}</td>'
            f'<td>{fmt(r["days_weight"])}</td><td>{fmt(r["rounds"])}</td>'
            f'<td>{fmt(r["deadline_tick"])}</td><td>{fmt(r["messages"])}</td></tr>')
    return (
        '<section id="duelos-historico" class="monitor duel-history">'
        '<div class="section-head"><div><h2>Histórico de duelos cerrados</h2>'
        '<p class="sub">Cada barra es un duelo; pasa el cursor para ver su resultado. '
        'La tabla muestra todos los detalles disponibles en la API.</p></div>'
        f'<span class="duel-history-count">{len(scored)} puntuables · {good} positivos · {bad} negativos · {missed} sin trato</span></div>'
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
        '<th>Rondas</th><th>Tick límite</th><th>Mensajes</th></tr></thead><tbody>'
        f'{"".join(rows_html)}</tbody></table></div></section>')
