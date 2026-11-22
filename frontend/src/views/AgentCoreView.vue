<script setup lang="ts">
/** 页面只订阅已落盘的步骤；离开页面不停止，停止也不回滚已提交的操作。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AgentCoreChat from '@/components/AgentCoreChat.vue'
import AtlasIcon from '@/components/AtlasIcon.vue'
import { useAgentSession } from '@/composables/useAgentSession'
import { getAISettings, type AISettings } from '@/api/ai'
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
const importBusy = computed(() => uploading.value || restoringStage.value || preparing.value)
const isZip = computed(() => stagedFile.value?.name.toLowerCase().endsWith('.zip') ?? false)
const uploadError = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const deciding = ref<string | null>(null)
const externalActions = ref<AgentAction[]>([])
const actionError = ref('')
const testMode = import.meta.env.VITE_AGENTCORE_TEST_MODE === '1'
const stageKey = 'agentcore.stage'
let gone = false
let stageToken = 0

// ── AI 模型状态：未配置时给设置入口；已配置时提示可以更换 ────────────────────
const aiSettings = ref<AISettings | null>(null)
const aiSettingsLoading = ref(true)
const aiSettingsError = ref('')

async function loadAISettings() {
  aiSettingsLoading.value = true
  aiSettingsError.value = ''
  try {
    aiSettings.value = await getAISettings()
  } catch (error) {
    aiSettingsError.value = error instanceof Error ? error.message : '暂时无法读取 AI 设置'
  } finally {
    aiSettingsLoading.value = false
  }
}

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
  canSend: () => !importBusy.value,
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
const scopeRange = computed(() => scope.value.date_from || scope.value.date_to
  ? `${scope.value.date_from || '不限'} 至 ${scope.value.date_to || '不限'}` : '')
const scopeLabel = computed(() => [
  scopeRange.value,
  scope.value.selection.length ? `已选 ${scope.value.selection.length} 项` : '',
].filter(Boolean).join(' · '))

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
  password.value = ''
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
  void loadAISettings()
  void init()
})
onBeforeUnmount(() => {
  gone = true
})
</script>

<template>
  <div class="ac-shell atlas-theme">
    <!-- 左：会话 -->
    <aside class="ac-col ac-sessions" aria-label="会话列表">
      <div class="ac-panel-head">
        <h2>会话 <span>{{ sessions.length }}</span></h2>
        <button class="mini mini--primary ac-new" data-testid="ac-new-session" :disabled="opening || sending" @click="newSession">
          <AtlasIcon name="plus" /> 新对话
        </button>
      </div>
      <ul v-if="sessions.length" class="ac-session-list">
        <li v-for="session in sessions" :key="session.id">
          <button :class="['ac-session', { 'ac-session--on': session.id === currentId }]"
                  :aria-current="session.id === currentId ? 'true' : undefined" :title="session.title || '未命名对话'"
                  :disabled="opening || sending" @click="openSession(session.id)">
            <AtlasIcon name="chat" />
            <span>
              <b>{{ session.title || '未命名对话' }}</b>
              <small v-if="session.updated_at">{{ session.updated_at.slice(0, 16) }}</small>
            </span>
          </button>
        </li>
      </ul>
      <p v-else class="ac-session-empty">{{ opening ? '正在加载会话…' : '还没有会话' }}</p>
      <div class="ac-session-foot">
        <button v-if="currentId" class="mini mini--danger" :disabled="active || opening || sending"
                @click="removeCurrent"><AtlasIcon name="remove" /> 删除当前会话</button>
      </div>
    </aside>

    <!-- 中：对话 -->
    <AgentCoreChat class="ac-col ac-main-chat" :key="currentId ?? 'opening'"
                   v-model:input="input" :items="items" :run="run" :active="active"
                   :status="detail?.status ?? ''" :step="detail?.step ?? 0" :totals="detail?.totals ?? null"
                   :ready="!!currentId" :send-blocked="importBusy" :sending="sending" :stopping="stopping" :opening="opening"
                   :reconnect-text="reconnectText" :error-banner="errorBanner" :notice="notice"
                   :test-mode="testMode" :title="detail?.session.title || 'ATLAS'" :context-label="scopeLabel"
                   subtitle="明确要求执行时，ATLAS 会直接写入账本；结果以回执为准。"
                   @send="send" @stop="stop" />

    <!-- 右：上下文、任务、导入与旧卡 -->
    <aside class="ac-side" aria-label="账本工作区">
      <div class="ac-work-head"><AtlasIcon name="ledger" /> <h2>账本工作区</h2></div>
      <section class="ac-panel ac-model" data-testid="ac-ai-model">
        <h3><AtlasIcon name="atlas" /> AI 模型</h3>
        <p v-if="aiSettingsLoading" class="muted">正在读取 AI 设置…</p>
        <template v-else-if="aiSettings?.configured">
          <p class="ac-model-name"><b>{{ aiSettings.provider_label }}</b> · {{ aiSettings.model }}</p>
          <p class="muted">
            {{ aiSettings.ai_ready ? '已就绪，可以处理账本任务。' : '模型已保存，但还没有授权发送交易字段。' }}
          </p>
          <RouterLink to="/ai/settings" class="ac-model-link">
            更改 AI 模型 <AtlasIcon name="arrow" />
          </RouterLink>
        </template>
        <template v-else-if="aiSettings">
          <p class="muted">还没有设置 AI 模型。设置后 ATLAS 才能按你的指令处理账本。</p>
          <RouterLink to="/ai/settings" class="ac-model-link ac-model-link--primary">
            去设置 AI 模型 <AtlasIcon name="arrow" />
          </RouterLink>
        </template>
        <template v-else>
          <p class="muted">{{ aiSettingsError || '暂时无法读取 AI 设置。' }}</p>
          <RouterLink to="/ai/settings" class="ac-model-link">打开 AI 设置 <AtlasIcon name="arrow" /></RouterLink>
        </template>
      </section>
      <section v-if="scopeLabel" class="ac-panel ac-scope">
        <h3><AtlasIcon name="ledger" /> V2 页面上下文</h3>
        <p v-if="scopeRange" class="ac-scope-range">{{ scopeRange }}</p>
        <p v-if="scope.selection.length" class="muted">已选 {{ scope.selection.length }} 项</p>
      </section>
      <section v-if="todos.length" class="ac-panel" data-testid="ac-todos">
        <h3><AtlasIcon name="list" /> 任务清单 <span class="ac-count">{{ todos.filter(todo => todo.status === 'completed').length }}/{{ todos.length }}</span></h3>
        <ul class="ac-todos">
          <li v-for="(todo, index) in todos" :key="index">
            <span>{{ todo.content }}</span>
            <span class="ac-tag" :class="`ac-tag--${todo.status}`">{{ TODO_LABELS[todo.status] }}</span>
          </li>
        </ul>
      </section>
      <section class="ac-panel ac-import">
        <h3><AtlasIcon name="file" /> 导入账单</h3>
        <p class="muted">先解析文件，再在对话里要求导入。流水入库后，仍需在 V2 导入账本才会进入账本树。</p>
        <label class="ac-file">
          <input ref="fileInput" type="file" accept=".zip,.csv,.xlsx" :disabled="importBusy"
                 aria-label="选择账单文件" data-testid="ac-file" @change="pickFile">
          <span class="ac-file-icon"><AtlasIcon name="file" /></span>
          <span class="ac-file-text">
            <b :title="stagedFile?.name">{{ stagedFile?.name || '选择账单文件' }}</b>
            <small>CSV / XLSX / ZIP</small>
          </span>
        </label>
        <label v-if="isZip" class="ac-password-label">
          ZIP 解压密码（如有）
          <input v-model="password" type="password" class="ac-password" data-testid="ac-password"
                 autocomplete="off" :disabled="importBusy" placeholder="仅随本次上传提交">
          <small>密码不进入对话、提示词或数据库。</small>
        </label>
        <button class="mini ac-upload" :disabled="!stagedFile || importBusy"
                data-testid="ac-upload" @click="doUpload">
          {{ uploading ? '解析中…' : '上传并解析' }}<AtlasIcon name="arrow" />
        </button>
        <p v-if="restoringStage" class="ac-hint" role="status">正在核对附件状态…</p>
        <p v-if="uploadError" class="ac-form-error" role="alert">{{ uploadError }}</p>

        <div v-if="staged" class="ac-stage" data-testid="ac-stage">
          <span class="ac-tag">已解析 · 未入库</span>
          <b>{{ staged.platform_name }} · {{ staged.total }} 笔</b>
          <p>{{ staged.date_from }} 至 {{ staged.date_to }}</p>
          <p v-if="staged.failed" class="ac-form-error">解析失败 {{ staged.failed }} 行</p>
          <p v-for="warning in staged.warnings" :key="warning" class="muted">· {{ warning }}</p>
          <p class="ac-hint">{{ staged.note }} 可以在对话里要求导入，也可以手工处理。</p>
          <button class="mini" :disabled="importBusy || opening"
                  data-testid="ac-make-import" @click="makeImportCard">生成手工导入卡</button>
        </div>
        <div v-else-if="imported" class="ac-stage ac-stage--done" data-testid="ac-import-receipt">
          <span class="ac-tag ac-tag--ok">已入库</span>
          <b>原始流水导入回执</b>
          <p>新增 {{ imported.inserted }} · 跳过 {{ imported.skipped }} · 失败 {{ imported.failed }}</p>
          <p class="ac-hint">批次 {{ imported.batch_id }}。V2 是否已导入账本，以账本回执为准。</p>
        </div>
      </section>

      <section v-if="pendingOwn.length" class="ac-panel" data-testid="ac-pending">
        <h3><AtlasIcon name="history" /> 历史待确认 · 本会话 <span class="ac-count">{{ pendingOwn.length }}</span></h3>
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
        <h3><AtlasIcon name="history" /> 未认领的历史记录 <span class="ac-count">{{ pendingExternal.length }}</span></h3>
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
        <h3><AtlasIcon name="history" /> 最近处理</h3>
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

<style scoped src="./AgentCoreView.css"></style>
