/**
 * api/ledger.ts —— 账本接口（照抄 api/reports.ts 的模式）
 *
 * 全部用相对路径 + Vite proxy，和第一轮保持一致。
 *
 * ⚠️ 写操作只有一个入口：POST /api/ledger/op。
 *    撤销 / 重做 / 回到 M₀ 是三个独立路由（它们不是 op）。
 */
import type {
  LedgerBookListResp,
  LedgerOpIn,
  LedgerOpResp,
  LedgerStatus,
  LedgerTree,
} from '@/types/ledger'

function queryString(params: Record<string, string | number | boolean | undefined | null>) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  return query.toString()
}

async function getJson<T>(
  path: string,
  params: Record<string, string | number | boolean | undefined | null>,
): Promise<T> {
  const qs = queryString(params)
  const response = await fetch(qs ? `${path}?${qs}` : path)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || body?.message || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

async function postJson<T>(path: string, payload: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    // 引擎的错误体是 {code, message, detail} —— message 是人话
    const body = await response.json().catch(() => null)
    const err = new Error(body?.message || body?.detail || `请求失败（${response.status}）`)
    ;(err as Error & { code?: string }).code = body?.code
    throw err
  }
  return response.json() as Promise<T>
}

export function getLedgerBooks() {
  return getJson<LedgerBookListResp>('/api/ledger/books', {})
}

export function createLedgerBook(name: string, dateFrom: string, dateTo: string) {
  return postJson<LedgerTree>('/api/ledger/books', {
    name,
    date_from: dateFrom,
    date_to: dateTo,
  })
}

export function getLedgerTree(bookId: string, focusNodeId?: string | null) {
  return getJson<LedgerTree>('/api/ledger/tree', {
    book_id: bookId,
    focus_node_id: focusNodeId ?? undefined,
  })
}

export function getLedgerStatus(bookId: string) {
  return getJson<LedgerStatus>('/api/ledger/status', { book_id: bookId })
}

export function applyLedgerOp(bookId: string, op: LedgerOpIn) {
  return postJson<LedgerOpResp>('/api/ledger/op', { book_id: bookId, op })
}

export function undoLedger(bookId: string) {
  return postJson<LedgerOpResp>('/api/ledger/undo', { book_id: bookId })
}

export function redoLedger(bookId: string) {
  return postJson<LedgerOpResp>('/api/ledger/redo', { book_id: bookId })
}

export function resetLedger(bookId: string) {
  return postJson<LedgerOpResp>('/api/ledger/reset', { book_id: bookId })
}
