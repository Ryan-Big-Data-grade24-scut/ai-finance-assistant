/**
 * 账本助手的会话与回合状态。
 *
 * 完整页面和跨页面浮窗共用这一份逻辑：两边读同一个 `agentcore.session`，
 * 打开时自动接上最近一轮。页面离开不停止运行；浮窗收起也不停止。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import {
  AgentCoreError, createSession, deleteSession, getSession, listSessions,
  startTurn, stopTurn, subscribeTurn,
  type AgentChatItem, type AgentEvent, type AgentRun, type AgentSession,
  type AgentSessionDetail, type AgentTodo,
} from '@/api/agentCore'

export interface AgentSessionOptions {
  /** 本轮开始时附带的页面快照（范围、选区、暂存附件）。 */
  pageContext: () => Record<string, unknown>
  /** 工具成功返回对象结果时的旁路，用于同步本地上传面板等页面状态。 */
  onToolResult?: (name: string, result: Record<string, unknown>) => void
  /** 一轮结束后的刷新，例如重读旧卡或恢复附件状态。 */
  onRunEnd?: () => void | Promise<void>
  /** 页面上传/恢复附件期间先不要发新消息。 */
  canSend?: () => boolean
}

function formatError(error: unknown): string {
  if (error instanceof AgentCoreError) {
    if (error.code === 'STALE_ACTION') return '账本在预览之后变过，这张卡已失效；请拒绝旧卡并按当前账本操作。'
    if (error.code === 'ACTION_NOT_PENDING') return '这个操作已经被处理过，卡片已刷新。'
  }
  return error instanceof Error ? error.message : '操作失败。'
}

