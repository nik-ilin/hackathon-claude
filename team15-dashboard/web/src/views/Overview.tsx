import { Gauge } from 'lucide-react'
import type { EChartsOption } from 'echarts'
import type { JsonRecord } from '../lib/types'
import { num, points, tickDateTime } from '../lib/format'
import { ChartPanel, MetricTable } from '../components/Chart'
import { MyListings } from '../components/MyListings'
import { Empty, Panel, SourceTag, Stat } from '../components/Panel'

const palette = ['#9aada2', '#789487', '#b2c0b7', '#547968', '#81978a', '#c0cbc4', '#658574', '#a3b4aa']

function raceOption(history: JsonRecord): EChartsOption {
  const scores = history.scores || {}
  const teams: string[] = history.teams || Object.keys(scores)
  return {
    color: palette,
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' } },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: '#687772', fontSize: 10 }, pageIconColor: '#1c685c' },
    grid: { left: 42, right: 18, top: 18, bottom: 66 },
    xAxis: { type: 'value', name: 'tick', splitLine: { show: false }, axisLabel: { color: '#76837e' }, axisLine: { lineStyle: { color: '#d9ded7' } } },
    yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#e8ebe5' } }, axisLabel: { color: '#76837e' } },
    dataZoom: [{ type: 'inside', filterMode: 'none' }, { type: 'slider', height: 14, bottom: 35, borderColor: 'transparent', backgroundColor: '#ebeee8', fillerColor: 'rgba(28,104,92,.14)' }],
    series: teams.map((team, i) => ({
      name: team === 't15' ? 'Team 15' : team.toUpperCase(), type: 'line', showSymbol: false,
      data: (scores[team] || []).map((p: [number, number]) => [p[0], p[1]]),
      lineStyle: { width: team === 't15' ? 3.5 : 1.1, opacity: team === 't15' ? 1 : .48,
        color: team === 't15' ? '#123f30' : palette[i % palette.length] },
      itemStyle: { color: team === 't15' ? '#123f30' : palette[i % palette.length] },
      emphasis: { focus: 'series' },
    })),
  }
}

