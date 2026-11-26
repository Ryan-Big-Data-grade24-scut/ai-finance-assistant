/** /api/agentcore/* —— 启动、停止和订阅是三种独立请求。 */

export interface AgentSession {
  id: string
  title: string
  created_at?: string
  updated_at?: string
  active_run_id?: string | null
}

export type AgentTerminalState = 'completed' | 'failed' | 'stopped' | 'interrupted'
export type AgentRunState = 'running' | 'stopping' | AgentTerminalState

export interface AgentRun {
  id: string
  session_id: string
  provider: string
  model: string
  state: AgentRunState
  start_id: number
  end_id: number | null
}

export interface AgentTodo {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

export interface AgentToolCall {
  id: string
  name: string
  arguments: string
}

export interface AgentTerminal {
  id: number
  type: 'terminal'
  run_id: string
  state: AgentTerminalState
  step: number
  totals: Record<string, number>
  code: string | null
  message: string | null
}

export type AgentChatItem =
  | { id: number; type: 'user' | 'compaction'; text: string; at?: string }
  | { id: number; type: 'assistant'; text: string; calls: AgentToolCall[]; step: number | null; at?: string }
  | { id: number; type: 'tool'; name: string; call_id: string; result: Record<string, unknown> | string | null;
      step: number | null; is_error: boolean; recovered: boolean; at?: string }
  | AgentTerminal

export interface AgentDiffItem {
  ref: string
  name?: string | null
  kind?: string | null
  parent?: string | null
  from?: string | null
  to?: string | null
}

export interface AgentImportPreview {
  platform_name?: string
  filename?: string
  total?: number
  date_from?: string
  date_to?: string
  by_status?: Record<string, number>
  by_direction?: Record<string, number>
  amount_sum_expense?: number
  duplicate_estimate?: number
  warnings?: string[]
}

export interface AgentPreview extends AgentImportPreview {
  created?: AgentDiffItem[]
  removed?: AgentDiffItem[]
  moved?: AgentDiffItem[]
  renamed?: AgentDiffItem[]
  counts?: { created: number; removed: number; moved: number; renamed: number }
  note?: string
}

export interface AgentAction {
  id: string
  kind: string
  title: string
  state: 'pending' | 'confirmed' | 'rejected'
  op: Record<string, unknown>
  preview: AgentPreview
  result: Record<string, unknown> | null
  created_at?: string
  decided_at?: string | null
}

export interface AgentSessionDetail {
  session: AgentSession
  run: AgentRun | null
  cursor: number
  messages: AgentChatItem[]
  todos: AgentTodo[]
  status: string
  step: number
  totals: Record<string, number> | null
  actions: AgentAction[]
  memory_index: string
}

export interface AgentStagePreview {
  stage_id: string
  state: 'staged'
  platform_id: string
  platform_name: string
  total: number
  date_from: string
  date_to: string
  failed: number
  warnings: string[]
  note: string
}

export interface AgentImportCommitted {
  stage_id: string
  state: 'committed'
  receipt: { batch_id: string; inserted: number; skipped: number; failed: number }
}

export type AgentStage = AgentStagePreview | AgentImportCommitted

export type AgentEvent = { cursor: number } & (
  | { type: 'message'; item: AgentChatItem }
  | { type: 'terminal'; item: AgentTerminal }
  | { type: 'status'; text: string; step?: number; state?: 'stopping' }
  | { type: 'usage'; totals: Record<string, number> }
)

export class AgentCoreError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

async function readError(response: Response): Promise<AgentCoreError> {
  const payload: unknown = await response.json().catch(() => null)
  let code = `HTTP_${response.status}`
  let message = `请求失败（HTTP ${response.status}）`
  if (payload && typeof payload === 'object') {
    const body = payload as { code?: unknown; message?: unknown; detail?: unknown }
    if (typeof body.code === 'string') code = body.code
    if (typeof body.message === 'string') message = body.message
    else if (typeof body.detail === 'string') message = body.detail
  }
  return new AgentCoreError(code, message)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) throw await readError(response)
  return response.json() as Promise<T>
}

const json = (body: object): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

