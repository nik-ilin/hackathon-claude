import { useMemo, useState } from 'react'
import type { EChartsOption } from 'echarts'
import { Radio, Search, ShieldCheck, Store } from 'lucide-react'
import type { JsonRecord } from '../lib/types'
import { list, num, points } from '../lib/format'
import { ChartPanel, MetricTable } from '../components/Chart'
import { Empty, Panel, Stat } from '../components/Panel'

const asRows = (value: unknown): JsonRecord[] => Array.isArray(value) ? value.filter((row) => row && typeof row === 'object') : []
const marketValue = (card: JsonRecord) => card.sold_median == null ? '—' : `${num(card.sold_median)} P · n=${num(card.sold_count, 0)}`
const sortByRef = (rows: JsonRecord[]) => [...rows].sort((a, b) => String(a.ref || '').localeCompare(String(b.ref || '')))

function SearchBox({ value, onChange, placeholder = 'Filtrar resultados…' }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return <label className="table-search"><Search size={14} /><input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} /></label>
}

function filterRows(rows: JsonRecord[], query: string) {
  const q = query.trim().toLowerCase()
  return q ? rows.filter((row) => JSON.stringify(row).toLowerCase().includes(q)) : rows
}

export function Operations({ data }: { data: JsonRecord }) {
  const ops = data.operations || {}, duels = ops.duels || {}, capital = ops.capital || {}, queue = asRows(ops.queue)
  const myOffers = asRows(data.my_offers), incoming = asRows(data.incoming_offers)
  const runtime = data.strategy_health?.execution || {}
  const commands = asRows(data.commands)
  const [copied, setCopied] = useState('')
  async function copy(command: JsonRecord) {
    try { await navigator.clipboard.writeText(command.command); setCopied(command.id) }
    catch { setCopied('error') }
    window.setTimeout(() => setCopied(''), 1800)
  }
  return <div className="view-stack">
    <div className="page-intro"><div><p className="eyebrow">Mesa operativa · solo lectura</p><h1>Operaciones</h1><p>Estado de procesos, caja comprometida y acciones que requieren revisión humana.</p></div><span className="tick-stamp">Tick {data.snapshot?.tick ?? '—'}</span></div>
    <div className="stat-grid"><Stat label="Duelos vivos" value={num(duels.live)} note={`${num(duels.urgent)} con tiempo crítico`} tone={Number(duels.live) > 0 ? 'warn' : 'neutral'} />
      <Stat label="Caja utilizable" value={`${num(capital.spendable_after_reserve)} P`} note={`${num(capital.open_bid_commitments)} P comprometidas`} />
      <Stat label="Reserva operativa" value={`${num(capital.operating_reserve)} P`} note="No usar como presupuesto libre" />
      <Stat label="Agente de ejecución" value={runtime.status === 'running' ? 'Activo' : 'No confirmado'} note={runtime.mode || 'Estado reportado por el runtime'} tone={runtime.status === 'running' ? 'good' : 'neutral'} /></div>
    <Panel title="Ofertas que requieren seguimiento" detail="Propias y entrantes separadas. Una oferta entrante no compromete tu caja hasta aceptarla.">
      {(myOffers.length || incoming.length) ? <MetricTable columns={['Dirección', 'Mercado', 'Equipo', 'Propuesta', 'Vence', 'Referencia']} rows={[
        ...myOffers.map((o) => ['Tu oferta', o.venue || '—', o.to || o.maker || 'Pública', offerSides(o), o.ticks_left == null ? '—' : `${num(o.ticks_left, 0)} ticks`, o.id || '—']),
        ...incoming.map((o) => ['Entrante', o.venue || '—', o.maker || o.from || '—', offerSides(o), o.ticks_left == null ? '—' : `${num(o.ticks_left, 0)} ticks`, o.id || '—']),
      ]} /> : <Empty>No hay ofertas propias ni entrantes activas en esta captura.</Empty>}
    </Panel>
    <div className="overview-grid"><Panel title="Cola de decisiones" detail="Señales calculadas a partir de las fuentes activas; revisa la evidencia antes de operar.">
      {queue.length ? <MetricTable columns={['Prioridad', 'Acción', 'Evidencia', 'Fuente']} rows={queue.map((row) => [row.priority || '—', <b>{row.action || 'Revisar'}</b>, row.evidence || '—', row.source || '—'])} /> : <Empty>No hay alertas operativas prioritarias en esta captura.</Empty>}
    </Panel><Panel title="Comandos copiables" detail="El navegador solo copia; los comandos se ejecutan desde tu terminal de operación.">
      <div className="command-list">{commands.map((command) => <article key={command.id}><div><b>{command.label}</b><small>{command.note}</small><code>{command.command}</code></div><button type="button" className="button-secondary" onClick={() => void copy(command)}>{copied === command.id ? 'Copiado' : 'Copiar'}</button></article>)}</div>
    </Panel></div>
  </div>
}

