import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, BarChart3, Boxes, CircleDollarSign, Clock3, Command, LayoutDashboard, Menu, Radio, RefreshCw, Search, Shield, Store, Swords, Users, X } from 'lucide-react'
import type { JsonRecord, ViewKey } from './lib/types'
import { ageLabel } from './lib/format'
import { getSection } from './lib/api'
import { Overview } from './views/Overview'
import { Ranking } from './views/Ranking'
import { Agents, Collection, Duels, Market, Operations, Rivals, Signals, Trading } from './views/OperationalViews'
import './styles/tokens.css'
import './styles/app.css'

const views: { key: ViewKey; label: string; icon: typeof LayoutDashboard; group: string; hint: string }[] = [
  { key: 'overview', label: 'Resumen global', icon: LayoutDashboard, group: '01 · Mando', hint: 'score puesto alertas' },
  { key: 'ranking', label: 'Ranking', icon: BarChart3, group: '01 · Mando', hint: 'carrera brechas histórico' },
  { key: 'operations', label: 'Operaciones', icon: Activity, group: '02 · Ejecución', hint: 'agentes duelos caja' },
  { key: 'duels', label: 'Duelos', icon: Swords, group: '02 · Ejecución', hint: 'histórico cierres rentabilidad' },
  { key: 'market', label: 'Mercado', icon: Store, group: '03 · Intercambio', hint: 'ofertas libros mercados' },
  { key: 'trading', label: 'Compras y ventas', icon: CircleDollarSign, group: '03 · Intercambio', hint: 'liquidaciones márgenes dealers' },
  { key: 'collection', label: 'Colección', icon: Boxes, group: '04 · Inteligencia', hint: 'cartas páginas duplicados packs' },
  { key: 'rivals', label: 'Rivales', icon: Users, group: '04 · Inteligencia', hint: 'equipos posesión demanda' },
  { key: 'signals', label: 'Señales', icon: Radio, group: '04 · Inteligencia', hint: 'radio noticias fuente' },
  { key: 'agents', label: 'Salud de agentes', icon: Shield, group: '05 · Sistema', hint: 'memoria fuentes datos' },
]
const validViews = new Set(views.map((v) => v.key))
function initialView(): ViewKey { const candidate = new URLSearchParams(location.search).get('view'); return validViews.has(candidate as ViewKey) ? candidate as ViewKey : 'overview' }

