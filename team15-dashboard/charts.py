"""Gráficas en SVG en línea. Sin dependencias y sin red: el panel es un solo HTML.

Decisiones que afectan a cómo se lee:
- Eje Y acotado al rango de los datos con un 8 % de aire, salvo cuando se pasa `lo`/`hi`
  explícitos (los componentes del score van de 0 a 30 y verlos sobre su escala real es
  el punto: 7.50 de 30 no parece gran cosa hasta que se dibuja).
- Los huecos no se rellenan: una serie con dos muestras se dibuja con dos puntos.
- `baseline` dibuja una referencia horizontal (el suelo de 7.50 del mercado, la
  eficiencia del mejor del juego) porque una línea sin referencia no dice si va bien.
"""
from __future__ import annotations

from html import escape

PALETTE = ("#1f8a76", "#d98c1f", "#6c5ce7", "#c0392b", "#2d7dd2")


def _scale(values, lo=None, hi=None, pad=0.08):
    """El aire del 8 % se aplica SÓLO a los extremos inferidos.

    Acolchar un límite explícito rompe el sentido de darlo: `lo=0, hi=30` dibujaba el eje
    de -2.40 a 32.40, y el punto de poner los componentes sobre su escala real es que 0 y
    30 sean 0 y 30."""
    auto_lo, auto_hi = lo is None, hi is None
    lo = min(values) if auto_lo else lo
    hi = max(values) if auto_hi else hi
    if hi == lo:
        hi, lo = hi + 1, lo - 1
        auto_lo = auto_hi = True
    span = hi - lo
    return (lo - span * pad if auto_lo else lo), (hi + span * pad if auto_hi else hi)


def line_chart(series: dict, *, width: int = 520, height: int = 170, lo=None, hi=None,
               baseline=None, baseline_label: str = "", invert: bool = False,
               value_fmt: str = "{:.2f}", title: str = "", caption: str = "") -> str:
    """`series` es {etiqueta: [(x, y), ...]}. `invert=True` para el puesto, donde 1 es arriba."""
    drawn = {k: v for k, v in (series or {}).items() if v}
    if not drawn:
        return (f'<figure class="chart empty"><figcaption>{escape(title)}</figcaption>'
                f'<p class="chart-note">Sin historia todavía: hace falta más de una muestra.</p></figure>')
    xs = [x for points in drawn.values() for x, _ in points]
    ys = [y for points in drawn.values() for _, y in points]
    if baseline is not None:
        ys = ys + [baseline]
    x0, x1 = min(xs), max(xs)
    y0, y1 = _scale(ys, lo, hi)
    pad_l, pad_r, pad_t, pad_b = 44, 12, 14, 22
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def px(x):
        return pad_l + (0 if x1 == x0 else (x - x0) / (x1 - x0) * iw)

    def py(y):
        t = 0.5 if y1 == y0 else (y - y0) / (y1 - y0)
        return pad_t + (t * ih if invert else (1 - t) * ih)

    parts = [f'<figure class="chart"><figcaption>{escape(title)}</figcaption>',
             f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" '
             f'preserveAspectRatio="none">']
    for frac in (0.0, 0.5, 1.0):
        y = y0 + (y1 - y0) * frac
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{py(y):.1f}" x2="{width - pad_r}" y2="{py(y):.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 6}" y="{py(y) + 3:.1f}" text-anchor="end">'
                     f'{escape(value_fmt.format(y))}</text>')
    if baseline is not None:
        parts.append(f'<line class="baseline" x1="{pad_l}" y1="{py(baseline):.1f}" '
                     f'x2="{width - pad_r}" y2="{py(baseline):.1f}"/>')
        if baseline_label:
            parts.append(f'<text class="tick base" x="{width - pad_r}" y="{py(baseline) - 5:.1f}" '
                         f'text-anchor="end">{escape(baseline_label)}</text>')
    for i, (label, points) in enumerate(drawn.items()):
        colour = PALETTE[i % len(PALETTE)]
        d = " ".join(("M" if j == 0 else "L") + f"{px(x):.1f},{py(y):.1f}"
                     for j, (x, y) in enumerate(points))
        parts.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2.2" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        lx, ly = points[-1]
        parts.append(f'<circle cx="{px(lx):.1f}" cy="{py(ly):.1f}" r="3.2" fill="{colour}"/>')
    parts.append(f'<text class="tick" x="{pad_l}" y="{height - 6}">tick {x0}</text>')
    parts.append(f'<text class="tick" x="{width - pad_r}" y="{height - 6}" text-anchor="end">tick {x1}</text>')
    parts.append('</svg>')
    if len(drawn) > 1:
        parts.append('<div class="chart-keys">')
        for i, (label, points) in enumerate(drawn.items()):
            last = value_fmt.format(points[-1][1])
            parts.append(f'<span class="key"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>'
                         f'{escape(str(label))} <b>{escape(last)}</b></span>')
        parts.append('</div>')
    if caption:
        parts.append(f'<p class="chart-note">{escape(caption)}</p>')
    parts.append('</figure>')
    return "".join(parts)


CSS = """
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
"""
