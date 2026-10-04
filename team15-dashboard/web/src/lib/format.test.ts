import { describe, expect, it } from 'vitest'
import { tickDateTime } from './format'

describe('tickDateTime', () => {
  it('uses exact and interpolated historical captures', () => {
    const context = { tick: 20, captured_at: 1800000600, samples: [
      { tick: 10, captured_at: 1800000000 }, { tick: 20, captured_at: 1800000600 },
    ] }
    expect(tickDateTime(20, context).quality).toBe('observed')
    expect(tickDateTime(15, context).quality).toBe('interpolated')
  })
  it('estimates future timestamps from live clock and respects pause', () => {
    expect(tickDateTime(22, { tick: 20, captured_at: 1000, tick_seconds: 15, next_tick_in: 5 }).quality).toBe('estimated')
    expect(tickDateTime(22, { tick: 20, captured_at: 1000, tick_seconds: 15, paused: true }).quality).toBe('paused')
  })
  it('does not invent a historical timestamp outside observed samples', () => {
    expect(tickDateTime(2, { tick: 20, captured_at: 1000, tick_seconds: 15 }).quality).toBe('unknown')
  })
})