function duelTrend(rows: JsonRecord[]): EChartsOption {
  const data = [...rows].reverse().map((row, index) => [index + 1, Number(row.result ?? row.net ?? row.delta ?? row.surplus ?? 0)])
  return { tooltip: { trigger: 'axis' }, grid: { left: 52, right: 22, top: 24, bottom: 34 }, xAxis: { type: 'value', name: 'Duelo', axisLabel: { color: '#66756f' }, splitLine: { show: false } }, yAxis: { type: 'value', name: 'Δ valor', axisLabel: { color: '#66756f' }, splitLine: { lineStyle: { color: '#e8ece7' } } }, series: [{ type: 'bar', data, itemStyle: { color: (p: any) => p.value[1] >= 0 ? '#256346' : '#81998b', borderRadius: [5, 5, 0, 0] } }] }
}

export function Duels({ data }: { data: JsonRecord }) {
  const rows = asRows(data.rows), analysis = data.analysis || {}, health = data.strategy_health || {}
  const scoredRows = rows.filter((r) => r.kind !== 'practice')
  const positive = scoredRows.filter((r) => Number(r.result ?? r.net ?? r.delta ?? r.surplus ?? 0) > 0).length
  const net = scoredRows.reduce((sum, row) => sum + Number(row.result ?? row.net ?? row.delta ?? row.surplus ?? 0), 0)
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Negociación · histórico por cierre</p><h1>Duelos</h1><p>Margen, cierres rentables y revisiones de aprendizaje con el dato original a mano.</p></div><span className="tick-stamp">{rows.length} registros</span></div>
    <div className="stat-grid"><Stat label="Cierres históricos" value={analysis.scored ?? scoredRows.length} note={`${num(analysis.no_deal)} duelos sin acuerdo`} /><Stat label="Rentables" value={positive} note={scoredRows.length ? `${num(positive / scoredRows.length * 100, 1)} % de la muestra puntuable` : 'Sin muestra'} tone="good" /><Stat label="Δ neta observada" value={`${points(net)}`} note="Suma de resultados; no equivale a puntos seguros" tone={net < 0 ? 'bad' : 'neutral'} /><Stat label="Estado de aprendizaje" value={health.duel_learning?.status || health.duels?.source || 'Sin dato'} note={health.duel_learning?.note || 'Consultar salud de estrategia'} /></div>
    <ChartPanel title="Resultado por duelo" detail="Cada barra conserva el signo del cambio de valor registrado." option={duelTrend(rows)} empty={!rows.length} />
    <Panel title="Detalle por duelo" detail="Incluye práctica; el resumen puntuable la excluye."><MetricTable columns={['Duelo', 'Sesión', 'Rival', 'Artículo', 'Rol', 'Resultado', 'Precio', 'Días', 'Δ valor']} rows={rows.map((r) => [r.duel ?? '—', r.session ?? '—', r.rival || '—', r.item || '—', r.role || '—', r.kind || r.status || '—', r.price == null ? '—' : `${num(r.price)} P`, r.days ?? '—', points(r.result)])} empty="El API no ha devuelto duelos cerrados." /></Panel>
  </div>
}

