export type LedgerOp = 'ADD' | 'NET'
export interface LedgerBook {
  id: string; name: string; date_from: string; date_to: string; tx_count: number; current_seq: number
}
export interface LedgerNode {
  id: string; parent_id: string | null; name: string; kind: string; op: LedgerOp
  e: number; i: number; net: number; count: number
}
export interface LedgerTx {
  id: number; date: string; tx_time: string; direction: 'expense' | 'income'; source: string
  amount: number; counterparty: string; item: string; pay_method: string; node_id: string
  funding?: string; tx_no?: string; merchant_no?: string; status?: string; tx_type?: string
  refund_zeroed?: boolean; partial_refund?: boolean; refund_conflict?: boolean
  refunded_amount?: number; effective_amount?: number
  internal_transfer_zeroed?: boolean; split?: boolean
}
export interface LedgerLeg {
  leg_id: string; tx_id: number; node_id: string; role: 'primary' | 'mirror'; e: number; i: number
}
export interface LedgerAudit {
  raw_net: number
  neutral_direction_delta?: number
  product_net?: number
  refund_delta?: number
  post_refund_net?: number
  transfer_delta?: number
  after_transfer_net?: number
  split_delta?: number
  transformation_delta: number
  expected_net: number
  root_net: number
}
export interface LedgerTree {
  book: LedgerBook; root_id: string; nodes: LedgerNode[]; transactions: LedgerTx[]
  legs?: LedgerLeg[]; audit?: LedgerAudit; audit_ok?: boolean; warnings?: string[]
  refund_pairs?: [number, number][]
  refund_amounts_by_original?: Record<string, number>
  refund_conflict_ids?: number[]
  internal_transfer_pairs?: [number, number][]
  split_count?: number
  root_e: number; root_i: number; root_net: number; count: number; invariants_ok: boolean
  source_count?: number; total_count?: number; database_path?: string
  invariants: string[]; seq: number; can_undo: boolean; can_redo: boolean
}
export type LedgerOperation =
  | { op: 'reassign'; tx_id: number; to_node_id: string }
  | { op: 'create_node'; parent_id: string; name: string; op_value: LedgerOp; tx_ids?: number[] }
  | { op: 'set_op'; node_id: string; op_value: LedgerOp }
  | { op: 'dissolve_node'; node_id: string }
  | { op: 'attach_tx' | 'detach_tx' | 'split' | 'unsplit'; tx_id: number; node_id?: string }

async function request<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(url, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(error.message || error.detail || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}
export const listLedgerBooks = () => request<{books: LedgerBook[]}>('/api/ledger/books')
export const createLedgerBook = (name: string, date_from: string, date_to: string) =>
  request<LedgerTree>('/api/ledger/books', { name, date_from, date_to })
export const getLedgerTree = (book_id: string) =>
  request<LedgerTree>(`/api/ledger/tree?book_id=${encodeURIComponent(book_id)}`)
export const applyLedgerOp = (book_id: string, op: LedgerOperation, expected_seq: number) =>
  request<LedgerTree>('/api/ledger/op', { book_id, op, expected_seq })
export const historyLedger = (book_id: string, action: 'undo' | 'redo' | 'reset', expected_seq: number) =>
  request<LedgerTree>(`/api/ledger/${action}`, { book_id, expected_seq })

export interface StarFilters { date_from?: string; date_to?: string; category_node_id?: string }
export function getLiveLedgerTree(filters: StarFilters = {}) {
  const q = new URLSearchParams()
  if (filters.date_from) q.set('date_from', filters.date_from)
  if (filters.date_to) q.set('date_to', filters.date_to)
  if (filters.category_node_id) q.set('category_node_id', filters.category_node_id)
  const suffix = q.toString()
  return request<LedgerTree>(`/api/ledger/live${suffix ? '?' + suffix : ''}`)
}