export function Overview({ data }: { data: JsonRecord }) {
  const ranking = data.ranking || {}
  const score = data.scoring || {}
  const capital = data.capital || {}
  const health = data.health || {}
  const order: JsonRecord[] = ranking.leaderboard_order || []
  const aboveIndex = Math.max(0, (ranking.position || 1) - 2)
  const above = ranking.position > 1 ? order[aboveIndex] : undefined
  const peerGap = above && typeof ranking.score === 'number' ? above.score - ranking.score : ranking.gap_to_next_position
  const actions: JsonRecord[] = data.top_actions || []
  const components = score.components || {}
  const race = (data.leaderboard_history || {}) as JsonRecord

  return <div className="view-stack">
    <div className="page-intro"><div><p className="eyebrow">Team 15 / centro de mando</p><h1>Estado de la partida</h1>
      <p>Score, movimiento del ranking y siguiente oportunidad en una sola lectura.</p></div>
      <div className="intro-tools"><span className="tick-stamp">Tick {ranking.tick ?? data.snapshot?.tick ?? '—'} · {tickDateTime(data.snapshot?.tick, data.snapshot?.tick_time_context).label}</span>
        <span className="updated-at"><Gauge size={14} />{data.snapshot?.captured_at ? new Date(data.snapshot.captured_at * 1000).toLocaleTimeString('es-ES') : 'Sin hora'}</span></div>
    </div>
    <section className="score-ribbon" aria-label="Posición de Team 15">
      <div className="score-main"><span>Posición actual</span><div><strong>{ranking.position ?? '—'}</strong><small>de {ranking.teams_count ?? order.length ?? '—'}</small></div>
        <p>{peerGap == null ? 'Sin rival superior medido' : `${num(peerGap)} puntos para adelantar al equipo de arriba`}</p></div>
      <div className="score-block"><span>{ranking.source === 'history' ? `Score público · tick ${ranking.observed_tick}` : 'Score de servidor'}</span><strong>{points(ranking.score ?? score.score)}</strong><small>{ranking.source === 'history' && ranking.private_score != null ? `Score privado actual: ${points(ranking.private_score)}` : 'La cifra pública puede ir por detrás del valor privado.'}</small></div>
      <div className="score-block"><span>Negociación</span><strong>{points(components.negotiating?.ours)}</strong><small>Máximo de componente: 30 P</small></div>
      <div className="score-block"><span>Mercado</span><strong>{points(components.market?.ours)}</strong><small>Prueba + valor creado entre equipos</small></div>
      <div className="score-motif" aria-hidden="true"><i /><i /><i /><i /><i /><i /><i /></div>
    </section>
    {ranking.source === 'history' && <div className="data-quality-banner" role="status"><span className="quality-indicator" />El leaderboard en vivo no llegó en tick {data.snapshot?.tick ?? '—'}. Se muestra la última captura completa (tick {ranking.observed_tick}); puesto y brechas corresponden a esa captura.</div>}
    <div className="stat-grid">
      <Stat label="Brecha al siguiente puesto" value={<>{num(peerGap)} <small>P</small></>} note={above?.team ? `Objetivo inmediato: ${above.team.toUpperCase()}` : 'Aún no hay puesto por encima'} tone="warn" />
      <Stat label="Caja utilizable" value={<>{num(capital.spendable_after_reserve)} <small>P</small></>} note={`${num(capital.open_bid_commitments)} P comprometidas · reserva ${num(capital.operating_reserve)} P`} />
      <Stat label="Tratos cerrados" value={num(score.deals)} note="El número de tratos por sí solo no suma puntos." />
      <Stat label="Feed" value={health.public_feed?.status === 'fresh' ? 'Al día' : 'Revisar'} note={`Tick ${health.public_feed?.last_tick ?? '—'} · ${health.public_feed?.ticks_behind ?? '—'} ticks de desfase`} tone={health.public_feed?.status === 'fresh' ? 'good' : 'bad'} />
    </div>
    <div className="overview-grid">
      <ChartPanel title="Carrera completa de score" detail="Todos los equipos · arrastra el rango inferior para acotar el periodo." option={raceOption(race)} empty={!Object.keys(race.scores || {}).length} />
      <Panel title="Componentes y fuentes" detail="Puntuación actual contra escala de 30 puntos.">
        <div className="component-list">{[['Negociación', components.negotiating], ['Mercado', components.market]].map(([label, item]: any) => <div className="component-row" key={label}>
          <div><span>{label}</span><b>{points(item?.ours)} <small>/ {num(item?.weight_in_total || 30)} P</small></b></div>
          <div className="component-track"><i style={{ width: `${Math.min(100, Math.max(0, ((item?.ours || 0) / 30) * 100))}%` }} /></div>
          <small>Mejor: {item?.best_team?.toUpperCase() || '—'} · {points(item?.best)}</small>
        </div>)}</div>
        <div className="source-list">{Object.entries(health).map(([key, source]: [string, any]) => <SourceTag key={key} label={key.replaceAll('_', ' ')} status={source?.status} />)}</div>
      </Panel>
    </div>
    <div className="overview-grid overview-bottom">
      <Panel title="Siguiente acción para revisar" detail="Ordenada por el motor actual; excedente no equivale a puntos garantizados.">
        {actions.length ? <div className="action-list">{actions.slice(0, 5).map((action, index) => <article className="action-row" key={`${action.team}-${action.action}-${index}`}>
          <span className="action-number">{String(index + 1).padStart(2, '0')}</span><div><b>{action.action} · {String(action.team || '').toUpperCase()}</b>
            <p>{(action.give || []).join(', ') || '—'} <span>por</span> {(action.receive || []).join(', ') || `${num(action.price)} P`}</p>
            <small>{action.confidence || 'Señal observada'} · {action.why || 'Revisar oferta y valor privado antes de actuar.'}</small></div>
          <strong>{action.surplus == null ? '—' : `+${points(action.surplus)}`}</strong></article>)}</div>
          : <Empty>No hay propuestas con margen verificable en este tick.</Empty>}
      </Panel>
      <Panel title="Carrera inmediata" detail="Público; se actualiza con la última tabla del servidor.">
        <MetricTable columns={['Puesto', 'Equipo', 'Score', 'Distancia']} rows={order.slice(Math.max(0, (ranking.position || 1) - 3), (ranking.position || 1) + 2).map((row, i) => {
          const globalIndex = Math.max(0, (ranking.position || 1) - 3) + i
          const next = order[globalIndex - 1]
          return [String(globalIndex + 1), <b className={row.team === 't15' ? 'our-team' : ''}>{String(row.team).toUpperCase()}</b>, num(row.score), next ? num(next.score - row.score) : '—']
        })} />
      </Panel>
    </div>
    <MyListings offers={data.my_offers || []} venue={data.own_venue || {}} tick={data.snapshot?.tick}
      shareMessage={data.group_share_message} />
  </div>
}