function offerSides(row: JsonRecord) { return `${list(row.gives)}${row.cash_give ? ` + ${num(row.cash_give)} P` : ''} → ${list(row.wants)}${row.cash_want ? ` + ${num(row.cash_want)} P` : ''}` }
export function Market({ data }: { data: JsonRecord }) {
  const activity = data.activity || {}, all = asRows(activity.all_offers), own = asRows(activity.my_open_offers), summary = activity.summary || {}, venue = data.venue || {}
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => filterRows(all, query), [all, query])
  const venues = new Map<string, JsonRecord[]>(); for (const offer of filtered) venues.set(offer.venue || 'unknown', [...(venues.get(offer.venue || 'unknown') || []), offer])
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Libro público · publicaciones privadas identificadas</p><h1>Mercado</h1><p>Qué publicas tú, qué circula en tu venue y cómo se distribuyen las ofertas entre mercados.</p></div><span className="tick-stamp">{activity.own_venue || venue.venue || '—'}</span></div>
    <div className="stat-grid"><Stat label="Tus ofertas abiertas" value={summary.my_open_offers ?? own.length} note={`${summary.active_sales ?? 0} ventas`} tone={own.length ? 'good' : 'neutral'} /><Stat label="Ofertas en tu mercado" value={summary.own_market_total ?? 0} note={`${summary.own_market_by_others ?? 0} de otros equipos`} /><Stat label="Libros activos" value={summary.active_venues ?? 0} note={`${summary.open_offers_total ?? all.length} ofertas abiertas`} /><Stat label="Comisión de tu venue" value={venue.fee_bps == null ? '—' : `${num(venue.fee_bps / 100)} %`} note={activity.own_venue_name || 'Fuente de venue'} /></div>
    <Panel title="Tus publicaciones activas" detail="Separadas del libro general; cada fila indica el mercado de publicación.">
      {own.length ? <MetricTable columns={['Mercado', 'Tipo', 'Das ↔ pides', 'Comisión', 'Caduca']} rows={own.map((o) => [`${o.venue_name || o.venue} (${o.venue})`, o.kind, offerSides(o), o.fee_bps == null ? '—' : `${num(o.fee_bps / 100)} % + ${num(o.fee_per_card)} P/carta`, o.ticks_left == null ? '—' : `${num(o.ticks_left, 0)} ticks`])} /> : <Empty>No hay publicaciones tuyas activas en el último snapshot.</Empty>}
    </Panel>
    <Panel title="Libro de ofertas" detail="Listado público, agrupado por mercado y filtrable por carta, equipo o precio." action={<SearchBox value={query} onChange={setQuery} placeholder="Buscar carta o mercado" />}>
      {[...venues.entries()].map(([id, rows]) => <section className="venue-book" key={id}><header><Store size={15} /><b>{rows[0]?.venue_name || id}</b><span>{id} · {rows.length} ofertas</span></header><MetricTable columns={['Autor', 'Tipo', 'Ofrece → pide', 'Destino', 'Caduca']} rows={rows.map((o) => [o.mine ? 'Team 15 · tuya' : o.maker || 'Otro equipo', o.kind, offerSides(o), o.to || 'Pública', o.ticks_left == null ? '—' : `${num(o.ticks_left, 0)} ticks`])} /></section>)}
      {!filtered.length && <Empty>No hay ofertas que coincidan con el filtro.</Empty>}
    </Panel>
  </div>
}

function tradeChart(rows: JsonRecord[]): EChartsOption {
  return { tooltip: { trigger: 'axis' }, grid: { left: 52, right: 18, top: 24, bottom: 35 }, xAxis: { type: 'category', data: rows.map((_, i) => i + 1), axisLabel: { color: '#738078' }, splitLine: { show: false } }, yAxis: { type: 'value', name: 'Δ colección + efectivo', axisLabel: { color: '#738078' }, splitLine: { lineStyle: { color: '#e8ece7' } } }, series: [{ type: 'line', smooth: .22, symbolSize: 6, data: rows.map((r) => r.net ?? r.delta ?? null), lineStyle: { color: '#26745f', width: 2.5 }, itemStyle: { color: '#26745f' }, areaStyle: { color: 'rgba(38,116,95,.12)' } }] }
}

