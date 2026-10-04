export type JsonRecord = Record<string, any>

export interface V3Envelope<T = JsonRecord> {
  schema: string
  section: string
  snapshot: { id: string; team: string; tick: number | null; captured_at: number | null; verified_private_data: boolean; read_only: true }
  sources: Record<string, JsonRecord>
  data: T
}

export type ViewKey = 'overview' | 'ranking' | 'operations' | 'duels' | 'market' | 'trading' | 'collection' | 'rivals' | 'signals' | 'agents'

export interface HistoryResponse {
  section: string
  ref?: string
  count: number
  points: JsonRecord[]
  retention_days: number
}
