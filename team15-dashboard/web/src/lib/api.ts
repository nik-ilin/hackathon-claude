import type { HistoryResponse, JsonRecord, V3Envelope, ViewKey } from './types'

export async function getSection<T = JsonRecord>(view: ViewKey, signal?: AbortSignal): Promise<V3Envelope<T>> {
  const response = await fetch(`/api/v3/${view}`, { signal, headers: { Accept: 'application/json' }, cache: 'no-store' })
  if (!response.ok) throw new Error(`La API respondió ${response.status} al cargar ${view}`)
  return response.json() as Promise<V3Envelope<T>>
}

export async function getHistory(section: string, ref?: string, signal?: AbortSignal): Promise<HistoryResponse> {
  const query = new URLSearchParams({ section, limit: '800' })
  if (ref) query.set('ref', ref)
  const response = await fetch(`/api/v3/history?${query}`, { signal, headers: { Accept: 'application/json' }, cache: 'no-store' })
  if (!response.ok) throw new Error(`No se pudo leer el histórico (${response.status})`)
  return response.json()
}
