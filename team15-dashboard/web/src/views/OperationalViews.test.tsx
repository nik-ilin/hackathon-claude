import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Operations } from './OperationalViews'

describe('Operations', () => {
  it('shows the API duel counters and keeps inbound offers separate from our cash commitments', () => {
    render(<Operations data={{
      snapshot: { tick: 42, tick_time_context: { tick: 42, captured_at: 1800000000, samples: [] } },
      operations: { duels: { live: 2, urgent: 1, safe: 1 }, capital: { spendable_after_reserve: 30, open_bid_commitments: 5, operating_reserve: 10 }, queue: [{ duel: 4, state: 'close_now', deadline_tick: 44, ticks_left: 2, total_margin: 12, vanish_risk: 1, expected_capture_at_risk: 12, action: 'Cerrar por cola', deadline_time: { label: '04/10 12:30 aprox.' } }] },
      strategy_health: { execution: { status: 'running', mode: 'duels' } },
      my_offers: [{ id: 'mine', venue: 'v15', wants: ['LAT-01'], cash_give: 5, ticks_left: 4, expires_tick: 46 }],
      incoming_offers: [{ id: 'inbound', venue: 'v19', maker: 't03', gives: ['LAT-02'], cash_give: 120, ticks_left: 2 }],
      commands: [],
    }} />)

    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText(/1 con tiempo crítico/)).toBeInTheDocument()
    expect(screen.getByText('Entrante')).toBeInTheDocument()
    expect(screen.getByText('Tu oferta')).toBeInTheDocument()
    expect(screen.getByText(/Una oferta entrante no compromete tu caja/)).toBeInTheDocument()
    expect(screen.getByText('Prioridad de cierres para el score')).toBeInTheDocument()
    expect(screen.getByText('04/10 12:30 aprox.')).toBeInTheDocument()
    expect(screen.getByText(/12 P · excedente, no leaderboard/)).toBeInTheDocument()
  })
})
