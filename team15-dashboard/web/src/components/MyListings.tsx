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

export function MyListings({ offers = [], venue = {}, tick, shareMessage }: {
  offers?: JsonRecord[]; venue?: JsonRecord; tick?: number; shareMessage?: string
}) {
  const [copied, setCopied] = useState(false)
  const active = offers.filter((offer) => offer && offer.id != null)
  const sales = active.filter((offer) => offer.kind === 'Venta').length
  const buys = active.filter((offer) => offer.kind === 'Compra').length
  const swaps = active.length - sales - buys
  const fallbackMessage = `🏪 TEAM 15 MARKET · ${venue.id || 'v15'}\n📍 ${venue.name || 'Team 15 market'}\n🕒 Snapshot tick ${tick ?? '—'}\n\n🔄 Have a missing card or a spare? Post a public offer on v15 — card-for-card swaps welcome. We can help spot matches between teams and complete sets.`
  async function copyMarket() {
    try { await navigator.clipboard.writeText(shareMessage || fallbackMessage); setCopied(true); window.setTimeout(() => setCopied(false), 2200) }
    catch { setCopied(false) }
  }

  return <Panel title="Lo que tienes publicado" detail="Ofertas abiertas tuyas, identificadas por mercado y tick de caducidad."
    action={<div className="panel-actions"><button className="copy-market-button" onClick={() => void copyMarket()} type="button"><Clipboard size={13} />{copied ? 'Copiado' : 'Copiar mensaje para el grupo'}</button><a className="panel-link" href="?view=market">Abrir mercado <ExternalLink size={13} /></a></div>}>
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
          <small>{offer.expires_tick == null ? 'Caducidad no informada' : `caduca en tick ${num(offer.expires_tick, 0)}${offer.expires_at?.label ? ` · ${offer.expires_at.label}` : ''}`}</small></div>
      </article>)}
    </div> : <Empty>No aparecen publicaciones abiertas tuyas en la última captura.</Empty>}
    <span className="copy-market-status" aria-live="polite">{copied ? 'Mensaje listo para pegar en WhatsApp.' : ''}</span>
    <p className="listing-footnote">El texto para el grupo incluye solo ofertas públicas de v15; no comparte valoraciones privadas, ofertas entrantes ni publicaciones tuyas en otros mercados.</p>
    <p className="listing-footnote">Solo muestra ofertas que el feed identifica como tuyas y abiertas; no confirma ventas ni ofertas ya caducadas.</p>
  </Panel>
}
