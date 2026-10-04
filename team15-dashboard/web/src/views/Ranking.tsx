import type { EChartsOption } from 'echarts'
import type { JsonRecord } from '../lib/types'
import { num, points, tickDateTime } from '../lib/format'
import { ChartPanel, MetricTable } from '../components/Chart'
import { Empty, Panel, Stat } from '../components/Panel'

function option(history: JsonRecord, field: 'scores' | 'positions', team15Position?: number): EChartsOption {
  const source = history[field] || {}
  const teams: string[] = history.teams || Object.keys(source)
  const palette = ['#a9bab0', '#789487', '#c0cbc4', '#557968', '#91a599', '#6e8b7c', '#b4c1b8', '#839b8d']
  return { tooltip: { trigger: 'axis' }, legend: { type: 'scroll', bottom: 0, textStyle: { fontSize: 10, color: '#697772' } },
    grid: { left: 48, right: 18, top: 18, bottom: 68 },
    xAxis: { type: 'value', name: 'tick', axisLabel: { color: '#7b8580' }, splitLine: { show: false } },
    yAxis: { type: 'value', inverse: field === 'positions', min: field === 'positions' ? 1 : undefined, max: field === 'positions' ? teams.length : undefined,
      name: field === 'positions' ? 'puesto' : 'puntos', splitLine: { lineStyle: { color: '#e7e9e3' } }, axisLabel: { color: '#7b8580' } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 13, bottom: 35, borderColor: 'transparent', backgroundColor: '#e9ece6', fillerColor: 'rgba(28,104,92,.15)' }],
    series: teams.map((team, i) => ({ name: team.toUpperCase(), type: 'line', showSymbol: false, data: source[team] || [],
      lineStyle: { width: team === 't15' ? 3.6 : 1.1, opacity: team === 't15' ? 1 : .48, color: team === 't15' ? '#123f30' : palette[i % palette.length] },
      itemStyle: { color: team === 't15' ? '#123f30' : palette[i % palette.length] }, emphasis: { focus: 'series' } })),
    ...(typeof team15Position === 'number' && field === 'positions' ? { graphic: [] } : {}),
  }
}

export function Ranking({ data, history }: { data: JsonRecord; history: JsonRecord }) {
  const ranking = data.ranking || {}
  const order = ranking.leaderboard_order || []
  const comps = data.scoring?.components || {}
  const peer = order[(ranking.position || 1) - 2]
  const our = order.find((t: JsonRecord) => t.team === 't15')
  return <div className="view-stack">
    <div className="page-intro"><div><p className="eyebrow">Puntuación pública y desglose privado</p><h1>Carrera del leaderboard</h1><p>Brechas, componentes e histórico de los 18 equipos.</p></div><span className="tick-stamp">{ranking.source === 'history' ? `Captura tick ${ranking.observed_tick}` : `Tick ${data.snapshot?.tick ?? '—'}`} · {tickDateTime(data.snapshot?.tick, data.snapshot?.tick_time_context).label}</span></div>
    {ranking.source === 'history' && <div className="data-quality-banner" role="status"><span className="quality-indicator" />Ranking en vivo no disponible; tabla y brechas muestran la última captura completa del tick {ranking.observed_tick}.</div>}
    <div className="stat-grid"><Stat label="Puesto Team 15" value={`${ranking.position ?? '—'} / ${ranking.teams_count ?? '—'}`} note={`Score público ${points(ranking.score)}`} />
      <Stat label="Brecha al puesto de arriba" value={`${num(ranking.gap_to_next_position ?? (peer && our ? peer.score - our.score : null))} P`} tone="warn" note={peer ? `${peer.team.toUpperCase()} · ${points(peer.score)}` : '—'} />
      <Stat label="Brecha al líder" value={`${num(ranking.gap_to_leader)} P`} note={order[0] ? `${order[0].team.toUpperCase()} · ${points(order[0].score)}` : '—'} />
      <Stat label="Eficiencia de negociación" value={num(data.peers?.ours?.negotiating_per_deal, 3)} note={`P/ trato · puesto ${data.peers?.our_efficiency_position ?? '—'} en eficiencia`} /></div>
    <div className="chart-grid"><ChartPanel title="Score de todos los equipos" detail="Las líneas no observadas en un tick no se interpolan." option={option(history, 'scores')} empty={!history.scores || !Object.keys(history.scores).length} />
      <ChartPanel title="Puesto a lo largo del tiempo" detail="Arriba es mejor. El gráfico usa el campo público completo." option={option(history, 'positions')} empty={!history.positions || !Object.keys(history.positions).length} /></div>
    <div className="overview-grid"><Panel title="Score por componentes" detail="Correlación del conjunto ≠ efecto causal de una operación.">
      {Object.entries(comps).map(([key, comp]: [string, any]) => <div className="rank-component" key={key}><header><b>{key === 'negotiating' ? 'Negociación' : 'Mercado'}</b><strong>{points(comp.ours)} <small>/30</small></strong></header>
        <div className="component-track"><i style={{ width: `${Math.min(100, Math.max(0, (comp.ours || 0) / 30 * 100))}%` }} /></div>
        <small>Mejor: {comp.best_team?.toUpperCase() || '—'} ({points(comp.best)}) · mediana {points(comp.median)} · {comp.teams_above ?? '—'} equipos por encima</small></div>)}
      <p className="data-note">El servidor no publica una conversión fiable de cada trato a puntos futuros. Los escenarios son rangos y deben conservar su incertidumbre.</p>
    </Panel><Panel title="Factores asociados al score" detail="Correlaciones observadas entre equipos en esta partida.">
      {data.peers?.drivers?.length ? <MetricTable columns={['Variable', 'Correlación']} rows={data.peers.drivers.map((d: JsonRecord) => [d.variable, num(d.r_with_score, 3)])} /> : <Empty>Sin muestra de rivales.</Empty>}
    </Panel></div>
    <Panel title="Tabla completa" detail="Snapshot más reciente; el ranking histórico está arriba.">
      <MetricTable columns={['Puesto', 'Equipo', 'Score']} rows={order.map((r: JsonRecord, i: number) => [i + 1, <b className={r.team === 't15' ? 'our-team' : ''}>{String(r.team).toUpperCase()}</b>, num(r.score)])} />
    </Panel>
  </div>
}
