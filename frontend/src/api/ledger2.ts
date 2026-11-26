export interface Ledger2Tx {
  type: 'tx'
  tx_id: number
  role: 'primary' | 'mirror'
  ref: string
  e: number
  i: number
  kind: 'expense' | 'income'
  date: string
  tx_time: string
  category: string | null
  counterparty: string | null
  item: string | null
  source: string | null
  note: string | null
  /** 人工命名（初始化时的标题 / 之后改的）；空则前端退回商品 / 交易对方 */
  title: string
  /** 人工备注 */
  memo: string
  category_path: string[]
}
export interface Ledger2Group {
  type: 'group'
  id: string
  name: string
  node_id: string
  note: string
  e: number
  i: number
  count: number
  members: Ledger2Tx[]
}
export interface Ledger2Node {
  type: 'node'
  id: string
  name: string
  note: string
  kind: 'root' | 'category' | 'node'
  e: number
  i: number
  count: number
  children: Array<Ledger2Node | Ledger2Group | Ledger2Tx>
}
export interface Ledger2HistoryItem {
  seq: number
  name: string
  state: 'applied' | 'undone'
  prims: number
  at: string
}
export interface Ledger2TimelineEvent {
  id: number
  seq: number
  name: string
  kind: 'apply' | 'undo' | 'redo'
  run_id: string | null
  request: string | null
  at: string
}
export interface Ledger2ReplayItem {
  ref: string
  kind: 'node' | 'group' | 'tx'
  name: string
  parent: string | null
  note: string
}
export interface Ledger2ReplayStep {
  id: number
  changed: string[]
  before: Ledger2ReplayItem[]
  after: Ledger2ReplayItem[]
}
export interface Ledger2Replay {
  steps: Ledger2ReplayStep[]
}
export interface Ledger2Status {
  seq: number
  can_undo: boolean
  can_redo: boolean
  history: Ledger2HistoryItem[]
  warnings: string[]
}
export interface Ledger2Tree {
  tree: Ledger2Node
  timeline?: Ledger2TimelineEvent[]
  scope: {
    date_from: string | null; date_to: string | null
    tx_in_scope: number; tx_total: number
  }
  /** 还没初始化的存活交易数 */
  uninit: number
  /** 存活交易的日期边界（读出口径，用来提示「最近有数据的月份」） */
  available: { first: string | null; last: string | null }
  status: Ledger2Status
  result?: {
    op: string
    created?: {
      /** 新建分组的节点 id */
      node_id?: string
      /** 新建净额组的组 id */
      group_id?: string
      /** 真正生成镜像的原始交易 id（选中的镜像交易已过滤） */
      split?: number[]
      /** 真正取消镜像的原始交易 id（前端把镜像交易换回原始交易） */
      unsplit?: number[]
      [key: string]: unknown
    }
  }
}
export interface Ledger2Op {
  type:
    | 'init' | 'uninstall' | 'create_node' | 'move' | 'join' | 'create_group' | 'dissolve'
    | 'split' | 'unsplit' | 'update'
    | 'dissolve_node' | 'rename_node' | 'update_node'
    | 'add_member' | 'remove_member' | 'rename_group' | 'dissolve_group'
  name?: string
  note?: string
  items?: string[]
  item?: string
  target?: string
  node_id?: string
  group_id?: string
  tx?: string
  tx_id?: number
  tx_ids?: number[]
  /** 初始化：按账单平台分类放置 */
  style?: string
  /** 初始化：自定义放置计划 */
  plan?: object
  /** 初始化：新建一个分组再全部放入 */
  new_node?: { name: string; parent?: string }
  /** 初始化：明确选择全部未初始化交易（默认就是全部） */
  all_uninitialized?: boolean
}
export interface Ledger2UninitItem {
  tx_id: number
  date: string
  tx_time: string
  counterparty: string
  item: string
  category: string
  e: number
  i: number
}
export interface Ledger2Uninit {
  total: number
  /** 全部未初始化 id；前端据此做全选/反选，写回时精确传 tx_ids */
  ids: number[]
  /** 前 N 笔的展示字段（列表用） */
  items: Ledger2UninitItem[]
  truncated: boolean
}
export interface Ledger2Range {
  date_from?: string
  date_to?: string
}

export class Ledger2Error extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(url, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    const err = await response.json().catch(() => ({}))
    const message = typeof err.message === 'string' ? err.message
      : typeof err.detail === 'string' ? err.detail : `请求失败（${response.status}）`
    throw new Ledger2Error(err.code ?? 'HTTP_' + response.status, message)
  }
  return response.json() as Promise<T>
}

export const getLedger2Uninitialized = (limit = 500) =>
  request<Ledger2Uninit>(`/api/ledger2/uninitialized?limit=${limit}`)

export function getLedger2Tree(range: Ledger2Range = {}) {
  const q = new URLSearchParams()
  if (range.date_from) q.set('date_from', range.date_from)
  if (range.date_to) q.set('date_to', range.date_to)
  const s = q.toString()
  return request<Ledger2Tree>(`/api/ledger2/tree${s ? '?' + s : ''}`)
}
export const applyLedger2Op = (op: Ledger2Op, range: Ledger2Range = {}) =>
  request<Ledger2Tree>('/api/ledger2/op', { op, ...range })
export const undoLedger2 = (range: Ledger2Range = {}) => request<Ledger2Tree>('/api/ledger2/undo', { ...range })
export const redoLedger2 = (range: Ledger2Range = {}) => request<Ledger2Tree>('/api/ledger2/redo', { ...range })
export const getLedger2Replay = (eventIds: number[]) =>
  request<Ledger2Replay>('/api/ledger2/replay', { event_ids: eventIds })