export function Trading({ data }: { data: JsonRecord }) {
  const rows = asRows(data.history), proposals = asRows(data.proposals), summary = data.summary || {}, liquidation = asRows(data.liquidation_targets), ladder = data.ladder || {}
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Libro de liquidaciones · calidad del precio</p><h1>Compras y ventas</h1><p>Separa tratos cerrados de propuestas pendientes y compara resultado por operación.</p></div></div>
    <div className="stat-grid"><Stat label="Operaciones cerradas" value={rows.length} note={`${num(summary.positive)} positivas · ${num(summary.negative)} negativas`} /><Stat label="Resultado neto registrado" value={points(summary.observed_net)} tone={Number(summary.observed_net) < 0 ? 'bad' : 'good'} note="Solo tratos medidos entre muestras privadas" /><Stat label="Escalera de dealers" value={`${num(ladder.points)} P`} note={`${num(ladder.filled_slots)} casillas ocupadas`} /><Stat label="Propuestas revisables" value={proposals.length} note="No son ventas confirmadas" /></div>
    <ChartPanel title="Resultado acumulado por operación" detail="Orden temporal de las operaciones disponibles; huecos en la fuente no se interpolan." option={tradeChart(rows)} empty={!rows.length} />
    <div className="overview-grid"><Panel title="Ventas potenciales" detail="Candidatos calculados desde duplicados, precio mínimo y demanda observada."><MetricTable columns={['Carta', 'Mercado/rival', 'Precio', 'Δ valor estimado', 'Confianza']} rows={proposals.slice(0, 40).map((r) => [r.ref || list(r.give), r.team || r.venue || '—', r.price == null ? '—' : `${num(r.price)} P`, points(r.net_or_signal ?? r.surplus), r.confidence || 'Señal'])} /></Panel>
      <Panel title="Stock para liquidar" detail="Solo cartas verificadas; vender puede romper el bonus de página."><MetricTable columns={['Carta', 'Mínimo', 'Pérdida de colección', 'Rompe página', 'Demanda']} rows={liquidation.map((r) => [r.ref, `${num(r.floor)} P`, `${num(r.loss)} P`, r.breaks_page ? 'Sí' : 'No', list(r.demanders)])} /></Panel></div>
    <Panel title="Historial de trades" detail="Efectivo y cambio de valoración de colección por trato."><MetricTable columns={['Tick', 'Tipo', 'Rival', 'Carta / lote', 'Precio', 'Resultado neto', 'Evaluación']} rows={rows.map((r) => [r.tick ?? '—', r.kind || '—', r.team || r.counterparty || '—', list(r.cards || r.refs || r.give || r.receive), r.price == null ? '—' : `${num(r.price)} P`, points(r.net ?? r.delta ?? r.surplus), r.quality || r.classification || '—'])} /></Panel>
  </div>
}

export function Collection({ data }: { data: JsonRecord }) {
  const rows = sortByRef(asRows(data.catalog)), [query, setQuery] = useState(''), [selectedSet, setSelectedSet] = useState('all'), [status, setStatus] = useState('all')
  const sets = [...new Set(rows.map((r) => r.set).filter(Boolean))]
  const filtered = rows.filter((r) => (!query || JSON.stringify(r).toLowerCase().includes(query.toLowerCase())) && (selectedSet === 'all' || r.set === selectedSet) && (status === 'all' || (status === 'owned' ? r.stock > 0 : status === 'free' ? r.free > 0 : status === 'missing' ? !r.stock : r.decision === status)))
  const owned = rows.filter((r) => Number(r.stock) > 0).length, missing = rows.filter((r) => Number(r.stock) <= 0 && r.released).length, dupes = rows.filter((r) => Number(r.free) > 0).length
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Inventario · valor marginal · páginas</p><h1>Colección</h1><p>Disponibilidad confirmada, duplicados libres, cartas faltantes y riesgo de vender.</p></div><span className="tick-stamp">{rows.length} referencias</span></div>
    <div className="stat-grid"><Stat label="Referencias propias" value={`${owned} / ${rows.filter((r) => r.released).length}`} note="Solo cartas publicadas" /><Stat label="Faltantes" value={missing} note="Señales de posesión rival no garantizan intercambio" tone="warn" /><Stat label="Duplicados libres" value={dupes} note="Stock vendible confirmado" tone={dupes ? 'good' : 'neutral'} /><Stat label="Packs sin abrir" value={asRows(data.packs).length} note="El contenido aleatorio no suma por sí solo" /></div>
    <Panel title="Cobertura por colección" detail="Cada celda corresponde a una referencia del catálogo."><div className="collection-coverage">{sets.map((set) => { const group = rows.filter((r) => r.set === set && r.released); const have = group.filter((r) => r.stock > 0).length; return <button key={set} type="button" onClick={() => setSelectedSet(selectedSet === set ? 'all' : set)} className={selectedSet === set ? 'coverage-item selected' : 'coverage-item'}><span><b>{set}</b><small>{have}/{group.length} · {group.length ? num(have / group.length * 100, 0) : 0}%</small></span><i><em style={{ width: `${group.length ? have / group.length * 100 : 0}%` }} /></i><span className="coverage-cells">{group.map((r) => <i key={r.ref} title={`${r.ref}: ${r.stock > 0 ? 'propia' : 'falta'}`} className={r.free > 0 ? 'duplicate' : r.stock > 0 ? 'owned' : 'missing'} />)}</span></button> })}</div></Panel>
    <Panel title="Catálogo completo" detail="Filtros por colección, posesión y búsqueda. Los valores observados y privados se mantienen separados." action={<SearchBox value={query} onChange={setQuery} placeholder="Carta, rareza o equipo" />}>
      <div className="filter-row"><label>Colección<select value={selectedSet} onChange={(e) => setSelectedSet(e.target.value)}><option value="all">Todas</option>{sets.map((s) => <option key={s}>{s}</option>)}</select></label><label>Situación<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="all">Todas</option><option value="owned">Propias</option><option value="free">Duplicado libre</option><option value="missing">Faltante</option><option value="sell_candidate">Venta sugerida</option><option value="buy_candidate">Compra sugerida</option></select></label><span>{filtered.length} resultados</span></div>
      <MetricTable columns={['Carta', 'Colección', 'Rareza', 'Stock', 'Libre', 'Mín. venta', 'Máx. compra', 'Venta mediana', 'Interés rival', 'Riesgo página']} rows={filtered.map((r) => [<b>{r.ref}</b>, r.set || '—', r.rarity || '—', r.stock ?? '—', r.free ?? '—', r.sell_floor == null ? '—' : `${num(r.sell_floor)} P`, r.buy_ceiling == null ? '—' : `${num(r.buy_ceiling)} P`, marketValue(r), `${num(r.demand_count_observed, 0)} equipos`, r.sell_breaks_page ? 'Rompe' : 'No'])} /></Panel>
  </div>
}

