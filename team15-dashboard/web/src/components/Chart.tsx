import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import type { ReactNode } from 'react'
import { Panel } from './Panel'

export function ChartPanel({ title, detail, option, empty, className = '' }: {
  title: string; detail?: string; option: EChartsOption; empty?: boolean; className?: string
}) {
  return <Panel title={title} detail={detail} className={`chart-panel ${className}`}>
    {empty ? <div className="chart-empty">Aún no hay muestras suficientes. La gráfica aparecerá al guardar nuevas capturas.</div>
      : <ReactECharts option={option} notMerge lazyUpdate style={{ height: 300, width: '100%' }} opts={{ renderer: 'canvas' }} />}
  </Panel>
}

export function MetricTable({ title, columns, rows, empty = 'No hay datos para este periodo' }: {
  title?: ReactNode; columns: string[]; rows: ReactNode[][]; empty?: string
}) {
  if (!rows.length) return <div className="table-empty">{empty}</div>
  return <div className="table-scroll">{title && <h3>{title}</h3>}<table className="data-table"><thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>
}

export const compactTooltip = {
  backgroundColor: '#123b2d', borderColor: '#123b2d', textStyle: { color: '#fff' },
  extraCssText: 'border-radius:8px;box-shadow:0 12px 32px rgba(12,24,21,.16)',
}
