/**
 * stores/ledger.ts —— 星图的状态（Pinia）
 *
 * 三条纪律：
 *   1. **前端不算钱** —— 树和每个节点的 (e,i) 全部来自引擎。
 *      本 store 只做「把引擎返回的树原样存下来」。
 *   2. **乐观更新只用于视觉**，落库后必须用引擎返回的权威树覆盖。
 *   3. **撤销栈在引擎里** —— 前端只留 can_undo / can_redo 两个标志。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '@/api/ledger'
import type {
  LedgerBook,
  LedgerNode,
  LedgerOpIn,
  LedgerStatus,
  LedgerTree,
} from '@/types/ledger'

export const useLedgerStore = defineStore('ledger', () => {
  const books = ref<LedgerBook[]>([])
  const currentBookId = ref<string>('')
  const tree = ref<LedgerTree | null>(null)
  const status = ref<LedgerStatus | null>(null)
  const selectedNodeId = ref<string>('')
  const selectedTxId = ref<number | null>(null)
  const focusNodeId = ref<string>('')
  const pending = ref(false)
  const lastError = ref<string>('')
  /** 被引擎拒绝时，抖一下哪个节点 */
  const shakeNodeId = ref<string>('')

  async function loadBooks() {
    books.value = (await api.getLedgerBooks()).books
    if (!currentBookId.value && books.value.length) {
      currentBookId.value = books.value[0].book_id
    }
  }

  async function selectBook(bookId: string) {
    currentBookId.value = bookId
    selectedNodeId.value = ''
    selectedTxId.value = null
    await refresh()
  }

  async function refresh() {
    if (!currentBookId.value) return
    pending.value = true
    try {
      const [t, s] = await Promise.all([
        api.getLedgerTree(currentBookId.value, focusNodeId.value || undefined),
        api.getLedgerStatus(currentBookId.value),
      ])
      tree.value = t
      status.value = s
      lastError.value = ''
    } catch (e) {
      lastError.value = (e as Error).message
    } finally {
      pending.value = false
    }
  }

  /**
   * 发一次操作。**只用引擎返回的树覆盖本地状态**。
   * 引擎拒绝时把 message 原样留下 —— 前端不自己拼拒绝文案。
   */
  async function applyOp(op: LedgerOpIn, shakeNode?: string | number) {
    if (!currentBookId.value) return
    pending.value = true
    try {
      const resp = await api.applyLedgerOp(currentBookId.value, op)
      tree.value = resp
      lastError.value = ''
      status.value = await api.getLedgerStatus(currentBookId.value)
      if (resp.warnings?.length) lastError.value = resp.warnings[0]
    } catch (e) {
      lastError.value = (e as Error).message
      if (shakeNode !== undefined && shakeNode !== null) {
        const key = String(shakeNode)
        shakeNodeId.value = key
        setTimeout(() => {
          if (shakeNodeId.value === key) shakeNodeId.value = ''
        }, 600)
      }
    } finally {
      pending.value = false
    }
  }

  async function undo() {
    if (!currentBookId.value) return
    pending.value = true
    try {
      tree.value = await api.undoLedger(currentBookId.value)
      status.value = await api.getLedgerStatus(currentBookId.value)
      lastError.value = ''
    } catch (e) {
      lastError.value = (e as Error).message
    } finally {
      pending.value = false
    }
  }

  async function redo() {
    if (!currentBookId.value) return
    pending.value = true
    try {
      tree.value = await api.redoLedger(currentBookId.value)
      status.value = await api.getLedgerStatus(currentBookId.value)
      lastError.value = ''
    } catch (e) {
      lastError.value = (e as Error).message
    } finally {
      pending.value = false
    }
  }

  async function resetToM0() {
    if (!currentBookId.value) return
    pending.value = true
    try {
      tree.value = await api.resetLedger(currentBookId.value)
      status.value = await api.getLedgerStatus(currentBookId.value)
      lastError.value = ''
    } catch (e) {
      lastError.value = (e as Error).message
    } finally {
      pending.value = false
    }
  }

  // ---- 派生 ----
  function nodeById(nodeId: string): LedgerNode | undefined {
    return tree.value?.nodes.find(n => n.node_id === nodeId)
  }

  function legsOf(nodeId: string) {
    return (tree.value?.legs ?? []).filter(l => l.node_id === nodeId)
  }

  return {
    books,
    currentBookId,
    tree,
    status,
    selectedNodeId,
    selectedTxId,
    focusNodeId,
    pending,
    lastError,
    shakeNodeId,
    loadBooks,
    selectBook,
    refresh,
    applyOp,
    undo,
    redo,
    resetToM0,
    nodeById,
    legsOf,
  }
})
