export interface AISettings {
  configured: boolean
  provider: 'openai_compatible' | 'anthropic'
  provider_label: string
  base_url: string
  model: string
  api_key_masked: string | null
  auto_apply_rules: boolean
  allow_external_processing: boolean
  ai_ready: boolean
  confidence_threshold: number
}

export interface AITaxonomyItem {
  major: string
  minor: string
  path: string
  description: string
  is_new: boolean
}

export interface AIImportSuggestion {
  row_index: number
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
  matched_rule_ids: string[]
  counterparty: string
  item: string
  amount: number
  refunded_amount?: number
  effective_amount?: number
  direction: string
  calibrated?: boolean
}

export interface AIImportAnalysis {
  summary: string
  taxonomy: AITaxonomyItem[]
  suggestions: AIImportSuggestion[]
  counts: { total: number; excluded: number; auto_apply: number; review: number; new_categories: number }
}

export interface AIImportAnalyzeResp {
  session_id: string
  platform_id: string
  platform_confidence: number
  platform_name: string
  total: number
  date_from: string
  date_to: string
  amount_sum: number
  by_direction: Record<string, number>
  by_status: Record<string, number>
  sample: Array<Record<string, unknown>>
  warnings: string[]
  analysis: AIImportAnalysis
}

export interface AIImportMessage { role: 'user' | 'assistant' | 'system'; content: string; created_at: string }
export interface AIImportCommitResp {
  status: string
  batch_id: string
  inserted: number
  skipped: number
  failed: number
  auto_applied: number
  manually_applied: number
  pending_review: number
  excluded_special: number
  auto_proposal_ids: string[]
  manual_proposal_ids: string[]
  pending_proposal_ids: string[]
  message: string
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.map(item => (item as { msg?: string })?.msg || String(item)).join('；')
    }
    throw new Error(message)
  }
  return payload as T
}

export async function getAISettings(): Promise<AISettings> {
  return parseResponse(await fetch('/api/ai/settings'))
}

export async function saveAISettings(payload: {
  provider: AISettings['provider']; base_url: string; model: string; api_key?: string
  clear_api_key?: boolean; auto_apply_rules: boolean; allow_external_processing: boolean; confidence_threshold: number
}): Promise<AISettings> {
  return parseResponse(await fetch('/api/ai/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function testAISettings(): Promise<{ ok: boolean; model: string; message: string }> {
  return parseResponse(await fetch('/api/ai/settings/test', { method: 'POST' }))
}

export async function deleteAIKey(): Promise<AISettings> {
  return parseResponse(await fetch('/api/ai/settings/key', { method: 'DELETE' }))
}

function makeFormData(file: File, alipayPassword: string): FormData {
  const body = new FormData()
  body.append('file', file, file.name)
  if (file.name.toLowerCase().endsWith('.zip') && alipayPassword) body.append('alipay_password', alipayPassword)
  return body
}

export async function analyzeAIImport(file: File, alipayPassword = '', signal?: AbortSignal): Promise<AIImportAnalyzeResp> {
  return parseResponse(await fetch('/api/ai/import/analyze', {
    method: 'POST', body: makeFormData(file, alipayPassword), signal,
  }))
}

export async function calibrateAIImport(sessionId: string, message: string): Promise<{
  assistant_message: string; analysis: AIImportAnalysis; messages: AIImportMessage[]
}> {
  return parseResponse(await fetch(`/api/ai/import/${encodeURIComponent(sessionId)}/calibrate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }),
  }))
}

export async function commitAIImport(sessionId: string, payload: {
  approved_category_paths: string[]; approved_row_indices: number[]; remember_rule_row_indices: number[]
}): Promise<AIImportCommitResp> {
  return parseResponse(await fetch(`/api/ai/import/${encodeURIComponent(sessionId)}/commit`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}


export interface AIProposalSummary {
  id: string
  status: string
  created_by: string
  created_at: string
  base_seq: number
  stale: boolean
  suggestion_count: number
}

export interface AIProposalSuggestion {
  tx_id: number
  status: string
  current_path: string
  target_path: string | null
  target_node_id: string | null
  confidence: number
  reason: string
  needs_clarification: boolean
  question: string | null
  remember_rule: boolean
  counterparty: string
  item: string
  amount: number
  direction: string
  source: string
}

export interface AIProposalDetail {
  id: string
  book_id: string
  status: string
  base_seq: number
  current_seq: number
  stale: boolean
  ready_count: number
  clarification_count: number
  available_categories: Array<{ node_id: string; path: string }>
  suggestions: AIProposalSuggestion[]
}

export async function getAIProposals(status = 'draft', limit = 50): Promise<{ proposals: AIProposalSummary[]; current_seq: number }> {
  return parseResponse(await fetch(`/api/ai/classification/proposals?status=${encodeURIComponent(status)}&limit=${limit}`))
}

export async function getAIProposal(proposalId: string): Promise<AIProposalDetail> {
  return parseResponse(await fetch(`/api/ai/classification/proposals/${encodeURIComponent(proposalId)}`))
}

export async function confirmAIProposal(proposalId: string, approvedTxIds: number[], expectedSeq: number): Promise<unknown> {
  return parseResponse(await fetch(`/api/ai/classification/proposals/${encodeURIComponent(proposalId)}/confirm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved_tx_ids: approvedTxIds, expected_seq: expectedSeq }),
  }))
}

export async function resolveAIProposal(proposalId: string, payload: {
  tx_id: number; target_node_id: string; confidence: number; reason: string; user_answer: string;
  remember_rule: boolean; rule_field?: string; rule_pattern?: string; expected_seq: number
}): Promise<AIProposalDetail> {
  return parseResponse(await fetch(`/api/ai/classification/proposals/${encodeURIComponent(proposalId)}/resolve`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  }))
}

export async function rejectAIProposal(proposalId: string, reason: string): Promise<unknown> {
  return parseResponse(await fetch(`/api/ai/classification/proposals/${encodeURIComponent(proposalId)}/reject`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason }),
  }))
}
