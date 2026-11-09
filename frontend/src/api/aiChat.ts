export interface AIChatConversation {
  id: string
  title: string
  created_at?: string
  updated_at?: string
  message_count?: number
}

export interface AIChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  created_at?: string
}

export interface AIChatAction {
  id: string
  action_type: 'create_category_rule' | 'reclassify_transaction' | 'bulk_reclassify_transactions' | 'create_manual_transaction'
  status: 'pending' | 'confirmed' | 'rejected'
  payload: Record<string, unknown>
  result?: Record<string, unknown> | null
  created_at?: string
  decided_at?: string | null
}

export interface AIChatAgentTaskStep {
  id: string
  tool: string
  purpose: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped'
  error?: string | null
  generation: number
}

export interface AIChatAgentEvidence {
  evidence_id: string
  tool: string
  scope: Record<string, string | number | boolean>
  state: 'verified' | 'no_data' | 'partial' | 'failed' | 'incomplete' | 'conflict' | 'proposal' | 'unknown'
  reason?: string | null
  summary?: Record<string, string | number | boolean>
}

export interface AIChatAgentTask {
  id: string
  status: 'planning' | 'running' | 'waiting_for_user' | 'completed' | 'failed' | 'interrupted'
  phase: string
  goal: string
  generation: number
  progress_message: string
  clarification_question?: string | null
  created_at?: string
  updated_at?: string
  completed_at?: string | null
  steps: AIChatAgentTaskStep[]
  completed_steps: number
  total_steps: number
  evidence?: AIChatAgentEvidence[]
}

export interface AIChatConversationDetail {
  conversation: AIChatConversation
  messages: AIChatMessage[]
  actions: AIChatAction[]
  agent_task?: AIChatAgentTask | null
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) {
        message = detail.map(item => (item as { msg?: string })?.msg || String(item)).join('；')
      }
    }
    throw new Error(message)
  }
  return payload as T
}

export async function listAIChatConversations(): Promise<{ conversations: AIChatConversation[] }> {
  return request('/api/ai/chat/conversations')
}

export async function createAIChatConversation(title = '新对话'): Promise<AIChatConversation> {
  return request('/api/ai/chat/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export async function getAIChatConversation(id: string): Promise<AIChatConversationDetail> {
  return request(`/api/ai/chat/conversations/${encodeURIComponent(id)}`)
}

export async function getLatestAIChatAgentTask(
  conversationId: string,
): Promise<{ agent_task: AIChatAgentTask | null }> {
  return request(`/api/ai/chat/conversations/${encodeURIComponent(conversationId)}/agent-tasks/latest`)
}

export async function sendAIChatMessage(
  id: string,
  content: string,
  context: Record<string, unknown>,
): Promise<AIChatConversationDetail & { assistant_message: string }> {
  return request(`/api/ai/chat/conversations/${encodeURIComponent(id)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, context }),
  })
}

export async function confirmAIChatAction(conversationId: string, actionId: string): Promise<{ status: string; message: string }> {
  return request(
    `/api/ai/chat/conversations/${encodeURIComponent(conversationId)}/actions/${encodeURIComponent(actionId)}/confirm`,
    { method: 'POST' },
  )
}

export async function rejectAIChatAction(conversationId: string, actionId: string): Promise<{ status: string; action_id: string }> {
  return request(
    `/api/ai/chat/conversations/${encodeURIComponent(conversationId)}/actions/${encodeURIComponent(actionId)}/reject`,
    { method: 'POST' },
  )
}

export async function deleteAIChatConversation(id: string): Promise<{ deleted: boolean }> {
  return request(`/api/ai/chat/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' })
}
