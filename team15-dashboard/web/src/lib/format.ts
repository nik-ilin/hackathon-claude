import type { JsonRecord } from './types'

export function num(value: unknown, digits = 2): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('es-ES', { maximumFractionDigits: digits }).format(value)
}

export function points(value: unknown, digits = 2): string {
  const n = num(value, digits)
  return n === '—' ? n : `${n} P`
}

export function ageLabel(timestamp: unknown): string {
  if (typeof timestamp !== 'number' || !Number.isFinite(timestamp)) return 'sin lectura reciente'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
  if (seconds < 60) return `actualizado hace ${seconds} s`
  if (seconds < 3600) return `actualizado hace ${Math.floor(seconds / 60)} min`
  return `actualizado hace ${Math.floor(seconds / 3600)} h`
}

export function dateTime(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return new Intl.DateTimeFormat('es-ES', { dateStyle: 'short', timeStyle: 'medium' }).format(value * 1000)
}

export function localDateTime(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return new Intl.DateTimeFormat('es-ES', { timeZone: 'Europe/Madrid', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(value * 1000)
}

export function tickDateTime(tick: unknown, context: JsonRecord = {}): { label: string; quality: string } {
  if (typeof tick !== 'number' || !Number.isFinite(tick)) return { label: 'hora no disponible', quality: 'unknown' }
  const points = (Array.isArray(context.samples) ? context.samples : [])
    .filter((point: JsonRecord) => typeof point.tick === 'number' && typeof point.captured_at === 'number')
    .sort((a: JsonRecord, b: JsonRecord) => a.tick - b.tick) as JsonRecord[]
  const exact = points.find((point) => point.tick === tick)
  if (exact) return { label: localDateTime(exact.captured_at), quality: 'observed' }
  const before = [...points].reverse().find((point) => point.tick < tick)
  const after = points.find((point) => point.tick > tick)
  if (before && after) {
    const ratio = (tick - before.tick) / (after.tick - before.tick)
    return { label: localDateTime(before.captured_at + (after.captured_at - before.captured_at) * ratio), quality: 'interpolated' }
  }
  if (tick === context.tick && typeof context.captured_at === 'number') return { label: localDateTime(context.captured_at), quality: 'observed' }
  if (tick > context.tick && typeof context.tick === 'number') {
    if (context.paused) return { label: 'en pausa · hora no estimable', quality: 'paused' }
    const interval = Number(context.tick_seconds)
    const nextIn = Number(context.next_tick_in ?? context.tick_seconds)
    if (Number.isFinite(interval) && interval > 0 && Number.isFinite(nextIn) && typeof context.captured_at === 'number') {
      const target = context.captured_at + nextIn + Math.max(0, tick - context.tick - 1) * interval
      return { label: `${localDateTime(target)} aprox.`, quality: 'estimated' }
    }
  }
  return { label: 'hora no disponible', quality: 'unknown' }
}

export function list(value: unknown): string {
  return Array.isArray(value) && value.length ? value.join(', ') : '—'
}