export function Rivals({ data }: { data: JsonRecord }) {
  const teams = asRows(data.teams), peers = data.peers || {}, board = asRows(data.leaderboard)
  const demand = new Map<string, number>(), holdings = new Map<string, number>()
  for (const team of teams) { for (const c of team.sought || []) demand.set(c, (demand.get(c) || 0) + 1); for (const c of team.held || []) holdings.set(c, (holdings.get(c) || 0) + 1) }
  const cards = [...new Set([...demand.keys(), ...holdings.keys()])].sort((a, b) => (demand.get(b) || 0) - (demand.get(a) || 0)).slice(0, 80)
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Demanda, posesión y rendimiento</p><h1>Rivales</h1><p>Observaciones públicas del feed. Ausencia de señal no significa ausencia de carta.</p></div><span className="tick-stamp">{teams.length} equipos observados</span></div>
    <div className="stat-grid"><Stat label="Equipos en tabla" value={board.length} note="Último leaderboard disponible" /><Stat label="Equipos con perfil" value={teams.length} note="Perfiles públicos; muestra parcial" /><Stat label="Drivers de score" value={asRows(peers.drivers).length} note="Correlación entre equipos, no causalidad" /><Stat label="Tu eficiencia" value={peers.ours?.negotiating_per_deal == null ? '—' : `${num(peers.ours.negotiating_per_deal, 3)} P`} note={`Puesto ${peers.our_efficiency_position ?? '—'} por trato`} /></div>
    <div className="overview-grid"><Panel title="Leaderboard" detail="Score público más reciente."><MetricTable columns={['Puesto', 'Equipo', 'Score']} rows={board.map((r, i) => [i + 1, <b className={r.team === 't15' ? 'our-team' : ''}>{String(r.team).toUpperCase()}</b>, points(r.score)])} /></Panel>
      <Panel title="Factores relacionados con score" detail="Correlaciones descriptivas sobre los equipos disponibles."><MetricTable columns={['Factor', 'r con score']} rows={asRows(peers.drivers).map((r) => [r.variable || r.name, num(r.r_with_score, 3)])} /></Panel></div>
    <Panel title="Matriz de posesión y demanda" detail="Conteo de equipos observados por carta; no estima probabilidad de venta."><MetricTable columns={['Carta', 'Equipos que la piden', 'Equipos que la tienen', 'Presión observada']} rows={cards.map((ref) => { const held = holdings.get(ref) || 0; const sought = demand.get(ref) || 0; return [ref, sought, held, held ? num(sought / held, 2) : '—'] })} /></Panel>
  </div>
}

