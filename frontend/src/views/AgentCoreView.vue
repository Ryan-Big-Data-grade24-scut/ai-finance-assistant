<script setup lang="ts">
/** 页面只订阅已落盘的步骤；离开页面不停止，停止也不回滚已提交的操作。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { renderMarkdown } from '@/utils/markdown'
import {
  AgentCoreError, confirmAction, createSession, deleteSession, getSession, getStage,
  listActions, listSessions, prepareImport, rejectAction, startTurn, stopTurn, subscribeTurn, uploadBill,
  type AgentAction, type AgentChatItem, type AgentEvent, type AgentImportCommitted, type AgentRun,
  type AgentSession, type AgentSessionDetail, type AgentStage, type AgentTodo,
} from '@/api/agentCore'

const route = useRoute()
const sessions = ref<AgentSession[]>([])
const detail = ref<AgentSessionDetail | null>(null)
const externalActions = ref<AgentAction[]>([])
const currentId = computed(() => detail.value?.session.id ?? null)
const run = computed(() => detail.value?.run ?? null)
const active = computed(() => run.value !== null && run.value.end_id === null)
const items = computed(() => detail.value?.messages ?? [])
const todos = computed(() => detail.value?.todos ?? [])

const input = ref('')
const composing = ref(false)
const opening = ref(true)
const sending = ref(false)
const stopping = ref(false)
const reconnectText = ref('')
const testMode = import.meta.env.VITE_AGENTCORE_TEST_MODE === '1'
const errorBanner = ref<{ code: string; message: string } | null>(null)
const notice = ref('')
const actionError = ref('')
const scrollArea = ref<HTMLElement | null>(null)
let controller: AbortController | null = null
let stageToken = 0
let disposed = false

const scope = ref<{ date_from?: string; date_to?: string; selection: string[] }>({ selection: [] })
const attachment = ref<AgentStage | null>(null)
const staged = computed(() => attachment.value?.state === 'staged' ? attachment.value : null)
const imported = computed(() => attachment.value?.state === 'committed' ? attachment.value.receipt : null)
const stagedFile = ref<File | null>(null)
const password = ref('')
const uploading = ref(false)
const restoringStage = ref(false)
const preparing = ref(false)
const uploadError = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const deciding = ref<string | null>(null)

const actions = computed(() => detail.value?.actions ?? [])
const pendingOwn = computed(() => actions.value.filter(action => action.state === 'pending'))
const pendingExternal = computed(() => externalActions.value)
const recentActions = computed(() => actions.value.filter(action => action.state !== 'pending').slice(0, 6))
const hasScope = computed(() => !!(scope.value.date_from || scope.value.date_to || scope.value.selection.length))

const KIND_LABELS: Record<string, string> = {
  init: '初始化', uninstall: '卸载', move: '移动', create_node: '新建节点', join: '加入净额组',
  create_group: '新建净额组', dissolve: '解散', dissolve_group: '解散净额组',
  split: '分裂', unsplit: '取消分裂', update: '改名/备注', undo: '撤销', redo: '重做', import: '导入账单',
}
const kindLabel = (kind: string) => KIND_LABELS[kind] ?? kind
const STATE_LABELS: Record<string, string> = {
  pending: '待确认', confirmed: '已确认', rejected: '已拒绝', expired: '已失效',
  running: '运行中', stopping: '正在停止', completed: '本轮已结束', failed: '本轮失败',
  stopped: '已停止', interrupted: '服务中断',
}
const stateLabel = (state: string) => STATE_LABELS[state] ?? state
const TODO_LABELS = { pending: '待做', in_progress: '进行中', completed: '已完成' }
const stageKey = 'agentcore.stage'

const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
function callArgs(args: string) {
  try { return pretty(JSON.parse(args)) } catch { return args }
}
function resultLabel(item: Extract<AgentChatItem, { type: 'tool' }>) {
  const result = typeof item.result === 'object' ? item.result : null
  if (item.is_error) {
    const error = result?.error as { code?: string } | undefined
    if (error?.code === 'STOPPED_BEFORE_CALL' || error?.code === 'INTERRUPTED_BEFORE_CALL') return '未派发'
    if (error?.code === 'RECEIPT_UNAVAILABLE' || error?.code === 'RECEIPT_MISSING') return '回执缺失 · 待核对'
    return '失败'
  }
  if (item.name === 'finance_ledger_apply' && result?.state === 'executed') return 'V2 已执行'
  if (item.name === 'finance_import_commit' && result?.state === 'committed') return '已入库'
  return '结果'
}

function formatError(error: unknown): string {
  if (error instanceof AgentCoreError) {
    if (error.code === 'STALE_ACTION') return '账本在预览之后变过，这张卡已失效；请拒绝旧卡并按当前账本操作。'
    if (error.code === 'ACTION_NOT_PENDING') return '这个操作已经被处理过，卡片已刷新。'
  }
  return error instanceof Error ? error.message : '操作失败。'
}
function showError(error: unknown, fallback: string) {
  errorBanner.value = { code: error instanceof AgentCoreError ? error.code : fallback, message: formatError(error) }
}
const needsSettings = computed(() =>
  errorBanner.value?.code === 'NOT_CONFIGURED' || errorBanner.value?.code === 'NOT_AUTHORIZED')

function scrollToBottom() {
  nextTick(() => {
    if (scrollArea.value) scrollArea.value.scrollTop = scrollArea.value.scrollHeight
  })
}

// ── 会话与持久事件 ────────────────────────────────────────────────────────────

async function refreshSessions() {
  const next = await listSessions()
  if (!disposed) sessions.value = next.sessions
}

async function refreshExternal() {
  try {
    const next = await listActions({ state: 'pending', unclaimed: true })
    if (!disposed) externalActions.value = next.actions
  } catch {
    // 旧卡拉取失败不打断运行，也不清除已经看到的卡片。
  }
}

async function refreshActions() {
  const view = detail.value
  await Promise.all([
    refreshSessions(), refreshExternal(),
    view ? getSession(view.session.id).then(next => {
      if (detail.value === view) view.actions = next.actions
    }) : Promise.resolve(),
  ])
}

function disconnect() {
  controller?.abort()
  controller = null
  reconnectText.value = ''
}

function openSnapshot(next: AgentSessionDetail) {
  disconnect()
  actionError.value = ''
  notice.value = ''
  detail.value = next
  localStorage.setItem('agentcore.session', next.session.id)
  if (next.run?.end_id === null) follow(next.run)
  scrollToBottom()
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
  const next = await createSession('账本助手')
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
    } else if (item.type === 'tool' && !item.is_error && item.result && typeof item.result !== 'string') {
      const result = item.result
      if (item.name === 'todo_write' && Array.isArray(result.todos)) view.todos = result.todos as AgentTodo[]
      if (item.name === 'finance_import_commit' && result.state === 'committed'
          && result.stage_id === attachment.value?.stage_id) {
        attachment.value = result as unknown as AgentImportCommitted
      }
    }
  }
  view.cursor = event.cursor
  reconnectText.value = ''
  scrollToBottom()
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
            if (local.signal.aborted) return
            consume(event, nextRun)
            failures = 0
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
      if (!local.signal.aborted) {
        void refreshActions().catch(() => undefined)
        void restoreStage()
      }
    } finally {
      if (controller === local) controller = null
    }
  })()
}

const pagePayload = (): Record<string, unknown> => {
  const { selection, ...dates } = scope.value
  const stage = staged.value
  return {
    route: '/v2/assistant',
    scope: dates,
    selection,
    ...(stage ? { staged_import: {
      stage_id: stage.stage_id, platform_name: stage.platform_name, total: stage.total,
      date_from: stage.date_from, date_to: stage.date_to,
    } } : {}),
  }
}

async function send() {
  const view = detail.value
  const text = input.value.trim()
  if (!view || !text || active.value || sending.value || opening.value || uploading.value
      || restoringStage.value || preparing.value) return
  sending.value = true
  errorBanner.value = null
  notice.value = ''
  try {
    const nextRun = await startTurn(view.session.id, text, pagePayload())
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

// ── 手工卡与本地上传 ──────────────────────────────────────────────────────────

async function decide(action: AgentAction, approve: boolean) {
  if (deciding.value || opening.value) return
  const view = detail.value
  deciding.value = action.id
  actionError.value = ''
  try {
    const sessionId = view?.session.id ?? null
    if (approve) await confirmAction(action.id, sessionId)
    else await rejectAction(action.id, sessionId)
    if (disposed) return
    if (detail.value === view) {
      notice.value = approve
        ? (action.kind === 'import' ? '导入已执行，回执见卡片。' : '旧卡已确认，V2 操作已执行。')
        : '已拒绝，旧卡没有执行。已另行提交的操作不会撤销。'
    }
    if (action.kind === 'import') await restoreStage()
  } catch (error) {
    if (!disposed && detail.value === view) actionError.value = formatError(error)
  } finally {
    deciding.value = null
    if (!disposed) await refreshActions().catch(() => undefined)
  }
}

async function restoreStage() {
  if (disposed || uploading.value) return
  const id = localStorage.getItem(stageKey)
  if (!id) return
  const token = ++stageToken
  const current = () => !disposed && token === stageToken && localStorage.getItem(stageKey) === id
  restoringStage.value = true
  uploadError.value = ''
  try {
    const next = await getStage(id)
    if (!current()) return
    // 一个较早发出的 GET 不能覆盖后来收到的已提交回执。
    if (attachment.value?.state !== 'committed' || next.state === 'committed') attachment.value = next
  } catch (error) {
    if (!current() || attachment.value?.state === 'committed') return
    if (error instanceof AgentCoreError && ['STAGE_EXPIRED', 'STAGE_CLOSED', 'STAGE_NOT_FOUND'].includes(error.code)) {
      localStorage.removeItem(stageKey)
      attachment.value = null
    }
    uploadError.value = formatError(error)
  } finally {
    if (token === stageToken) restoringStage.value = false
  }
}

function pickFile(event: Event) {
  const files = (event.target as HTMLInputElement).files
  stagedFile.value = files?.[0] ?? null
  uploadError.value = ''
}

async function doUpload() {
  if (!stagedFile.value || uploading.value || restoringStage.value || preparing.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    const next = await uploadBill(stagedFile.value, password.value)
    if (disposed) return
    attachment.value = next
    localStorage.setItem(stageKey, next.stage_id)
    stagedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    notice.value = '已解析，尚未入库。明确要求导入后，助手可继续处理。'
  } catch (error) {
    uploadError.value = formatError(error)
  } finally {
    password.value = ''
    uploading.value = false
  }
}

async function makeImportCard() {
  if (!staged.value || preparing.value || uploading.value || restoringStage.value || opening.value) return
  const view = detail.value
  preparing.value = true
  uploadError.value = ''
  try {
    await prepareImport(staged.value.stage_id, view?.session.id ?? null)
    if (disposed) return
    if (detail.value === view) notice.value = '手工导入卡已生成，卡片确认前不执行。'
    await refreshActions()
  } catch (error) {
    if (!disposed && detail.value === view) uploadError.value = formatError(error)
  } finally {
    preparing.value = false
  }
}

onMounted(async () => {
  const query = route.query
  const selection = window.history.state?.agentcoreSelection
  scope.value = {
    date_from: typeof query.date_from === 'string' ? query.date_from : undefined,
    date_to: typeof query.date_to === 'string' ? query.date_to : undefined,
    selection: Array.isArray(selection) ? selection.filter(ref => typeof ref === 'string')
      : typeof query.sel === 'string' ? query.sel.split(',').filter(Boolean) : [],
  }
  void restoreStage()
  void refreshExternal()
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
})
onBeforeUnmount(() => {
  disposed = true
  detail.value = null
  disconnect()
})
</script>

<template>
  <div class="ac-shell">
    <!-- 左：会话 -->
    <aside class="ac-col ac-sessions">
      <div class="ac-panel-head">
        <h2>会话</h2>
        <button class="mini" data-testid="ac-new-session" :disabled="opening || sending" @click="newSession">＋ 新对话</button>
      </div>
      <ul class="ac-session-list">
        <li v-for="session in sessions" :key="session.id">
          <button :class="['ac-session', { 'ac-session--on': session.id === currentId }]"
                  :disabled="opening || sending" @click="openSession(session.id)">
            <b>{{ session.title || '未命名对话' }}</b>
            <small>{{ (session.updated_at ?? '').slice(0, 16) }}</small>
          </button>
        </li>
      </ul>
      <button v-if="currentId" class="mini mini--danger" :disabled="active || opening || sending"
              @click="removeCurrent">删除当前会话</button>

      <div v-if="hasScope" class="ac-scope">
        <b>来自 V2 页面的上下文</b>
        <p v-if="scope.date_from || scope.date_to">
          范围：{{ scope.date_from || '不限' }} ~ {{ scope.date_to || '不限' }}
        </p>
        <p v-if="scope.selection.length">已选 {{ scope.selection.length }} 项</p>
      </div>
    </aside>

    <!-- 中：对话 -->
    <section class="ac-col ac-chat">
      <header class="ac-chat-head">
        <div>
          <h2>{{ detail?.session.title || '账本助手' }}</h2>
          <p class="muted">可查流水、导入账单和修改 V2。明确要求执行时会直接写入，结果以回执为准。</p>
          <p v-if="testMode" class="ac-warning" data-testid="ac-test-mode">测试模式 · 虚构账单 / 脚本化模型</p>
        </div>
        <div v-if="run" class="ac-run" data-testid="ac-run">
          <b>{{ run.model }}</b>
          <span>{{ stateLabel(run.state) }} · 第 {{ detail?.step ?? 0 }} 步</span>
          <small v-if="detail?.totals" class="ac-usage">
            输入 {{ detail.totals.input_tokens ?? 0 }} / 输出 {{ detail.totals.output_tokens ?? 0 }}<br>
            缓存读 {{ detail.totals.cache_read_tokens ?? 0 }} / 写 {{ detail.totals.cache_write_tokens ?? 0 }}
          </small>
        </div>
      </header>

      <div ref="scrollArea" class="ac-messages" data-testid="ac-messages">
        <template v-for="item in items" :key="item.id">
          <article v-if="item.type === 'user'" class="ac-msg ac-msg--user">
            <div class="ac-bubble">{{ item.text }}</div>
          </article>
          <template v-else-if="item.type === 'assistant'">
            <article v-if="item.text" class="ac-msg ac-msg--assistant">
              <div class="ac-bubble md" v-html="renderMarkdown(item.text)"></div>
            </article>
            <details v-for="call in item.calls" :key="call.id" class="ac-tool-result" data-testid="ac-tool-call">
              <summary>第 {{ item.step ?? '—' }} 步 · 调用 {{ call.name }}</summary>
              <pre>{{ callArgs(call.arguments) }}</pre>
            </details>
          </template>
          <details v-else-if="item.type === 'tool'" class="ac-tool-result" data-testid="ac-tool-result"
                   :class="{ 'ac-tool-result--error': item.is_error }">
            <summary>{{ item.name }} · {{ resultLabel(item) }}<template v-if="item.recovered"> · 已恢复回执</template></summary>
            <pre>{{ pretty(item.result) }}</pre>
          </details>
          <p v-else-if="item.type === 'terminal'" class="ac-terminal" :class="{ 'ac-warning': item.state !== 'completed' }"
             :data-state="item.state" data-testid="ac-terminal">
            <b>{{ stateLabel(item.state) }}</b><template v-if="item.message"> · {{ item.message }}</template>
          </p>
          <details v-else class="ac-tool-result">
            <summary>已整理上下文</summary>
            <pre>{{ item.text }}</pre>
          </details>
        </template>

        <p v-if="active" class="ac-status" data-testid="ac-status" role="status">{{ detail?.status || '等待下一步…' }}</p>
        <p v-if="reconnectText" class="ac-warning" role="status">{{ reconnectText }}</p>
      </div>

      <div v-if="errorBanner" class="ac-banner" data-testid="ac-error">
        <span>{{ errorBanner.message }}</span>
        <RouterLink v-if="needsSettings" to="/ai/settings" class="ac-banner-link">去 AI 设置 →</RouterLink>
      </div>
      <p v-if="notice" class="ac-notice">{{ notice }}</p>

      <div class="ac-composer">
        <textarea v-model="input" rows="2" data-testid="ac-input"
                  placeholder="例如：导入右侧账单，把这批新增流水初始化到「旅行」，再核对净额。"
                  @compositionstart="composing = true"
                  @compositionend="composing = false"
                  @keydown.enter.exact.prevent="!composing && send()"></textarea>
        <button v-if="!active" class="ac-send" data-testid="ac-send"
                :disabled="!currentId || !input.trim() || opening || sending || uploading || restoringStage || preparing"
                @click="send">
          {{ sending ? '启动中…' : '发送' }}
        </button>
        <button v-else class="ac-send ac-send--stop" data-testid="ac-stop"
                :disabled="stopping || run?.state === 'stopping'" @click="stop">
          {{ stopping || run?.state === 'stopping' ? '正在停止…' : '停止' }}
        </button>
      </div>
    </section>

    <!-- 右：任务、导入与旧卡 -->
    <aside class="ac-col ac-side">
      <section v-if="todos.length" class="ac-panel" data-testid="ac-todos">
        <h3>任务清单</h3>
        <p class="muted">助手记录的进度；是否写入以回执为准。</p>
        <ul class="ac-todos">
          <li v-for="(todo, index) in todos" :key="index">
            <span class="ac-tag" :class="{ 'ac-tag--ok': todo.status === 'completed' }">{{ TODO_LABELS[todo.status] }}</span>
            <span>{{ todo.content }}</span>
          </li>
        </ul>
      </section>
      <section class="ac-panel">
        <h3>导入账单</h3>
        <p class="muted">
          文件由你选，解压密码只随这次上传提交：不进对话、不进提示词、不入库。
          导入原始流水后仍需在 V2 里初始化，才会进入账本树。
        </p>
        <label class="ac-file">
          <input ref="fileInput" type="file" accept=".zip,.csv,.xlsx" :disabled="uploading || preparing"
                 data-testid="ac-file" @change="pickFile">
        </label>
        <input v-model="password" type="password" class="ac-password" data-testid="ac-password"
               placeholder="支付宝账单 ZIP 的解压密码（仅本地）">
        <button class="mini" :disabled="!stagedFile || uploading || restoringStage || preparing"
                data-testid="ac-upload" @click="doUpload">
          {{ uploading ? '解析中…' : '上传并解析' }}
        </button>
        <p v-if="restoringStage" class="muted" role="status">正在核对附件状态…</p>
        <p v-if="uploadError" class="ac-form-error">{{ uploadError }}</p>

        <div v-if="staged" class="ac-stage" data-testid="ac-stage">
          <b>已解析 · {{ staged.platform_name }}</b>
          <p>{{ staged.date_from }} ~ {{ staged.date_to }} · {{ staged.total }} 笔</p>
          <p v-if="staged.failed" class="ac-form-error">解析失败 {{ staged.failed }} 行</p>
          <p v-for="warning in staged.warnings" :key="warning" class="muted">· {{ warning }}</p>
          <p class="ac-hint">{{ staged.note }} 可以在左侧对话里要求导入，也可以手工处理。</p>
          <button class="mini" :disabled="preparing || uploading || restoringStage || opening"
                  data-testid="ac-make-import" @click="makeImportCard">生成手工导入卡</button>
        </div>
        <div v-else-if="imported" class="ac-stage" data-testid="ac-import-receipt">
          <b>原始流水已入库</b>
          <p>新增 {{ imported.inserted }} · 跳过 {{ imported.skipped }} · 失败 {{ imported.failed }}</p>
          <p class="ac-hint">批次 {{ imported.batch_id }}。V2 是否已初始化，以账本回执为准。</p>
        </div>
      </section>

      <section v-if="pendingOwn.length" class="ac-panel" data-testid="ac-pending">
        <h3>旧待确认卡 · 本会话 <span class="ac-count">{{ pendingOwn.length }}</span></h3>
        <p class="muted">这些卡不会自动执行，也不阻塞新任务。</p>
        <article v-for="action in pendingOwn" :key="action.id" class="ac-card" :data-action-id="action.id">
          <header>
            <b>{{ action.title || kindLabel(action.kind) }}</b>
            <span class="ac-tag">{{ kindLabel(action.kind) }}</span>
          </header>
          <div v-if="action.kind === 'import'" class="ac-preview">
            <p><b>{{ action.preview.platform_name }}</b> · {{ action.preview.date_from }} ~ {{ action.preview.date_to }}</p>
            <p>{{ action.preview.total }} 笔 · 预计重复 {{ action.preview.duplicate_estimate ?? 0 }} 笔会被跳过</p>
            <p v-if="action.preview.amount_sum_expense">
              支出合计 ¥{{ (action.preview.amount_sum_expense ?? 0).toFixed(2) }}
            </p>
          </div>
          <div v-else class="ac-preview">
            <p>
              新建 {{ action.preview.counts?.created ?? 0 }} ·
              移动 {{ action.preview.counts?.moved ?? 0 }} ·
              移除 {{ action.preview.counts?.removed ?? 0 }} ·
              改名 {{ action.preview.counts?.renamed ?? 0 }}
            </p>
            <p v-if="action.preview.created?.length" class="muted">
              新建：{{ action.preview.created.slice(0, 6).map(x => x.name || x.ref).join('、') }}
              <template v-if="action.preview.created.length > 6">…等 {{ action.preview.counts?.created }} 个</template>
            </p>
            <p v-if="action.preview.moved?.length" class="muted">
              移动：{{ action.preview.moved.slice(0, 4).map(x => `${x.ref} ${x.from ?? '?'} → ${x.to ?? '?'}`).join('；') }}
            </p>
            <p v-if="action.preview.note" class="muted">{{ action.preview.note }}</p>
          </div>
          <div class="ac-card-actions">
            <button class="mini mini--primary" data-testid="ac-confirm"
                    :disabled="opening || !!deciding" @click="decide(action, true)">确认</button>
            <button class="mini" data-testid="ac-reject"
                    :disabled="opening || !!deciding" @click="decide(action, false)">拒绝</button>
          </div>
        </article>
      </section>

      <section v-if="pendingExternal.length" class="ac-panel" data-testid="ac-external">
        <h3>未认领的旧卡 <span class="ac-count">{{ pendingExternal.length }}</span></h3>
        <p class="muted">这些旧卡没有会话归属，可以在此手工处理。</p>
        <article v-for="action in pendingExternal" :key="action.id" class="ac-card" :data-action-id="action.id">
          <header>
            <b>{{ action.title || kindLabel(action.kind) }}</b>
            <span class="ac-tag">{{ kindLabel(action.kind) }}</span>
          </header>
          <div v-if="action.kind === 'import'" class="ac-preview">
            <p><b>{{ action.preview.platform_name }}</b> · {{ action.preview.date_from }} ~ {{ action.preview.date_to }}</p>
            <p>{{ action.preview.total }} 笔 · 预计重复 {{ action.preview.duplicate_estimate ?? 0 }} 笔会被跳过</p>
          </div>
          <div v-else class="ac-preview">
            <p>
              新建 {{ action.preview.counts?.created ?? 0 }} ·
              移动 {{ action.preview.counts?.moved ?? 0 }} ·
              移除 {{ action.preview.counts?.removed ?? 0 }} ·
              改名 {{ action.preview.counts?.renamed ?? 0 }}
            </p>
          </div>
          <div class="ac-card-actions">
            <button class="mini mini--primary" :disabled="deciding === action.id"
                    @click="decide(action, true)">确认</button>
            <button class="mini" :disabled="deciding === action.id"
                    @click="decide(action, false)">拒绝</button>
          </div>
        </article>
      </section>

      <p v-if="actionError" class="ac-form-error" data-testid="ac-action-error">{{ actionError }}</p>

      <section v-if="recentActions.length" class="ac-panel">
        <h3>最近处理</h3>
        <article v-for="action in recentActions" :key="action.id" class="ac-card ac-card--done">
          <header>
            <b>{{ action.title || kindLabel(action.kind) }}</b>
            <span :class="['ac-tag', action.state === 'confirmed' ? 'ac-tag--ok' : 'ac-tag--off']">
              {{ stateLabel(action.state) }}
            </span>
          </header>
          <details>
            <summary>回执</summary>
            <pre>{{ pretty(action.result) }}</pre>
          </details>
        </article>
      </section>
    </aside>
  </div>
</template>

<style scoped>
.ac-shell {
  display: grid;
  grid-template-columns: 208px minmax(0, 1fr) 344px;
  gap: var(--space-3);
  height: calc(100vh - 132px);
  min-height: 520px;
}
.ac-col {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.ac-panel-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-border);
}
h2 { font-size: var(--font-md); }
h3 { font-size: var(--font-sm); margin-bottom: var(--space-2); }
.muted { color: var(--color-muted); line-height: 1.6; }

.mini {
  padding: 6px 10px; border: 1px solid var(--color-border); border-radius: var(--radius);
  background: #fff; color: var(--color-text); cursor: pointer; font-size: var(--font-xs);
}
.mini:hover { background: #f3f6fa; }
.mini:disabled { opacity: .5; cursor: default; }
.mini--primary { background: #2f6fae; border-color: #2f6fae; color: #fff; }
.mini--primary:hover { background: #285f95; }
.mini--danger { margin: var(--space-3); color: var(--color-expense); border-color: #f0d4d5; }

/* 会话栏 */
.ac-session-list { list-style: none; overflow-y: auto; flex: 1; padding: var(--space-2); }
.ac-session {
  display: block; width: 100%; text-align: left; padding: 8px 10px; border: 0;
  border-radius: var(--radius); background: transparent; cursor: pointer;
}
.ac-session b { display: block; font-size: var(--font-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ac-session small { color: var(--color-muted); font-size: 10px; }
.ac-session:hover { background: #f3f6fa; }
.ac-session--on { background: #edf3fa; }
.ac-scope {
  margin: var(--space-2); padding: var(--space-2);
  border: 1px dashed #c8d6e4; border-radius: var(--radius);
  font-size: var(--font-xs); color: #45607a; line-height: 1.6;
}

/* 聊天 */
.ac-chat { background: linear-gradient(180deg, #fbfdff, #ffffff); }
.ac-chat-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-2);
  padding: var(--space-3); border-bottom: 1px solid var(--color-border);
}
.ac-chat-head p { margin-top: 2px; font-size: var(--font-xs); }
.ac-run {
  flex: 0 1 42%; min-width: 0; display: grid; gap: 3px; text-align: right;
  color: #45607a; font-size: var(--font-xs); overflow-wrap: anywhere;
}
.ac-usage { color: var(--color-muted); font-size: 10px; }
.ac-messages { flex: 1; min-height: 0; overflow-y: auto; padding: var(--space-3); display: flex; flex-direction: column; gap: 10px; }
.ac-msg { display: flex; }
.ac-msg--user { justify-content: flex-end; }
.ac-bubble {
  max-width: 78%; padding: 9px 12px; border-radius: 13px; line-height: 1.7;
  overflow-wrap: anywhere; font-size: var(--font-sm);
}
.ac-msg--user .ac-bubble { background: #2f6fae; color: #fff; border-bottom-right-radius: 4px; }
.ac-msg--assistant .ac-bubble { background: #f3f7fb; border: 1px solid #e3ebf3; border-bottom-left-radius: 4px; }
.ac-tool-result {
  padding: 7px 10px; border-radius: var(--radius); background: #f7fafc;
  border: 1px solid #e3ebf3; font-size: var(--font-xs); color: #45607a;
}
.ac-terminal {
  padding: 7px 0; border-top: 1px solid var(--color-border);
  font-size: var(--font-xs); color: var(--color-muted);
}
.ac-tool-result summary { cursor: pointer; }
.ac-tool-result pre, .ac-card pre {
  margin-top: 6px; padding: 8px; border-radius: var(--radius); background: #172a3b; color: #eaf2fa;
  overflow: auto; max-height: 220px; font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
}
.ac-tool-result--error summary { color: var(--color-expense); }
.ac-status { color: var(--color-muted); font-size: var(--font-xs); }
.ac-warning { color: #9a6b00; font-size: var(--font-xs); }
.ac-banner {
  margin: 0 var(--space-3); padding: 9px 11px; border-radius: var(--radius);
  background: #fdf3f3; border: 1px solid #f2d6d7; color: #8c2f33; font-size: var(--font-xs);
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
}
.ac-banner-link { color: #2f6fae; font-weight: 600; text-decoration: none; white-space: nowrap; }
.ac-notice { margin: 6px var(--space-3) 0; color: #2f7d57; font-size: var(--font-xs); }
.ac-composer {
  display: flex; gap: var(--space-2); padding: var(--space-3);
  border-top: 1px solid var(--color-border); background: #fff;
}
.ac-composer textarea {
  flex: 1; resize: none; border: 1px solid var(--color-border); border-radius: var(--radius);
  padding: 9px 11px; font-size: var(--font-sm); line-height: 1.5; outline: none;
}
.ac-composer textarea:focus { border-color: #8db4d9; box-shadow: 0 0 0 3px rgba(54, 121, 237, .12); }
.ac-send {
  align-self: flex-end; padding: 10px 16px; border: 0; border-radius: var(--radius);
  background: #2f6fae; color: #fff; cursor: pointer; font-weight: 600;
}
.ac-send:disabled { opacity: .45; cursor: default; }
.ac-send--stop { background: #8c2f33; }

/* 侧栏 */
.ac-side { overflow-y: auto; background: transparent; border: 0; gap: var(--space-3); }
.ac-panel {
  background: var(--color-card); border: 1px solid var(--color-border);
  border-radius: var(--radius-lg); padding: var(--space-3);
}
.ac-panel > .muted { font-size: var(--font-xs); margin-bottom: var(--space-2); }
.ac-todos { display: grid; gap: 6px; list-style: none; font-size: var(--font-xs); }
.ac-todos li { display: flex; gap: var(--space-2); align-items: baseline; overflow-wrap: anywhere; }
.ac-file { display: block; margin-bottom: var(--space-2); font-size: var(--font-xs); }
.ac-password {
  width: 100%; margin-bottom: var(--space-2); padding: 7px 9px; font-size: var(--font-xs);
  border: 1px solid var(--color-border); border-radius: var(--radius);
}
.ac-stage {
  margin-top: var(--space-2); padding: var(--space-2); border-radius: var(--radius);
  background: #f7fafc; border: 1px solid #e3ebf3; font-size: var(--font-xs); line-height: 1.6;
}
.ac-hint { color: var(--color-muted); margin: var(--space-1) 0; }
.ac-form-error { color: var(--color-expense); font-size: var(--font-xs); margin-top: var(--space-1); }
.ac-card {
  border: 1px solid var(--color-border); border-radius: var(--radius);
  padding: var(--space-2); margin-top: var(--space-2); font-size: var(--font-xs);
}
.ac-card header { display: flex; justify-content: space-between; gap: var(--space-2); align-items: baseline; }
.ac-tag { color: #56708a; background: #eef3f8; border-radius: 999px; padding: 1px 7px; font-size: 10px; white-space: nowrap; }
.ac-tag--ok { color: #2f7d57; background: #effaf4; }
.ac-tag--off { color: var(--color-muted); }
.ac-preview { margin: var(--space-1) 0; line-height: 1.6; }
.ac-preview .muted { font-size: 10px; }
.ac-card-actions { display: flex; gap: var(--space-2); margin-top: var(--space-2); }
.ac-card--done { opacity: .92; }
.ac-count {
  display: inline-block; min-width: 18px; text-align: center; padding: 0 5px;
  border-radius: 999px; background: #2f6fae; color: #fff; font-size: 10px;
}

/* Markdown */
.md :deep(p) { margin: 0 0 8px; }
.md :deep(p:last-child) { margin-bottom: 0; }
.md :deep(h1), .md :deep(h2), .md :deep(h3), .md :deep(h4) { margin: 10px 0 6px; line-height: 1.4; }
.md :deep(h2) { font-size: 15px; }
.md :deep(h3), .md :deep(h4) { font-size: 14px; }
.md :deep(ul), .md :deep(ol) { margin: 5px 0 9px; padding-left: 19px; }
.md :deep(li) { margin: 3px 0; }
.md :deep(code) { padding: 1px 4px; border-radius: 5px; background: #e9f0f7; font-family: ui-monospace, Consolas, monospace; font-size: .92em; }
.md :deep(pre) { overflow-x: auto; margin: 8px 0; padding: 10px; border-radius: 9px; background: #172a3b; color: #eaf2fa; }
.md :deep(pre code) { padding: 0; background: transparent; color: inherit; }
.md :deep(table) { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 11px; }
.md :deep(th), .md :deep(td) { padding: 5px 7px; border: 1px solid #e2eaf1; text-align: left; }
.md :deep(th) { background: #f1f6fb; }
.md :deep(a) { color: #2c75b4; }

@media (max-width: 1180px) {
  .ac-shell { grid-template-columns: 180px minmax(0, 1fr) 300px; height: auto; }
  .ac-chat { min-height: 70vh; }
}
@media (max-width: 900px) {
  .ac-shell { grid-template-columns: 1fr; }
  .ac-sessions { max-height: 180px; }
  .ac-side { overflow: visible; }
}
</style>
