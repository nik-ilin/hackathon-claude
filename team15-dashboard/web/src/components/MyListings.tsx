import { useState } from 'react'
import { ArrowRightLeft, Clipboard, ExternalLink, Store } from 'lucide-react'
import type { JsonRecord } from '../lib/types'
import { num } from '../lib/format'
import { Empty, Panel } from './Panel'
import './MyListings.css'

function side(cards: unknown, cash: unknown) {
  const items = Array.isArray(cards) ? cards.map(String) : []
  const amount = typeof cash === 'number' && cash > 0 ? `${num(cash, 0)} P` : null
  return [...items, ...(amount ? [amount] : [])].join(' + ') || 'Nada indicado'
}

export function MyListings({ offers = [], incoming = [], publicOffers = [], cards = [], venue = {}, tick }: {
  offers?: JsonRecord[]; incoming?: JsonRecord[]; publicOffers?: JsonRecord[]; cards?: JsonRecord[]; venue?: JsonRecord; tick?: number
}) {
  const [copied, setCopied] = useState(false)
  const active = offers.filter((offer) => offer && offer.id != null)
  const sales = active.filter((offer) => offer.kind === 'Venta').length
  const buys = active.filter((offer) => offer.kind === 'Compra').length
  const swaps = active.length - sales - buys
  const cardMap = new Map(cards.map((card) => [card.ref, card]))
  const sideText = (refs: unknown, cash: unknown) => [...(Array.isArray(refs) ? refs.map(String) : []), ...(typeof cash === 'number' && cash > 0 ? [`${num(cash, 0)} P`] : [])].join(' + ') || '—'
  const noteFor = (offer: JsonRecord) => {
    const ref = [...(offer.gives || []), ...(offer.wants || [])][0]
    const card = cardMap.get(ref)
    if (!card) return 'Private comparison unavailable; verify before accepting.'
    if (offer.kind === 'Venta' && !offer.mine && typeof offer.cash_want === 'number') {
      return typeof card.buy_ceiling === 'number' ? `Ask is ${num(offer.cash_want - card.buy_ceiling)} P above our ${num(card.buy_ceiling)} P buy cap; not a profitable buy at this price.` : 'Our private buy cap is unavailable.'
    }
    if (offer.kind === 'Venta' && offer.mine && typeof card.sell_floor === 'number') {
      const gap = Number(offer.cash_want || 0) - card.sell_floor
      return `Our ask is ${num(Math.abs(gap))} P ${gap >= 0 ? 'above' : 'below'} the ${num(card.sell_floor)} P private sale floor${card.sell_breaks_page ? '; selling may break a page' : ''}.`
    }
    if (offer.kind === 'Compra' && typeof offer.cash_give === 'number') {
      if (offer.mine && typeof card.buy_ceiling === 'number') return `Our bid ${num(offer.cash_give)} P is ${offer.cash_give <= card.buy_ceiling ? 'within' : `${num(offer.cash_give - card.buy_ceiling)} P above`} our ${num(card.buy_ceiling)} P cap.`
      if (!offer.mine && typeof card.sell_floor === 'number') return `Bid ${num(offer.cash_give)} P vs ${num(card.sell_floor)} P sale floor; ${card.free > 0 ? 'free copy available' : 'no free duplicate; check collection impact'}.`
    }
    return 'Compare both collections and the cash before accepting.'
  }
  const activeBuys = active.filter((offer) => offer.kind === 'Compra')
  const marketMessage = [
    `🏪 EL DUENDE · TEAM 15 · MARKET ${venue.id || 'v15'}`,
    `📍 ${venue.name || 'Puesto de Team 15'}`,
    `🕒 Snapshot tick ${tick ?? '—'}`,
    '',
    `📌 PUBLIC OFFERS ON ${venue.id || 'v15'} (${publicOffers.length}):`,
    ...(publicOffers.length ? publicOffers.map((offer) => `${offer.mine ? '• Team 15' : '• Another team'} · ${offer.kind}: ${sideText(offer.gives, offer.cash_give)} → ${sideText(offer.wants, offer.cash_want)}${offer.ticks_left == null ? '' : ` · ${num(offer.ticks_left, 0)} ticks left`}\n  Why it matters: ${noteFor(offer)}`) : ['• No active public offers at the latest refresh.']),
    '', `🛒 WHAT TEAM 15 IS BUYING (${activeBuys.length} active requests):`,
    ...(activeBuys.length ? activeBuys.map((offer) => `• ${offer.venue_name || offer.venue} (${offer.venue}) · ${sideText(offer.wants, offer.cash_want)} · ${noteFor(offer)}`) : ['• No active Team 15 buy requests.']),
    '', `📨 OFFERS ADDRESSED TO TEAM 15 (${incoming.length}):`,
    ...(incoming.length ? incoming.map((offer) => `• ${offer.venue_name || offer.venue} (${offer.venue}) · ${offer.kind}: ${sideText(offer.gives, offer.cash_give)} → ${sideText(offer.wants, offer.cash_want)} · ${noteFor(offer)}`) : ['• No incoming offers addressed to us.']),
    '', `🧾 OUR OPEN LISTINGS ACROSS MARKETS (${active.length}):`,
    ...(active.length ? active.map((offer) => `• ${offer.venue_name || offer.venue} (${offer.venue}) · ${offer.kind}: ${sideText(offer.gives, offer.cash_give)} → ${sideText(offer.wants, offer.cash_want)}`) : ['• No Team 15 listings currently open.']),
  ].join('\n')
  async function copyMarket() {
    try { await navigator.clipboard.writeText(marketMessage); setCopied(true); window.setTimeout(() => setCopied(false), 2200) }
    catch { setCopied(false) }
  }

  return <Panel title="Lo que tienes publicado" detail="Ofertas abiertas tuyas, identificadas por mercado y tick de caducidad."
    action={<div className="panel-actions"><button className="copy-market-button" onClick={() => void copyMarket()} type="button"><Clipboard size={13} />{copied ? 'Copiado' : 'Copiar WhatsApp'}</button><a className="panel-link" href="?view=market">Abrir mercado <ExternalLink size={13} /></a></div>}>
    <div className="listing-summary" aria-label="Resumen de publicaciones activas">
      <span><b>{active.length}</b> activas</span><span>{sales} ventas</span><span>{buys} compras</span><span>{swaps} canjes / otras</span>
    </div>
    {active.length ? <div className="listing-list">
      {active.map((offer) => <article className="listing-row" key={`${offer.venue}-${offer.id}`}>
        <div className="listing-market"><Store size={15} /><div><b>{offer.venue_name || offer.venue || venue.name || 'Mercado sin identificar'}</b>
          <small>{offer.venue || venue.id || '—'} · oferta #{offer.id}{offer.to ? ` · dirigida a ${offer.to}` : ' · pública'}</small></div></div>
        <span className={`listing-kind listing-${String(offer.kind || 'otra').toLowerCase()}`}>{offer.kind || 'Oferta'}</span>
        <div className="listing-exchange"><span><small>Ofreces</small><b>{side(offer.gives, offer.cash_give)}</b></span>
          <ArrowRightLeft size={14} aria-hidden="true" /><span><small>Pides</small><b>{side(offer.wants, offer.cash_want)}</b></span></div>
        <div className="listing-expiry"><b>{offer.ticks_left == null ? 'Sin tick de caducidad' : `${num(offer.ticks_left, 0)} ticks`}</b>
          <small>{offer.expires_tick == null ? 'Caducidad no informada' : `caduca en tick ${num(offer.expires_tick, 0)}`}</small></div>
      </article>)}
    </div> : <Empty>No aparecen publicaciones abiertas tuyas en la última captura.</Empty>}
    <span className="copy-market-status" aria-live="polite">{copied ? 'Mensaje listo para pegar en WhatsApp.' : ''}</span>
    <p className="listing-footnote">Solo muestra ofertas que el feed identifica como tuyas y abiertas; no confirma ventas ni ofertas ya caducadas.</p>
  </Panel>
}
