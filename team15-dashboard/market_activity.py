"""Snapshot and presentation helpers for currently circulating venue offers."""
from __future__ import annotations

import html


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _side(side: dict | None) -> tuple[list[str], int]:
    side = side if isinstance(side, dict) else {}
    items = []
    for asset in side.get("assets") or []:
        if isinstance(asset, dict):
            items.append(str(asset.get("ref") or asset.get("kind") or "activo"))
    items.extend(str(ref) for ref in side.get("cards") or [])
    items.extend(str(value).removeprefix("card:") for value in side.get("types") or [])
    return items, int(side.get("cash") or 0)


def _kind(give: dict, want: dict) -> str:
    gc, g_cash = _side(give)
    wc, w_cash = _side(want)
    if gc and w_cash and not wc:
        return "Venta"
    if g_cash and wc and not gc:
        return "Compra"
    if gc and wc:
        return "Canje"
    return "Otra oferta"


def build(venues: list[dict], boards: dict[str, list[dict]], own_offers: list[dict],
          team: str = "t15", tick: int | None = None) -> dict:
    venue_map = {v.get("venue"): v for v in venues or [] if v.get("venue")}
    own_venue = next((v.get("venue") for v in venues or [] if v.get("owner") == team), None)
    rows = []
    seen = set()
    for venue_id, offers in (boards or {}).items():
        for raw in offers or []:
            if not isinstance(raw, dict) or raw.get("status", "open") != "open":
                continue
            row = dict(raw)
            row["venue"] = row.get("venue") or venue_id
            key = (row.get("venue"), row.get("id"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    own_rows = []
    for raw in own_offers or []:
        if not isinstance(raw, dict) or raw.get("status") != "open":
            continue
        row = dict(raw)
        row["maker"] = row.get("maker") or team
        row["venue"] = row.get("venue") or own_venue
        key = (row.get("venue"), row.get("id"))
        if key not in seen:
            rows.append(row)
            seen.add(key)
        own_rows.append(row)

    def present(row):
        give_cards, give_cash = _side(row.get("give"))
        want_cards, want_cash = _side(row.get("want"))
        venue_id = row.get("venue")
        venue = venue_map.get(venue_id) or {}
        expires = row.get("expires_tick")
        return {
            "id": row.get("id"), "venue": venue_id,
            "venue_name": venue.get("name") or ("El Rastro" if venue_id == "rastro" else venue_id or "—"),
            "maker": row.get("maker") or "—", "to": row.get("to"),
            "kind": _kind(row.get("give"), row.get("want")),
            "gives": give_cards, "wants": want_cards,
            "cash_give": give_cash, "cash_want": want_cash,
            "expires_tick": expires,
            "ticks_left": max(0, int(expires) - int(tick)) if expires is not None and tick is not None else None,
            "thread": row.get("thread"),
            "mine": row.get("maker") == team,
        }

    offers = [present(r) for r in rows]
    mine = [present(r) for r in own_rows]
    own_market = [r for r in offers if r["venue"] == own_venue]
    my_sales = [r for r in mine if r["kind"] == "Venta"]
    all_sales = [r for r in mine if r["kind"] == "Venta"]
    return {"tick": tick, "own_venue": own_venue,
            "own_venue_name": (venue_map.get(own_venue) or {}).get("name") or own_venue or "—",
            "my_open_offers": mine, "my_active_sales": all_sales,
            "own_market_offers": own_market, "all_offers": offers,
            "summary": {"active_sales": len(my_sales), "my_open_offers": len(mine),
                        "own_market_total": len(own_market),
                        "own_market_by_others": sum(not r["mine"] for r in own_market),
                        "open_offers_total": len(offers),
                        "active_venues": len({r["venue"] for r in offers})}}


def render(data: dict) -> str:
    watch = data.get("market_activity") or {}
    summary = watch.get("summary") or {}
    sales = watch.get("my_active_sales") or []
    own_market = watch.get("own_market_offers") or []
    all_offers = watch.get("all_offers") or []

    def money(value):
        return f"{int(value):,} P".replace(",", " ") if value else "—"

    def cards(values):
        return ", ".join(_e(v) for v in values) if values else "—"

    def table(rows, *, mine=False):
        if not rows:
            return '<p class="market-empty">No hay ofertas abiertas en esta vista.</p>'
        body = []
        for row in rows:
            expiry = f"tick { _e(row['expires_tick']) } · quedan {_e(row['ticks_left'])}" if row.get("expires_tick") is not None else "Sin caducidad publicada"
            addressed = f" · dirigida a {_e(row['to'])}" if row.get("to") else " · pública"
            body.append(
                '<tr><td><b>#' + _e(row.get("id")) + '</b><small>' + _e(row.get("kind")) + '</small></td>'
                '<td><b>' + _e(row.get("venue_name")) + '</b><small>' + _e(row.get("venue")) + '</small></td>'
                '<td><b>' + _e(row.get("maker")) + '</b><small>' + addressed + '</small></td>'
                '<td>' + cards(row.get("gives") or []) + '<small>Entrega · ' + money(row.get("cash_give")) + '</small></td>'
                '<td>' + cards(row.get("wants") or []) + '<small>Pide · ' + money(row.get("cash_want")) + '</small></td>'
                '<td>' + _e(expiry) + ('<small>Tuya</small>' if row.get("mine") else '') + '</td></tr>')
        return ('<div class="market-table-scroll"><table class="market-table"><thead><tr>'
                '<th>Oferta</th><th>Mercado</th><th>Publica</th><th>Entrega</th><th>Solicita</th><th>Caducidad</th>'
                '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>')

    if sales:
        headline = f"Sí: tienes {len(sales)} oferta(s) de venta abierta(s)."
        venues = ", ".join(sorted({r["venue_name"] for r in sales}))
        headline += f" Publicadas en: {venues}."
    else:
        headline = "No tienes ventas abiertas detectadas en tus ofertas activas."
    return (
        '<section id="mercado-vivo" class="monitor market-live"><div class="section-head"><div>'
        '<p class="eyebrow">Libro en vivo · solo lectura</p><h2>Qué está circulando ahora</h2>'
        '<p class="sub">Tick ' + _e(watch.get("tick") or "—") + ' · Tus publicaciones y el libro público por mercado.</p></div>'
        '<span class="market-live-badge">' + _e(watch.get("own_venue_name") or "Mercado propio") + '</span></div>'
        '<div class="market-live-headline"><b>' + _e(headline) + '</b><span>El estado refleja ofertas abiertas al último refresco.</span></div>'
        '<div class="market-live-kpis">'
        '<article><small>Tus ventas abiertas</small><strong>' + _e(summary.get("active_sales", 0)) + '</strong></article>'
        '<article><small>Tus ofertas abiertas</small><strong>' + _e(summary.get("my_open_offers", 0)) + '</strong></article>'
        '<article><small>Ofertas en tu mercado</small><strong>' + _e(summary.get("own_market_total", 0)) + '</strong></article>'
        '<article><small>De otros equipos en tu mercado</small><strong>' + _e(summary.get("own_market_by_others", 0)) + '</strong></article>'
        '</div><details open><summary>Mis publicaciones activas</summary>' + table(watch.get("my_open_offers") or [], mine=True) + '</details>'
        '<details open><summary>Ofertas abiertas en ' + _e(watch.get("own_venue_name") or "mi mercado") + '</summary>'
        + table(own_market) + '</details><details><summary>Todos los mercados · ' + _e(summary.get("open_offers_total", 0))
        + ' ofertas en ' + _e(summary.get("active_venues", 0)) + ' mercados</summary>' + table(all_offers)
        + '</details></section>')
