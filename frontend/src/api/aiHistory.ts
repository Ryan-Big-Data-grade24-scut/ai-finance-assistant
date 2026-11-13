export interface AIHistoryStatus {
  book_id: string
  total_transactions: number
  eligible_transactions: number
  already_reviewed: number
  pending_transactions: number
  protected_manual: number
  protected_manual_pending: number
  excluded_special: Record<string, number>
  active_rules: number
  latest_session: null | {
    id: string; status: string; total_count: number; processed_count: number
    error_message?: string | null; created_at: string; updated_at: string
  }
}

export interface AIHistorySuggestion {
  tx_id: number
  tx_time: string
  source: string
  current_path: string
  target_major: string | null
  target_minor: string | null
  target_path: string | null
  confidence: number
  reason: string
  needs_review: boolean
  question: string | null
  excluded: boolean
  is_new_category?: boolean
  auto_apply_eligible: boolean
  counterparty: string
  item: string
  amount: number
  direction: string
  history_evidence?: Array<{
    tx_id: number
    date: string
    counterparty: string
    item: string
    source: string
    confirmed_category: string
    evidence_type: string
    match_basis: string
  }>
  history_evidence_count?: number
}

export interface AIHistoryAnalysis {
  summary: string
  date_from?: string | null
  date_to?: string | null
  source_counts?: Record<string, number>
  taxonomy: Array<{ major: string; minor: string; path: string; description: string; is_new: boolean }>
  suggestions: AIHistorySuggestion[]
  counts: {
    total: number; suggested: number; excluded: number; needs_review: number
    high_confidence: number; new_categories: number
  }
  protected_manual: number
  already_reviewed: number
}

export interface AIHistorySession {
  session_id: string
  status: 'queued' | 'running' | 'preview' | 'partially_applied' | 'applied' | 'failed' | 'cancelled'
  base_seq: number
  total_count: number
  processed_count: number
  analysis: AIHistoryAnalysis | null
  result: null | { status: string; applied_count: number; applied_tx_ids: number[]; created_categories: string[]; remembered_rules: number; message: string }
  applied_tx_ids: number[]
  commit_request: null | { approved_category_paths: string[]; approved_tx_ids: number[]; remember_rule_tx_ids: number[] }
  error_message: string | null
  messages: Array<{ role: string; content: string; created_at: string }>
  created_at: string
  updated_at: string
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string') message = detail
    }
    throw new Error(message)
  }
  return payload as T
}

export async function getAIHistoryStatus(): Promise<AIHistoryStatus> {
  return parseResponse(await fetch('/api/ai/history/status'))
}

export async function startAIHistoryAnalysis(): Promise<{ session_id: string; status: string; total_count?: number; reused?: boolean }> {
  return parseResponse(await fetch('/api/ai/history/analyze', { method: 'POST' }))
}

export async function getAIHistorySession(sessionId: string): Promise<AIHistorySession> {
  return parseResponse(await fetch(`/api/ai/history/${encodeURIComponent(sessionId)}`))
}

export async function commitAIHistorySession(sessionId: string, payload: {
  approved_category_paths: string[]; approved_tx_ids: number[]; remember_rule_tx_ids: number[]
}): Promise<NonNullable<AIHistorySession['result']>> {
  return parseResponse(await fetch(`/api/ai/history/${encodeURIComponent(sessionId)}/commit`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}
