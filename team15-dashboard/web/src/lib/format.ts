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

export function list(value: unknown): string {
  return Array.isArray(value) && value.length ? value.join(', ') : '—'
}