export default function App() {
  const [view, setView] = useState<ViewKey>(initialView)
  const [cache, setCache] = useState<Partial<Record<ViewKey, JsonRecord>>>({})
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [lastSuccess, setLastSuccess] = useState<number | null>(null)
  const [snapshot, setSnapshot] = useState<JsonRecord | null>(null)
  const [sources, setSources] = useState<JsonRecord>({})
  const [search, setSearch] = useState('')
  const [mobileOpen, setMobileOpen] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(async (target: ViewKey, quiet = false) => {
    abortRef.current?.abort()
    const abort = new AbortController(); abortRef.current = abort
    if (!quiet && !cache[target]) setLoading(true)
    try {
      const result = await getSection(target, abort.signal)
      setCache((previous) => ({ ...previous, [target]: result.data }))
      setSnapshot(result.snapshot); setSources(result.sources); setLastSuccess(Date.now()); setError('')
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      setError(reason instanceof Error ? reason.message : 'No se pudo actualizar el dashboard')
    } finally { setLoading(false) }
  }, [cache])

  useEffect(() => {
    const url = new URL(location.href); url.searchParams.set('view', view); history.replaceState(null, '', url)
    setMobileOpen(false); void load(view)
  }, [view]) // load is refreshed below on the active tab timer; keep route changes deterministic

  useEffect(() => {
    const id = window.setInterval(() => void load(view, true), 15_000)
    return () => window.clearInterval(id)
  }, [view, load])

  useEffect(() => {
    const onPop = () => setView(initialView())
    const onKeys = (event: KeyboardEvent) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchRef.current?.focus() } if (event.key === 'Escape') { setSearch(''); searchRef.current?.blur(); setMobileOpen(false) } }
    window.addEventListener('popstate', onPop); window.addEventListener('keydown', onKeys)
    return () => { window.removeEventListener('popstate', onPop); window.removeEventListener('keydown', onKeys); abortRef.current?.abort() }
  }, [])

  const currentMeta = views.find((item) => item.key === view)!
  const currentData = cache[view] || {}
  const matched = useMemo(() => {
    const q = search.trim().toLowerCase()
    return q ? views.filter((item) => `${item.label} ${item.group} ${item.hint}`.toLowerCase().includes(q)) : views
  }, [search])
  function navigate(target: ViewKey) { if (target !== view) { const url = new URL(location.href); url.searchParams.set('view', target); history.pushState(null, '', url); setView(target) } else setMobileOpen(false) }

  return <div className="app-shell">
    <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
      <div className="brand"><div className="brand-mark">15</div><div><b>BAZAAR DESK</b><small>TEAM 15 · OPERATIONS</small></div><button className="mobile-close" onClick={() => setMobileOpen(false)} aria-label="Cerrar navegación"><X size={18} /></button></div>
      <div className="team-chip"><span className="live-dot" /> <span>TEAM 15</span><small>{snapshot?.tick == null ? 'local feed' : `tick ${snapshot.tick}`}</small></div>
      <nav className="side-nav" aria-label="Secciones del dashboard">{['01 · Mando', '02 · Ejecución', '03 · Intercambio', '04 · Inteligencia', '05 · Sistema'].map((group) => {
        const items = matched.filter((item) => item.group === group)
        return items.length ? <div className="nav-group" key={group}><h2>{group}</h2>{items.map((item) => { const Icon = item.icon; return <button className={view === item.key ? 'nav-item active' : 'nav-item'} key={item.key} onClick={() => navigate(item.key)} aria-current={view === item.key ? 'page' : undefined}><Icon size={16} /><span>{item.label}</span>{view === item.key && <i />}</button> })}</div> : null
      })}</nav>
      <div className="sidebar-foot"><Shield size={13} /><span>Panel de lectura · sin ejecución</span></div>
    </aside>
    {mobileOpen && <button className="mobile-scrim" onClick={() => setMobileOpen(false)} aria-label="Cerrar menú" />}
    <main className="main-shell">
      <header className="topbar"><button className="mobile-menu" aria-label="Abrir navegación" onClick={() => setMobileOpen(true)}><Menu size={19} /></button>
        <label className="global-search"><Search size={15} /><input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Buscar sección…" aria-label="Buscar módulo" /><kbd><Command size={11} /> K</kbd></label>
        <div className="topbar-meta"><span className={`feed-indicator ${error ? 'degraded' : 'live'}`}><i />{error ? 'Datos retrasados' : 'Conectado'}</span><span className="topbar-updated"><Clock3 size={13} />{lastSuccess ? ageLabel(Math.floor(lastSuccess / 1000)) : 'Esperando captura'}</span><button className="icon-button" aria-label="Actualizar ahora" onClick={() => void load(view)}><RefreshCw size={15} /></button></div>
      </header>
      <div className="content-shell">
        {error && <div className="stale-banner" role="status"><AlertTriangle size={16} /><span>{error}{cache[view] ? ' · Se mantiene visible la última captura válida.' : ''}</span><button onClick={() => void load(view)}>Reintentar</button></div>}
        {loading && !cache[view] ? <div className="loading-state"><span className="loading-pulse" /> Cargando {currentMeta.label.toLowerCase()}…</div> : <>
          {view === 'overview' && <Overview data={{ ...currentData, snapshot, health: { ...(currentData.health || {}), ...sources }, leaderboard_history: currentData.leaderboard_history }} />}
          {view === 'ranking' && <Ranking data={currentData} history={currentData.leaderboard_history || {}} />}
          {view === 'operations' && <Operations data={currentData} />}
          {view === 'duels' && <Duels data={currentData} />}
          {view === 'market' && <Market data={currentData} />}
          {view === 'trading' && <Trading data={currentData} />}
          {view === 'collection' && <Collection data={currentData} />}
          {view === 'rivals' && <Rivals data={currentData} />}
          {view === 'signals' && <Signals data={currentData} />}
          {view === 'agents' && <Agents data={currentData} />}
        </>}
        <footer className="page-footer"><span>Team 15 · Market operations</span><span>Read only · no trades are submitted here</span></footer>
      </div>
    </main>
  </div>
}
