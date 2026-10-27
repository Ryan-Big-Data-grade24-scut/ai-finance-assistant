/**
 * types/ledger.ts —— 账本契约（人工同步自 backend/schemas.py 第 6 节）
 *
 * ⚠️ 改后端 schemas.py 时必须同步改这里。
 *    字段名、类型、可空性必须一一对应。
 *
 * 口径提醒：账本的数字和 types/report.ts 的数字**不是一回事**。
 * 报表是平台口径的平铺统计，账本是用户自己分的组。两者不相加。
 */

export type LedgerNodeOp = 'ADD' | 'NET'
export type LedgerRole = 'primary' | 'mirror'
export type LedgerNodeKind = 'root' | 'group' | 'external_income'

export interface LedgerBook {
  book_id: string
  name: string
  date_from: string
  date_to: string
  member_count: number
  created_at: string
}

export interface LedgerBookListResp {
  books: LedgerBook[]
}

export interface LedgerNode {
  node_id: string
  parent_id: string | null
  name: string
  op: LedgerNodeOp
  kind: LedgerNodeKind
  depth: number
  /** 支出分量，单位元 */
  e: number
  /** 收入分量，单位元 */
  i: number
  /** 净值 i - e，正数是净流入 */
  net: number
  child_count: number
  leg_count: number
}

export interface LedgerLeg {
  tx_id: number
  role: LedgerRole
  node_id: string
  e: number
  i: number
}

export interface LedgerTree {
  book_id: string
  root_id: string
  root_e: number
  root_i: number
  root_net: number
  nodes: LedgerNode[]
  legs: LedgerLeg[]
  focus_node_ids: string[]
}

export interface LedgerInvariant {
  name: string
  ok: boolean
  message: string
}

export interface LedgerStatus {
  book_id: string
  total_tx: number
  placed_tx: number
  unplaced_tx: number
  node_count: number
  leg_count: number
  root_e: number
  root_i: number
  root_net: number
  invariants: LedgerInvariant[]
  invariants_ok: boolean
  current_seq: number
  can_undo: boolean
  can_redo: boolean
}

export interface LedgerOpResp extends LedgerTree {
  seq: number
  warnings: string[]
}

/**
 * 八个 op 的判别联合 —— 与后端一一对应。
 * switch (op.op) 能穷尽检查（联合类型 + 字面量 discriminant）。
 */
export type LedgerOpIn =
  | { op: 'set_op'; node_id: string; op_value: LedgerNodeOp }
  | { op: 'split'; tx_id: number }
  | { op: 'unsplit'; tx_id: number }
  | { op: 'create_node'; parent_id: string; name: string; op_value: LedgerNodeOp; tx_ids: number[] }
  | { op: 'dissolve_node'; node_id: string }
  | { op: 'reassign'; tx_id: number; to_node_id: string }
  | { op: 'attach_tx'; tx_id: number; node_id?: string | null }
  | { op: 'detach_tx'; tx_id: number }

/** 引擎拒绝时的响应体 */
export interface LedgerErrorBody {
  code: string
  message: string
  detail: Record<string, unknown>
}