export function Signals({ data }: { data: JsonRecord }) {
  const radio = asRows(data.radio), summary = data.summary || {}
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Radio · feed · evidencia</p><h1>Señales</h1><p>Noticias rastreadas con fuente, tiempo y coincidencia con tu colección.</p></div><span className="tick-stamp"><Radio size={14} /> {radio.length} señales</span></div>
    <div className="stat-grid"><Stat label="Señales encontradas" value={radio.length} note="Noticias y eventos del feed" /><Stat label="Relevantes para stock" value={summary.holding_matches ?? '—'} note="Coincidencias señaladas" /><Stat label="Fuentes recientes" value={summary.sources ?? '—'} note="Fuente según el dato disponible" /><Stat label="Impactos medidos" value={asRows(data.impacts).length} note="No implica causalidad" /></div>
    <div className="signal-list">{radio.map((r, i) => <article className="signal-card" key={r.id || i}><div className="signal-icon"><Radio size={16} /></div><div><header><b>{r.headline || r.title || r.type || 'Evento de radio'}</b><span>{r.source || r.channel || 'Fuente no identificada'}</span></header><p>{r.summary || r.text || r.body || 'Evento sin descripción textual.'}</p><footer><span>{r.at_hours ?? r.tick ?? 'Tiempo desconocido'}</span><span>{r.ref || list(r.cards)}</span><span className={`confidence confidence-${String(r.confidence || 'unknown').toLowerCase()}`}>{r.confidence || 'Confianza no calculada'}</span></footer></div></article>)}{!radio.length && <Empty>No hay señales disponibles en este snapshot.</Empty>}</div>
    <Panel title="Efectos registrados" detail="Cambios del histórico próximos a operaciones; correlación temporal no prueba causalidad."><MetricTable columns={['Tick', 'Componente', 'Cambio', 'Evento asociado', 'Evidencia']} rows={asRows(data.impacts).map((r) => [r.tick, r.component || r.field, points(r.change ?? r.delta), r.event || r.action || '—', r.evidence || r.source || '—'])} /></Panel>
  </div>
}

export function Agents({ data }: { data: JsonRecord }) {
  const health = data.health || {}, sources = data.sources || {}, operations = data.operations || {}
  const sourceRows = Object.entries(sources).map(([key, value]: [string, any]) => [key.replaceAll('_', ' '), value.status || 'unknown', value.tick ?? value.last_tick ?? '—', value.ticks_behind ?? '—'])
  const runtime = operations.execution || health.execution || {}
  const memory = health.memory || {}, learning = health.duel_learning || health.learner || {}
  return <div className="view-stack"><div className="page-intro"><div><p className="eyebrow">Agentes · memoria · fuentes</p><h1>Salud de agentes</h1><p>Comprueba procesos, frescura y señales de aprendizaje antes de confiar una operación.</p></div><span className="tick-stamp"><ShieldCheck size={14} /> Solo lectura</span></div>
    <div className="stat-grid"><Stat label="Proceso operativo" value={runtime.status === 'running' ? 'En ejecución' : 'Detenido / no confirmado'} note={runtime.mode || 'Modo no informado'} tone={runtime.status === 'running' ? 'good' : 'warn'} /><Stat label="Memoria" value={memory.status || 'Sin estado'} note={memory.last_tick == null ? 'Sin último tick informado' : `Último tick ${memory.last_tick}`} /><Stat label="Eventos recientes" value={num(memory.events_in_window)} note="Muestra local detectada" /><Stat label="Aprendizaje de duelos" value={learning.status || 'Sin informe'} note={learning.last_update || learning.last_tick || 'Sin fecha de actualización'} /></div>
    <div className="overview-grid"><Panel title="Frescura por fuente" detail="Las fuentes parciales se mantienen señaladas y no se rellenan con datos inventados."><MetricTable columns={['Fuente', 'Estado', 'Tick disponible', 'Desfase']} rows={sourceRows} /></Panel>
      <Panel title="Actividad del agente" detail="La telemetría local informa actividad; no prueba que una política esté aprendiendo correctamente."><MetricTable columns={['Medida', 'Valor']} rows={Object.entries(health).filter(([, value]) => typeof value !== 'object').map(([key, value]) => [key.replaceAll('_', ' '), String(value)])} /></Panel></div>
  </div>
}