export function useAgentSession(options: AgentSessionOptions) {
  const sessions = ref<AgentSession[]>([])
  const detail = ref<AgentSessionDetail | null>(null)
  const input = ref('')
  const composing = ref(false)
  const opening = ref(true)
  const sending = ref(false)
  const stopping = ref(false)
  const reconnectText = ref('')
  const errorBanner = ref<{ code: string; message: string } | null>(null)
  const notice = ref('')

  let controller: AbortController | null = null
  let disposed = false
  let started = false

  const currentId = computed(() => detail.value?.session.id ?? null)
  const run = computed(() => detail.value?.run ?? null)
  const active = computed(() => run.value !== null && run.value.end_id === null)
  const items = computed(() => detail.value?.messages ?? [])
  const todos = computed(() => detail.value?.todos ?? [])

  function showError(error: unknown, fallback: string) {
    errorBanner.value = { code: error instanceof AgentCoreError ? error.code : fallback, message: formatError(error) }
  }

  async function refreshSessions() {
    const next = await listSessions()
    if (!disposed) sessions.value = next.sessions
  }

  function disconnect() {
    controller?.abort()
    controller = null
    reconnectText.value = ''
  }

  function openSnapshot(next: AgentSessionDetail) {
    disconnect()
    errorBanner.value = null
    notice.value = ''
    detail.value = next
    localStorage.setItem('agentcore.session', next.session.id)
    if (next.run?.end_id === null) follow(next.run)
  }

  async function loadSession(id: string) {
    const next = await getSession(id)
    if (!disposed) openSnapshot(next)
  }

  async function openSession(id: string) {
    if (disposed || sending.value || opening.value) return
    opening.value = true
    errorBanner.value = null
    try {
      await loadSession(id)
    } catch (error) {
      if (!disposed) showError(error, 'LOAD_FAILED')
    } finally {
      opening.value = false
    }
  }

  async function createAndOpenSession() {
    const next = await createSession('ATLAS')
    if (disposed) return
    sessions.value.unshift(next)
    await loadSession(next.id)
  }

  async function newSession() {
    if (disposed || sending.value || opening.value) return
    opening.value = true
    errorBanner.value = null
    try {
      await createAndOpenSession()
    } catch (error) {
      if (!disposed) showError(error, 'SESSION_FAILED')
    } finally {
      opening.value = false
    }
  }

  async function removeCurrent() {
    const id = currentId.value
    if (disposed || !id || active.value || opening.value || sending.value) return
    if (!window.confirm('删除这个会话？聊天记录一起删除，未处理的旧卡会作废。已经提交的业务操作不会撤销。')) return
    opening.value = true
    errorBanner.value = null
    try {
      await deleteSession(id)
      if (disposed) return
      disconnect()
      detail.value = null
      localStorage.removeItem('agentcore.session')
      await refreshSessions()
      if (disposed) return
      if (sessions.value.length) await loadSession(sessions.value[0].id)
      else await createAndOpenSession()
      if (!disposed) notice.value = '会话已删除。'
    } catch (error) {
      if (!disposed) showError(error, 'DELETE_FAILED')
    } finally {
      opening.value = false
    }
  }

  function consume(event: AgentEvent, nextRun: AgentRun) {
    const view = detail.value
    if (!view || view.run?.id !== nextRun.id || event.cursor <= view.cursor) return
    if (event.type === 'status') {
      view.status = event.text
      if (event.step != null) view.step = event.step
      if (event.state === 'stopping' && view.run.end_id === null) view.run.state = 'stopping'
    } else if (event.type === 'usage') {
      view.totals = event.totals
    } else {
      const item = event.item
      view.messages.push(item)
      if (item.type === 'terminal') {
        view.run.state = item.state
        view.run.end_id = item.id
        view.totals = item.totals
        view.step = item.step
        view.status = ''
      } else if (item.type === 'tool' && !item.is_error && item.result && typeof item.result === 'object') {
        const result = item.result as Record<string, unknown>
        if (item.name === 'todo_write' && Array.isArray(result.todos)) view.todos = result.todos as AgentTodo[]
        options.onToolResult?.(item.name, result)
      }
    }
    view.cursor = event.cursor
    reconnectText.value = ''
  }

  function follow(nextRun: AgentRun) {
    disconnect()
    const local = new AbortController()
    controller = local
    void (async () => {
      let failures = 0
      try {
        while (!local.signal.aborted && detail.value?.run?.id === nextRun.id && active.value) {
          try {
            await subscribeTurn(nextRun, detail.value.cursor, event => {
              if (!local.signal.aborted) {
                consume(event, nextRun)
                failures = 0
              }
            }, local.signal)
            break
          } catch (error) {
            if (local.signal.aborted) return
            if (error instanceof AgentCoreError && error.code === 'RUN_NOT_FOUND') {
              reconnectText.value = '找不到该轮，请刷新会话读取最新状态。'
              return
            }
            reconnectText.value = '订阅已断开，正在重连。断开不会停止运行。'
            await new Promise(resolve => window.setTimeout(resolve, Math.min(500 * 2 ** failures++, 5000)))
          }
        }
        if (!local.signal.aborted) await options.onRunEnd?.()
      } finally {
        if (controller === local) controller = null
      }
    })()
  }

  async function send() {
    const view = detail.value
    const text = input.value.trim()
    if (!view || !text || active.value || sending.value || opening.value) return
    if (options.canSend && !options.canSend()) return
    sending.value = true
    errorBanner.value = null
    notice.value = ''
    try {
      const nextRun = await startTurn(view.session.id, text, options.pageContext())
      if (detail.value !== view) return
      if (input.value.trim() === text) input.value = ''
      view.run = nextRun
      view.cursor = nextRun.start_id - 1
      view.status = '运行已启动，正在接收步骤…'
      view.step = 0
      view.totals = null
      follow(nextRun)
    } catch (error) {
      // POST 可能已成功但回复丢失；只读回最新运行，绝不自动重发一个可能已经写账的请求。
      if (detail.value === view) {
        const next = await getSession(view.session.id).catch(() => null)
        const recovered = next?.run
        if (detail.value === view && next && recovered && recovered.id !== view.run?.id) {
          if (input.value.trim() === text && next.messages.some(item =>
            item.type === 'user' && item.id >= recovered.start_id && item.text === text)) input.value = ''
          openSnapshot(next)
          notice.value = '已从会话记录接回最新一轮，没有重发消息。'
        } else if (detail.value === view) {
          showError(error, 'TURN_FAILED')
        }
      }
    } finally {
      sending.value = false
    }
  }

  async function stop() {
    const nextRun = run.value
    if (!nextRun || nextRun.state !== 'running' || stopping.value) return
    stopping.value = true
    try {
      const stopped = await stopTurn(nextRun.session_id, nextRun.id)
      if (run.value?.id === stopped.id && active.value && stopped.state === 'stopping') {
        run.value.state = 'stopping'
      }
    } catch (error) {
      if (run.value?.id === nextRun.id) showError(error, 'STOP_FAILED')
    } finally {
      stopping.value = false
    }
  }

  /** 打开页面或浮窗时接上最近会话；只执行一次。 */
  async function init() {
    if (started || disposed) return
    started = true
    try {
      await refreshSessions()
      if (disposed) return
      const stored = localStorage.getItem('agentcore.session')
      const pick = sessions.value.find(candidate => candidate.id === stored) ?? sessions.value[0]
      if (pick) await loadSession(pick.id)
      else await createAndOpenSession()
    } catch (error) {
      if (!disposed) showError(error, 'LOAD_FAILED')
    } finally {
      opening.value = false
    }
  }

  onBeforeUnmount(() => {
    disposed = true
    detail.value = null
    disconnect()
  })

  return {
    sessions, detail, input, composing, opening, sending, stopping, reconnectText,
    errorBanner, notice, currentId, run, active, items, todos,
    formatError, showError, refreshSessions, openSession, newSession, removeCurrent,
    send, stop, init,
  }
}
