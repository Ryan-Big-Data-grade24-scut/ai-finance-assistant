/**
 * 页面 → AgentCore 的单轮快照载荷。
 *
 * 完整助手（/v2/assistant）和未来的跨页面宠物共用同一个形状：
 * 路由、日期范围、完整选区、已解析附件。宠物只是 `surface: 'pet'`，
 * 旁栏收起，但看到的对象与大页面一致。后端仍会做字段白名单和上限校验，
 * 这里不静默裁剪选区——裁掉多少必须由后端在快照里明确告诉模型。
 */
export type AgentSurface = 'page' | 'pet'

export interface AgentStagedImport {
  stage_id: string
  platform_name?: string
  total?: number
  date_from?: string
  date_to?: string
}

export interface AgentPageContext {
  // 便于直接作为 startTurn 的 page 载荷（Record<string, unknown>）传递。
  [key: string]: unknown
  route: string
  surface: AgentSurface
  scope: { date_from?: string; date_to?: string }
  selection: string[]
  staged_import?: AgentStagedImport
}

type QueryLike = Record<string, unknown>

function cleanString(value: unknown, maxChars: number): string | undefined {
  if (typeof value !== 'string') return undefined
  const text = value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim()
  return text ? text.slice(0, maxChars).trim() : undefined
}

function cleanDate(value: unknown): string | undefined {
  const text = cleanString(value, 10)
  if (!text || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return undefined
  const [year, month, day] = text.split('-').map(Number)
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
    ? text
    : undefined
}

/** 从 Ledger2 跳转时写入 history.state 的完整选区；兼容旧的 ?sel= 链接。 */
export function readAgentSelection(query: QueryLike = {}): string[] {
  const state = typeof window !== 'undefined'
    ? (window.history.state as { agentcoreSelection?: unknown } | null)?.agentcoreSelection
    : undefined
  if (Array.isArray(state)) {
    return state.filter((ref): ref is string => typeof ref === 'string' && !!ref.trim())
  }
  const raw = query.sel
  return typeof raw === 'string' ? raw.split(',').map(ref => ref.trim()).filter(Boolean) : []
}

export function buildAgentPageContext(input: {
  route: string
  surface: AgentSurface
  dateFrom?: unknown
  dateTo?: unknown
  selection?: unknown
  stagedImport?: Record<string, unknown> | null
}): AgentPageContext {
  const scope: AgentPageContext['scope'] = {}
  const dateFrom = cleanDate(input.dateFrom)
  const dateTo = cleanDate(input.dateTo)
  if (dateFrom) scope.date_from = dateFrom
  if (dateTo) scope.date_to = dateTo

  const selection = Array.isArray(input.selection)
    ? input.selection
      .filter((ref): ref is string => typeof ref === 'string')
      .map(ref => ref.trim())
      .filter(Boolean)
    : []

  const out: AgentPageContext = {
    route: cleanString(input.route, 200) ?? '',
    surface: input.surface,
    scope,
    selection,
  }

  const stage = input.stagedImport
  const stageId = cleanString(stage?.stage_id, 120)
  if (stageId) {
    const staged: AgentStagedImport = { stage_id: stageId }
    const platformName = cleanString(stage?.platform_name, 80)
    if (platformName) staged.platform_name = platformName
    if (typeof stage?.total === 'number' && Number.isInteger(stage.total) && stage.total >= 0) {
      staged.total = stage.total
    }
    const stageFrom = cleanDate(stage?.date_from)
    const stageTo = cleanDate(stage?.date_to)
    if (stageFrom) staged.date_from = stageFrom
    if (stageTo) staged.date_to = stageTo
    out.staged_import = staged
  }
  return out
}