// ── 会话 ──────────────────────────────────────────────────────────────────────

export const listSessions = () =>
  request<{ sessions: AgentSession[] }>('/api/agentcore/sessions')

export const createSession = (title = '') =>
  request<AgentSession>('/api/agentcore/sessions', json({ title }))

export const getSession = (id: string) =>
  request<AgentSessionDetail>(`/api/agentcore/sessions/${encodeURIComponent(id)}`)

export const renameSession = (id: string, title: string) =>
  request<AgentSession>(`/api/agentcore/sessions/${encodeURIComponent(id)}`, { ...json({ title }), method: 'PATCH' })

export const deleteSession = (id: string) =>
  request<{ ok: boolean }>(`/api/agentcore/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })

const turnsUrl = (id: string) => `/api/agentcore/sessions/${encodeURIComponent(id)}/turns`
const runUrl = (sessionId: string, runId: string) => `${turnsUrl(sessionId)}/${encodeURIComponent(runId)}`

export const startTurn = (id: string, text: string, page: Record<string, unknown>) =>
  request<AgentRun>(turnsUrl(id), json({ text, page }))

export const stopTurn = (sessionId: string, runId: string) =>
  request<AgentRun>(`${runUrl(sessionId, runId)}/stop`, json({}))

/** EOF 不是终态；断线或坏帧都由调用方从最后应用过的游标重接。 */
export async function subscribeTurn(
  run: AgentRun,
  after: number,
  onEvent: (event: AgentEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${runUrl(run.session_id, run.id)}/events?after=${after}`, { signal })
  if (!response.ok) throw await readError(response)
  if (!response.body) throw new AgentCoreError('NO_STREAM', '浏览器不支持流式读取。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) throw new AgentCoreError('STREAM_DISCONNECTED', '连接断开，尚未收到本轮终态。')
      buffer += decoder.decode(value, { stream: true })
      let cut: number
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const lines = buffer.slice(0, cut).split('\n')
        buffer = buffer.slice(cut + 2)
        const data = lines.filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n')
        if (!data) continue  // 保活注释没有游标。
        const event = JSON.parse(data) as AgentEvent
        onEvent(event)
        if (event.type === 'terminal') return
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

// ── 上传与导入卡 ──────────────────────────────────────────────────────────────

export async function uploadBill(file: File, password: string): Promise<AgentStagePreview> {
  const form = new FormData()
  form.append('file', file)
  if (password) form.append('alipay_password', password)
  const response = await fetch('/api/agentcore/upload', { method: 'POST', body: form })
  if (!response.ok) throw await readError(response)
  return response.json() as Promise<AgentStagePreview>
}

export const getStage = (id: string) =>
  request<AgentStage>(`/api/agentcore/import/stages/${encodeURIComponent(id)}`)

export const prepareImport = (stageId: string, sessionId: string | null) =>
  request<{ action_id: string; state: string; preview: AgentImportPreview; note: string }>(
    '/api/agentcore/import/prepare',
    json({ stage_id: stageId, session_id: sessionId }),
  )

// ── 待确认动作 ────────────────────────────────────────────────────────────────

export const listActions = (params: {
  state?: 'pending' | 'confirmed' | 'rejected'
  unclaimed?: boolean
  sessionId?: string
} = {}) => {
  const q = new URLSearchParams()
  if (params.state) q.set('state', params.state)
  if (params.unclaimed) q.set('unclaimed', 'true')
  if (params.sessionId) q.set('session_id', params.sessionId)
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return request<{ actions: AgentAction[] }>(`/api/agentcore/actions${suffix}`)
}

export const getAction = (id: string) =>
  request<AgentAction>(`/api/agentcore/actions/${encodeURIComponent(id)}`)

export const confirmAction = (id: string, sessionId: string | null) =>
  request<Record<string, unknown>>(
    `/api/agentcore/actions/${encodeURIComponent(id)}/confirm`,
    json({ session_id: sessionId }),
  )

export const rejectAction = (id: string, sessionId: string | null) =>
  request<Record<string, unknown>>(
    `/api/agentcore/actions/${encodeURIComponent(id)}/reject`,
    json({ session_id: sessionId }),
  )
