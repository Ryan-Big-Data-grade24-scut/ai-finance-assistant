<script setup lang="ts">
/** 页面只订阅已落盘的步骤；离开页面不停止，停止也不回滚已提交的操作。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AgentCoreChat from '@/components/AgentCoreChat.vue'
import { useAgentSession } from '@/composables/useAgentSession'
import { buildAgentPageContext, readAgentSelection } from '@/utils/agentPageContext'
import { TODO_LABELS, kindLabel, stateLabel } from '@/utils/agentLabels'
import {
  AgentCoreError, confirmAction, getSession, getStage, listActions, prepareImport,
  rejectAction, uploadBill,
  type AgentAction, type AgentImportCommitted, type AgentStage,
} from '@/api/agentCore'

const route = useRoute()

// ── 页面上下文与本地上传 ──────────────────────────────────────────────────────

const scope = ref<{
  date_from?: string; date_to?: string; selection: string[]
}>({ selection: [] })
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
const externalActions = ref<AgentAction[]>([])
const actionError = ref('')
const testMode = import.meta.env.VITE_AGENTCORE_TEST_MODE === '1'
const stageKey = 'agentcore.stage'
let gone = false
let stageToken = 0

function pageContext(): Record<string, unknown> {
  const stage = staged.value
  return buildAgentPageContext({
    route: route.path,
    surface: 'page',
    dateFrom: scope.value.date_from,
    dateTo: scope.value.date_to,
    selection: scope.value.selection,
    stagedImport: stage ? {
      stage_id: stage.stage_id, platform_name: stage.platform_name, total: stage.total,
      date_from: stage.date_from, date_to: stage.date_to,
    } : null,
  })
}

const {
  sessions, detail, input, opening, sending, stopping, reconnectText, errorBanner, notice,
  currentId, run, active, items, todos, formatError, showError, refreshSessions,
  openSession, newSession, removeCurrent, send, stop, init,
} = useAgentSession({
  pageContext,
  canSend: () => !uploading.value && !restoringStage.value && !preparing.value,
  onToolResult: (name, result) => {
    if (name === 'finance_import_commit' && result.state === 'committed'
        && result.stage_id === attachment.value?.stage_id) {
      attachment.value = result as unknown as AgentImportCommitted
    }
  },
  onRunEnd: async () => {
    await refreshActions().catch(() => undefined)
    await restoreStage()
  },
})

const actions = computed(() => detail.value?.actions ?? [])
const pendingOwn = computed(() => actions.value.filter(action => action.state === 'pending'))
const pendingExternal = computed(() => externalActions.value)
const recentActions = computed(() => actions.value.filter(action => action.state !== 'pending').slice(0, 6))
const hasScope = computed(() =>
  Boolean(scope.value.date_from || scope.value.date_to || scope.value.selection.length))

const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)

// ── 旧卡与本地上传 ────────────────────────────────────────────────────────────

async function refreshExternal() {
  try {
    const next = await listActions({ state: 'pending', unclaimed: true })
    if (!gone) externalActions.value = next.actions
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
    if (gone) return
    if (detail.value === view) {
      notice.value = approve
        ? (action.kind === 'import' ? '导入已执行，回执见卡片。' : '旧卡已确认，V2 操作已执行。')
        : '已拒绝，旧卡没有执行。已另行提交的操作不会撤销。'
    }
    if (action.kind === 'import') await restoreStage()
  } catch (error) {
    if (!gone && detail.value === view) actionError.value = formatError(error)
  } finally {
    deciding.value = null
    if (!gone) await refreshActions().catch(() => undefined)
  }
}

async function restoreStage() {
  if (gone || uploading.value) return
  const id = localStorage.getItem(stageKey)
  if (!id) return
  const token = ++stageToken
  const current = () => !gone && token === stageToken && localStorage.getItem(stageKey) === id
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
    if (gone) return
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
    if (gone) return
    if (detail.value === view) notice.value = '手工导入卡已生成，卡片确认前不执行。'
    await refreshActions()
  } catch (error) {
    if (!gone && detail.value === view) uploadError.value = formatError(error)
  } finally {
    preparing.value = false
  }
}

onMounted(() => {
  const query = route.query
  scope.value = {
    date_from: typeof query.date_from === 'string' ? query.date_from : undefined,
    date_to: typeof query.date_to === 'string' ? query.date_to : undefined,
    selection: readAgentSelection(query),
  }
  void restoreStage()
  void refreshExternal()
  void init()
})
onBeforeUnmount(() => {
  gone = true
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
    <AgentCoreChat class="ac-col" v-model:input="input" :items="items" :run="run" :active="active"
                   :status="detail?.status ?? ''" :step="detail?.step ?? 0" :totals="detail?.totals ?? null"
                   :sending="sending" :stopping="stopping" :opening="opening"
                   :reconnect-text="reconnectText" :error-banner="errorBanner" :notice="notice"
                   :test-mode="testMode" :title="detail?.session.title || 'ATLAS'"
                   subtitle="ATLAS 可查流水、导入账单和修改 V2。明确要求执行时会直接写入，结果以回执为准。"
                   @send="send" @stop="stop" />

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
        <h3>历史待确认记录 · 本会话 <span class="ac-count">{{ pendingOwn.length }}</span></h3>
        <p class="muted">来自旧版确认卡；它们不是已执行的结果，不会自动执行，也不阻塞新任务。</p>
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
        <h3>未认领的历史记录 <span class="ac-count">{{ pendingExternal.length }}</span></h3>
        <p class="muted">这些旧卡没有会话归属，可以在此手工处理；不是已执行的结果。</p>
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

/* 会话 */
.ac-session-list { list-style: none; overflow-y: auto; flex: 1; padding: var(--space-2); }
.ac-session {
  display: block; width: 100%; text-align: left; padding: 8px 10px;
  border: 0; border-radius: var(--radius); background: transparent; cursor: pointer;
}
.ac-session b {
  display: block; font-size: var(--font-xs);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ac-session small { color: var(--color-muted); font-size: 10px; }
.ac-session:hover { background: #f3f6fa; }
.ac-session--on { background: #edf3fa; }
.ac-scope {
  margin: var(--space-2); padding: var(--space-2);
  border: 1px dashed #c8d6e4; border-radius: var(--radius);
  font-size: var(--font-xs); color: #45607a; line-height: 1.6;
}

/* 侧栏里旧卡回执的代码块（对话区的样式在 AgentCoreChat 内）。 */
.ac-card pre {
  margin-top: 6px; padding: 8px; border-radius: var(--radius); background: #172a3b; color: #eaf2fa;
  overflow: auto; max-height: 220px; font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
}

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
