/**
 * 账本助手的会话与回合状态。
 *
 * 完整页面和浮窗共用列表、当前会话和消息订阅。
 */
import { computed, ref, shallowRef, watch } from 'vue'
import type { JSONContent } from '@tiptap/core'
import { readDraft, textDraft, withoutSkill } from '@/utils/agentDraft'
import {
  AgentCoreError, createSession, deleteSession, getSession, listSessions, listSkills,
  renameSession, startTurn, stopTurn, subscribeTurn,
  type AgentChatItem, type AgentEvent, type AgentRun, type AgentSession,
  type AgentSessionDetail, type AgentSkill, type AgentTodo,
} from '@/api/agentCore'
import { agentLedgerRevision } from '@/utils/agentPageState'
import { useLibraries } from '@/composables/useLibraries'

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

const { revision: libraryRevision, mutating: libraryChanging } = useLibraries()

function createSessionState() {
  const sessions = ref<AgentSession[]>([])
  const detail = ref<AgentSessionDetail | null>(null)
  /** 编辑器草稿：纯文本 + 可选的行内 Skill 节点。 */
  const draft = shallowRef<JSONContent>(textDraft())
  const skills = ref<AgentSkill[]>([])
  const skillStatus = ref<'loading' | 'ready' | 'error'>('loading')
  const opening = ref(true)
  const sending = ref(false)
  const stopping = ref(false)
  const reconnectText = ref('')
  const errorBanner = ref<{ code: string; message: string } | null>(null)
  const notice = ref('')
  const toolResult = shallowRef<{ name: string; result: Record<string, unknown> }>()

  let controller: AbortController | null = null
  let started = false
  let libraryVersion = 0
  let listVersion = 0
  /** 分会话保存的草稿；只存正文，不带 Skill 选择。 */
  const drafts = new Map<string, JSONContent>()

  const currentId = computed(() => detail.value?.session.id ?? null)
  const run = computed(() => detail.value?.run ?? null)
  const active = computed(() => run.value !== null && run.value.end_id === null)
  const items = computed(() => detail.value?.messages ?? [])
  const todos = computed(() => detail.value?.todos ?? [])
  const sessionBusy = computed(() => opening.value || sending.value || libraryChanging.value)
  const backgroundRunning = computed(() => sessions.value.some(row => row.id !== currentId.value && row.active_run_id))

  function isSessionRunning(id: string) {
    return (id === currentId.value && (sending.value || active.value))
      || !!sessions.value.find(row => row.id === id)?.active_run_id
  }

  function showError(error: unknown, fallback: string) {
    errorBanner.value = { code: error instanceof AgentCoreError ? error.code : fallback, message: formatError(error) }
  }

  async function refreshSessions() {
    const version = ++listVersion
    const next = await listSessions()
    if (version !== listVersion) return
    const stillRunning = new Set(next.sessions.map(row => row.active_run_id))
    if (sessions.value.some(row => row.id !== currentId.value && row.active_run_id
        && !stillRunning.has(row.active_run_id))) notifyRunEnd()
    sessions.value = next.sessions
  }

  function updateSession(session: AgentSession) {
    listVersion += 1
    const index = sessions.value.findIndex(row => row.id === session.id)
    if (index === -1) sessions.value.unshift(session)
    else sessions.value[index] = { ...sessions.value[index], ...session }
  }

  function notifyRunEnd() {
    agentLedgerRevision.value += 1
  }

  // 保持后台会话的运行标识和账本刷新。
  watch(backgroundRunning, (running, _, onCleanup) => {
    if (!running) return
    let stopped = false
    let timer: number
    const schedule = () => {
      if (stopped) return
      timer = window.setTimeout(async () => {
        if (!libraryChanging.value) await refreshSessions().catch(() => undefined)
        schedule()
      }, 1500)
    }
    schedule()
    onCleanup(() => {
      stopped = true
      window.clearTimeout(timer)
    })
  })

  async function refreshSkills() {
    skillStatus.value = 'loading'
    try {
      const next = await listSkills()
      skills.value = next.skills
      skillStatus.value = 'ready'
    } catch {
      skillStatus.value = 'error'
    }
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
    const before = currentId.value
    if (before !== next.session.id) {
      // 切走时只带走正文，Skill 选择不跨会话。
      if (before) drafts.set(before, withoutSkill(draft.value))
      draft.value = drafts.get(next.session.id) ?? textDraft()
    }
    detail.value = next
    updateSession({ ...next.session, active_run_id: next.run?.end_id === null ? next.run.id : null })
    localStorage.setItem('agentcore.session', next.session.id)
    if (next.run?.end_id === null) follow(next.run)
  }

  async function loadSession(id: string) {
    const version = libraryVersion
    const previousTitle = sessions.value.find(row => row.id === id)?.title
    const next = await getSession(id)
    if (version !== libraryVersion) return
    const title = sessions.value.find(row => row.id === id)?.title
    if (title !== undefined && title !== previousTitle) next.session.title = title
    openSnapshot(next)
  }

  async function openSession(id: string) {
    if (sessionBusy.value || currentId.value === id) return
    const version = libraryVersion
    opening.value = true
    errorBanner.value = null
    try {
      await loadSession(id)
    } catch (error) {
      if (version === libraryVersion) showError(error, 'LOAD_FAILED')
    } finally {
      if (version === libraryVersion) opening.value = false
    }
  }

  async function createAndOpenSession() {
    const version = libraryVersion
    const next = await createSession('ATLAS')
    if (version !== libraryVersion) return
    updateSession(next)
    await loadSession(next.id)
  }

  async function newSession() {
    if (sessionBusy.value) return
    const version = libraryVersion
    opening.value = true
    errorBanner.value = null
    try {
      await createAndOpenSession()
    } catch (error) {
      if (version === libraryVersion) showError(error, 'SESSION_FAILED')
    } finally {
      if (version === libraryVersion) opening.value = false
    }
  }

  async function rename(id: string, title: string) {
    if (libraryChanging.value) return false
    const version = libraryVersion
    errorBanner.value = null
    try {
      const updated = await renameSession(id, title)
      if (version !== libraryVersion) return false
      updateSession(updated)
      if (detail.value?.session.id === id) detail.value.session.title = updated.title
      return true
    } catch (error) {
      if (version === libraryVersion) showError(error, 'RENAME_FAILED')
      return false
    }
  }

  async function removeCurrent() {
    const id = currentId.value
    if (!id || isSessionRunning(id) || sessionBusy.value) return
    if (!window.confirm('删除这个会话？聊天记录一起删除，未处理的旧卡会作废。已经提交的业务操作不会撤销。')) return
    const version = libraryVersion
    opening.value = true
    errorBanner.value = null
    try {
      await deleteSession(id)
      if (version !== libraryVersion) return
      listVersion += 1
      sessions.value = sessions.value.filter(row => row.id !== id)
      drafts.delete(id)
      disconnect()
      draft.value = textDraft()
      detail.value = null
      localStorage.removeItem('agentcore.session')
      await refreshSessions()
      if (version !== libraryVersion) return
      if (sessions.value.length) await loadSession(sessions.value[0].id)
      else await createAndOpenSession()
      if (version === libraryVersion) notice.value = '会话已删除。'
    } catch (error) {
      if (version === libraryVersion) {
        if (error instanceof AgentCoreError && error.code === 'RUN_ACTIVE') {
          await loadSession(id).catch(() => undefined)
        }
        if (version === libraryVersion) showError(error, 'DELETE_FAILED')
      }
    } finally {
      if (version === libraryVersion) opening.value = false
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
        updateSession({ id: view.session.id, title: view.session.title, active_run_id: null })
        notifyRunEnd()
        void refreshSessions().catch(() => undefined)
      } else if (item.type === 'tool' && !item.is_error && item.result && typeof item.result === 'object') {
        const result = item.result as Record<string, unknown>
        if (item.name === 'todo_write' && Array.isArray(result.todos)) view.todos = result.todos as AgentTodo[]
        toolResult.value = { name: item.name, result }
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
      } finally {
        if (controller === local) controller = null
      }
    })()
  }

  async function send(pageContext: () => Record<string, unknown>) {
    const view = detail.value
    const submitted = draft.value
    const { text: body, skillId } = readDraft(submitted)
    const text = body.trim()
    if (!view || !text || active.value || sessionBusy.value) return
    const version = libraryVersion
    sending.value = true
    errorBanner.value = null
    notice.value = ''
    try {
      const nextRun = await startTurn(view.session.id, text, pageContext(), skillId)
      if (detail.value !== view) return
      if (draft.value === submitted) draft.value = textDraft()
      view.run = nextRun
      view.cursor = nextRun.start_id - 1
      view.status = '运行已启动，正在接收步骤…'
      view.step = 0
      view.totals = null
      updateSession({ id: view.session.id, title: view.session.title, active_run_id: nextRun.id })
      follow(nextRun)
    } catch (error) {
      // POST 可能已成功但回复丢失；只读回最新运行，绝不自动重发一个可能已经写账的请求。
      if (detail.value === view) {
        const next = await getSession(view.session.id).catch(() => null)
        const recovered = next?.run
        if (detail.value === view && next && recovered && recovered.id !== view.run?.id) {
          if (draft.value === submitted && next.messages.some(item => item.type === 'user'
            && item.id >= recovered.start_id && (recovered.end_id === null || item.id <= recovered.end_id)
            && item.text === text && item.skill?.id === skillId)) draft.value = textDraft()
          openSnapshot(next)
          if (recovered.end_id !== null) notifyRunEnd()
          notice.value = '已从会话记录接回最新一轮，没有重发消息。'
        } else if (detail.value === view) {
          showError(error, 'TURN_FAILED')
        }
      }
    } finally {
      if (version === libraryVersion) sending.value = false
    }
  }

  async function stop() {
    const nextRun = run.value
    if (!nextRun || nextRun.state !== 'running' || stopping.value || libraryChanging.value) return
    const version = libraryVersion
    stopping.value = true
    try {
      const stopped = await stopTurn(nextRun.session_id, nextRun.id)
      if (run.value?.id === stopped.id && active.value && stopped.state === 'stopping') {
        run.value.state = 'stopping'
      }
    } catch (error) {
      if (run.value?.id === nextRun.id) showError(error, 'STOP_FAILED')
    } finally {
      if (version === libraryVersion) stopping.value = false
    }
  }

  /** 打开页面或浮窗时接上最近会话；只执行一次。 */
  async function init() {
    if (started) return
    const version = libraryVersion
    started = true
    opening.value = true
    void refreshSkills()
    try {
      await refreshSessions()
      if (version !== libraryVersion) return
      const stored = localStorage.getItem('agentcore.session')
      const pick = sessions.value.find(candidate => candidate.id === stored) ?? sessions.value[0]
      if (pick) await loadSession(pick.id)
      else await createAndOpenSession()
    } catch (error) {
      if (version === libraryVersion) {
        started = false
        showError(error, 'LOAD_FAILED')
      }
    } finally {
      if (version === libraryVersion) opening.value = false
    }
  }

  watch(libraryRevision, () => {
    libraryVersion += 1
    listVersion += 1
    started = false
    disconnect()
    sessions.value = []
    detail.value = null
    drafts.clear()
    draft.value = textDraft()
    opening.value = true
    sending.value = false
    stopping.value = false
    errorBanner.value = null
    notice.value = ''
  }, { flush: 'sync' })

  return {
    sessions, detail, draft, skills, skillStatus, refreshSkills,
    opening, sending, stopping, reconnectText, toolResult, sessionBusy,
    errorBanner, notice, currentId, run, active, items, todos,
    formatError, showError, openSession, newSession, rename, removeCurrent, isSessionRunning,
    send, stop, init,
  }
}

const shared = createSessionState()

export function useAgentSession(options: AgentSessionOptions) {
  let opened = false
  const init = () => {
    opened = true
    return shared.init()
  }
  watch(libraryRevision, () => { if (opened) void init() })
  if (options.onToolResult) {
    watch(shared.toolResult, item => {
      if (item) options.onToolResult?.(item.name, item.result)
    }, { flush: 'sync' })
  }
  if (options.onRunEnd) watch(agentLedgerRevision, options.onRunEnd)
  return {
    ...shared,
    init,
    send: () => {
      if (!options.canSend || options.canSend()) return shared.send(options.pageContext)
    },
  }
}
