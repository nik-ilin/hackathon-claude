import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MyListings } from './MyListings'

describe('MyListings group copy', () => {
  it('copies only the supplied public group message and excludes private listing details', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const safe = '🏪 TEAM 15 MARKET · v15\nTick 42 · 04/10 12:00\nPublic offers only'
    render(<MyListings offers={[{ id: 1, venue: 'v19', kind: 'Compra', cash_give: 999,
      wants: ['SECRET'], private_cap: 500 }]} venue={{ id: 'v15' }} tick={42} shareMessage={safe} />)
    fireEvent.click(screen.getByRole('button', { name: /Copiar mensaje para el grupo/ }))
    expect(writeText).toHaveBeenCalledWith(safe)
    expect(writeText.mock.calls[0][0]).not.toContain('SECRET')
    expect(writeText.mock.calls[0][0]).not.toContain('999')
  })
})
