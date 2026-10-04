import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Operations } from './OperationalViews'

describe('Operations', () => {
  it('shows the API duel counters and keeps inbound offers separate from our cash commitments', () => {
    render(<Operations data={{
      snapshot: { tick: 42 },
      operations: { duels: { live: 2, urgent: 1 }, capital: { spendable_after_reserve: 30, open_bid_commitments: 5, operating_reserve: 10 }, queue: [] },
      strategy_health: { execution: { status: 'running', mode: 'duels' } },
      my_offers: [{ id: 'mine', venue: 'v15', wants: ['LAT-01'], cash_give: 5, ticks_left: 4 }],
      incoming_offers: [{ id: 'inbound', venue: 'v19', maker: 't03', gives: ['LAT-02'], cash_give: 120, ticks_left: 2 }],
      commands: [],
    }} />)

    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('1 con tiempo crítico')).toBeInTheDocument()
    expect(screen.getByText('Entrante')).toBeInTheDocument()
    expect(screen.getByText('Tu oferta')).toBeInTheDocument()
    expect(screen.getByText(/Una oferta entrante no compromete tu caja/)).toBeInTheDocument()
  })
})
