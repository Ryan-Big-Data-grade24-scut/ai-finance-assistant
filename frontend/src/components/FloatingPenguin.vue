<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { renderMarkdown } from '@/utils/markdown'
import {
  confirmAIChatAction,
  createAIChatConversation,
  deleteAIChatConversation,
  getAIChatConversation,
  getLatestAIChatAgentTask,
  listAIChatConversations,
  rejectAIChatAction,
  sendAIChatMessage,
  type AIChatAction,
  type AIChatAgentTask,
  type AIChatAgentTaskStep,
  type AIChatAgentEvidence,
  type AIChatConversation,
  type AIChatMessage,
} from '@/api/aiChat'

const route = useRoute()
const isOpen = ref(false)
const isLoading = ref(false)
const isSending = ref(false)
const isActionBusy = ref<string | null>(null)
const isDeletingConversation = ref(false)
const conversations = ref<AIChatConversation[]>([])
const conversationId = ref<string | null>(null)
const messages = ref<AIChatMessage[]>([])
const actions = ref<AIChatAction[]>([])
const agentTask = ref<AIChatAgentTask | null>(null)
const draft = ref('')
const errorMessage = ref('')
const scrollArea = ref<HTMLElement | null>(null)

const PET_WIDTH = 76
const PET_HEIGHT = 88
const PANEL_WIDTH = 410
const PANEL_HEIGHT = 620
const position = ref({ left: 0, top: 0 })
const isDragging = ref(false)
const panelPosition = ref({ left: 0, top: 0 })
const isPanelDragging = ref(false)
const isSpeaking = ref(false)
let dragOrigin = { x: 0, y: 0, left: 0, top: 0 }
let panelDragOrigin = { x: 0, y: 0, left: 0, top: 0 }
let movedDuringDrag = false
let movedPanelDuringDrag = false
let suppressClick = false
let speakingTimer: number | undefined
let panelPositionRestored = false

const conversationOptions = computed(() => conversations.value.slice(0, 30))

interface BulkActionSample {
  tx_id: number
  date: string
  merchant: string
  amount: number
}

function actionSamples(action: AIChatAction): BulkActionSample[] {
  const value = action.payload.sample
  return Array.isArray(value) ? value.slice(0, 5) as BulkActionSample[] : []
}

function remainingActionSampleCount(action: AIChatAction): number {
  const total = Number(action.payload.matched_count || 0)
  return Math.max(0, total - actionSamples(action).length)
}

function clampPosition(left: number, top: number) {
  const maxLeft = Math.max(8, window.innerWidth - PET_WIDTH - 8)
  const maxTop = Math.max(8, window.innerHeight - PET_HEIGHT - 8)
  return {
    left: Math.min(Math.max(8, left), maxLeft),
    top: Math.min(Math.max(8, top), maxTop),
  }
}

function clampPanelPosition(left: number, top: number) {
  const width = Math.min(PANEL_WIDTH, window.innerWidth - 24)
  const height = Math.min(PANEL_HEIGHT, window.innerHeight - 24)
  return {
    left: Math.min(Math.max(12, left), Math.max(12, window.innerWidth - width - 12)),
    top: Math.min(Math.max(12, top), Math.max(12, window.innerHeight - height - 12)),
  }
}

function savePosition() {
  try {
    localStorage.setItem('ai-finance-penguin-position', JSON.stringify(position.value))
  } catch {
    // Storage may be unavailable in private browsing; dragging still works for this session.
  }
}

function restorePosition() {
  let restored: { left?: number; top?: number } | null = null
  try {
    const raw = localStorage.getItem('ai-finance-penguin-position')
    if (raw) restored = JSON.parse(raw)
  } catch {
    restored = null
  }
  if (restored && Number.isFinite(restored.left) && Number.isFinite(restored.top)) {
    position.value = clampPosition(Number(restored.left), Number(restored.top))
  } else {
    position.value = clampPosition(window.innerWidth - PET_WIDTH - 28, window.innerHeight - PET_HEIGHT - 26)
  }
}

function restorePanelPosition() {
  let restored: { left?: number; top?: number } | null = null
  try {
    const raw = localStorage.getItem('ai-finance-penny-chat-position')
    if (raw) restored = JSON.parse(raw)
  } catch {
    restored = null
  }
  if (restored && Number.isFinite(restored.left) && Number.isFinite(restored.top)) {
    panelPosition.value = clampPanelPosition(Number(restored.left), Number(restored.top))
  } else {
    const width = Math.min(PANEL_WIDTH, window.innerWidth - 24)
    const height = Math.min(PANEL_HEIGHT, window.innerHeight - 24)
    panelPosition.value = clampPanelPosition(window.innerWidth - width - 24, window.innerHeight - height - 112)
  }
  panelPositionRestored = true
}

function savePanelPosition() {
  try {
    localStorage.setItem('ai-finance-penny-chat-position', JSON.stringify(panelPosition.value))
  } catch {
    // Chat remains draggable for the current session if browser storage is unavailable.
  }
}

function onPanelHeaderPointerDown(event: PointerEvent) {
  if (event.button !== 0 || (event.target as HTMLElement).closest('button,select,a')) return
  if (!panelPositionRestored) restorePanelPosition()
  isPanelDragging.value = true
  movedPanelDuringDrag = false
  panelDragOrigin = {
    x: event.clientX, y: event.clientY,
    left: panelPosition.value.left, top: panelPosition.value.top,
  }
  window.addEventListener('pointermove', onPanelPointerMove)
  window.addEventListener('pointerup', onPanelPointerUp, { once: true })
  window.addEventListener('pointercancel', onPanelPointerUp, { once: true })
  event.preventDefault()
}

function onPanelPointerMove(event: PointerEvent) {
  if (!isPanelDragging.value) return
  const dx = event.clientX - panelDragOrigin.x
  const dy = event.clientY - panelDragOrigin.y
  if (Math.abs(dx) + Math.abs(dy) > 4) movedPanelDuringDrag = true
  if (movedPanelDuringDrag) {
    panelPosition.value = clampPanelPosition(panelDragOrigin.left + dx, panelDragOrigin.top + dy)
  }
}

function onPanelPointerUp() {
  if (!isPanelDragging.value) return
  isPanelDragging.value = false
  window.removeEventListener('pointermove', onPanelPointerMove)
  window.removeEventListener('pointerup', onPanelPointerUp)
  window.removeEventListener('pointercancel', onPanelPointerUp)
  if (movedPanelDuringDrag) savePanelPosition()
}

function triggerSpeaking() {
  isSpeaking.value = true
  if (speakingTimer !== undefined) window.clearTimeout(speakingTimer)
  speakingTimer = window.setTimeout(() => { isSpeaking.value = false }, 1500)
}

function onPointerDown(event: PointerEvent) {
  if (event.button !== 0) return
  isDragging.value = true
  movedDuringDrag = false
  dragOrigin = {
    x: event.clientX,
    y: event.clientY,
    left: position.value.left,
    top: position.value.top,
  }
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp, { once: true })
  window.addEventListener('pointercancel', onPointerUp, { once: true })
}

function onPointerMove(event: PointerEvent) {
  if (!isDragging.value) return
  const dx = event.clientX - dragOrigin.x
  const dy = event.clientY - dragOrigin.y
  if (Math.abs(dx) + Math.abs(dy) > 5) movedDuringDrag = true
  if (movedDuringDrag) {
    position.value = clampPosition(dragOrigin.left + dx, dragOrigin.top + dy)
  }
}

function onPointerUp() {
  if (!isDragging.value) return
  isDragging.value = false
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerUp)
  if (movedDuringDrag) {
    savePosition()
    suppressClick = true
    window.setTimeout(() => { suppressClick = false }, 180)
  }
}

function onWindowResize() {
  position.value = clampPosition(position.value.left, position.value.top)
  savePosition()
  if (panelPositionRestored) {
    panelPosition.value = clampPanelPosition(panelPosition.value.left, panelPosition.value.top)
    savePanelPosition()
  }
}

async function refreshConversationList() {
  const result = await listAIChatConversations()
  conversations.value = result.conversations
}

async function loadConversation(id: string) {
  isLoading.value = true
  errorMessage.value = ''
  try {
    const result = await getAIChatConversation(id)
    conversationId.value = id
    messages.value = result.messages
    actions.value = result.actions
    agentTask.value = result.agent_task || null
    await nextTick()
    scrollToBottom()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取聊天记录失败。'
  } finally {
    isLoading.value = false
  }
}

async function ensureConversation() {
  if (conversationId.value) return
  await refreshConversationList()
  const latest = conversations.value[0]
  if (latest) {
    await loadConversation(latest.id)
    return
  }
  await createNewConversation()
}

async function openChat() {
  if (suppressClick) return
  isOpen.value = !isOpen.value
  if (isOpen.value) {
    if (!panelPositionRestored) restorePanelPosition()
    await ensureConversation()
  }
}

async function createNewConversation() {
  errorMessage.value = ''
  try {
    const conversation = await createAIChatConversation()
    conversationId.value = conversation.id
    messages.value = []
    actions.value = []
    agentTask.value = null
    await refreshConversationList()
    await nextTick()
    scrollToBottom()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建新对话失败。'
  }
}

async function deleteCurrentConversation() {
  const id = conversationId.value
  if (!id || isDeletingConversation.value || isSending.value || isLoading.value) return

  const active = conversations.value.find(item => item.id === id)
  const title = active?.title || '当前对话'
  const hasPendingActions = actions.value.some(action => action.status === 'pending')
  const extra = hasPendingActions
    ? '\\n这段对话还有待确认的操作提案，删除后提案也会一并删除，但不会执行。'
    : ''
  if (!window.confirm(`确定删除「${title}」吗？删除后聊天记录无法恢复。${extra}`)) return

  isDeletingConversation.value = true
  errorMessage.value = ''
  try {
    await deleteAIChatConversation(id)
    conversationId.value = null
    messages.value = []
    actions.value = []
    agentTask.value = null
    await refreshConversationList()
    const next = conversations.value[0]
    if (next) {
      await loadConversation(next.id)
    } else {
      await createNewConversation()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '删除对话失败。'
  } finally {
    isDeletingConversation.value = false
  }
}

function onConversationSelect(event: Event) {
  const id = (event.target as HTMLSelectElement).value
  if (id) void loadConversation(id)
}

function scrollToBottom() {
  if (scrollArea.value) scrollArea.value.scrollTop = scrollArea.value.scrollHeight
}

function pageContext() {
  const query = route.query
  const routeName = String(route.name || 'unknown')
  const routePath = String(route.path || '')
  const isTransactionsPage = (
    routeName.toLowerCase().includes('transaction')
    || /\/transactions?(?:\/|$)/i.test(routePath)
  )
  return {
    // Normalize route aliases so the backend can reliably recognize the
    // transaction page without depending on one exact Vue route name.
    page: isTransactionsPage ? 'transactions' : routeName,
    path: routePath,
    period: typeof query.value === 'string' ? query.value : '',
    type: typeof query.type === 'string' ? query.type : (isTransactionsPage ? 'expense' : ''),
    tab: typeof query.tab === 'string' ? query.tab : '',
    date: typeof query.date === 'string' ? query.date : '',
    tx_id: typeof query.id === 'string' ? query.id : '',
    category: typeof query.category === 'string' ? query.category : '',
    subcat: typeof query.subcat === 'string' ? query.subcat : '',
    pay_method: typeof query.pay_method === 'string' ? query.pay_method : '',
  }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || isSending.value) return
  await ensureConversation()
  if (!conversationId.value) return

  const activeConversationId = conversationId.value
  const previousAgentTask = agentTask.value
  draft.value = ''
  errorMessage.value = ''
  isSending.value = true
  agentTask.value = null
  messages.value.push({ role: 'user', content })
  await nextTick()
  scrollToBottom()

  let pollHandle: number | undefined
  let polling = false
  let pollActive = true
  const pollTask = async () => {
    if (polling || !pollActive) return
    polling = true
    try {
      const result = await getLatestAIChatAgentTask(activeConversationId).catch(() => null)
      const task = result?.agent_task
      const isNewTaskGeneration = !!task && (
        !previousAgentTask
        || task.id !== previousAgentTask.id
        || task.generation > previousAgentTask.generation
      )
      const isCurrentRunTask = !!task && !!agentTask.value
        && task.id === agentTask.value.id
        && task.generation === agentTask.value.generation
      const currentUpdatedAt = agentTask.value?.updated_at ? Date.parse(agentTask.value.updated_at) : 0
      const nextUpdatedAt = task?.updated_at ? Date.parse(task.updated_at) : 0
      const isAtLeastAsFresh = !agentTask.value || !currentUpdatedAt || !nextUpdatedAt
        || nextUpdatedAt >= currentUpdatedAt
      if (
        pollActive && task && (isNewTaskGeneration || isCurrentRunTask)
        && isAtLeastAsFresh && conversationId.value === activeConversationId
      ) {
        agentTask.value = task
        await nextTick()
        scrollToBottom()
      }
    } finally {
      polling = false
    }
  }

  try {
    const pendingRequest = sendAIChatMessage(activeConversationId, content, pageContext())
    pollHandle = window.setInterval(() => { void pollTask() }, 850)
    await pollTask()
    const result = await pendingRequest
    messages.value = result.messages
    actions.value = result.actions
    agentTask.value = result.agent_task ?? null
    await refreshConversationList()
    triggerSpeaking()
    await nextTick()
    scrollToBottom()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发送消息失败。'
    if (conversationId.value === activeConversationId) {
      const result = await getAIChatConversation(activeConversationId).catch(() => null)
      if (result) {
        messages.value = result.messages
        actions.value = result.actions
        agentTask.value = result.agent_task || null
      }
    }
  } finally {
    pollActive = false
    if (pollHandle !== undefined) window.clearInterval(pollHandle)
    isSending.value = false
  }
}

async function confirmAction(action: AIChatAction) {
  if (!conversationId.value || isActionBusy.value) return
  isActionBusy.value = action.id
  errorMessage.value = ''
  try {
    await confirmAIChatAction(conversationId.value, action.id)
    await loadConversation(conversationId.value)
    await refreshConversationList()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认操作失败。'
  } finally {
    isActionBusy.value = null
  }
}

async function rejectAction(action: AIChatAction) {
  if (!conversationId.value || isActionBusy.value) return
  isActionBusy.value = action.id
  errorMessage.value = ''
  try {
    await rejectAIChatAction(conversationId.value, action.id)
    await loadConversation(conversationId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '拒绝操作失败。'
  } finally {
    isActionBusy.value = null
  }
}

function actionTitle(action: AIChatAction) {
  const payload = action.payload || {}
  if (action.action_type === 'create_category_rule') {
    return `保存规则：${String(payload.pattern || '')} → ${String(payload.target_path || '')}`
  }
  if (action.action_type === 'bulk_reclassify_transactions') {
    return `批量调整 ${String(payload.matched_count || (Array.isArray(payload.tx_ids) ? payload.tx_ids.length : 0))} 笔交易 → ${String(payload.target_path || '')}`
  }
  if (action.action_type === 'create_manual_transaction') {
    return `新增${String(payload.direction || '')} ¥${Number(payload.amount || 0).toFixed(2)} · ${String(payload.counterparty || payload.item || '未填写商户')}`
  }
  return `修改交易 #${String(payload.tx_id || '')}：${String(payload.current_category || '')} → ${String(payload.target_path || '')}`
}

function formatTime(value?: string) {
  if (!value) return ''
  const date = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const agentPhaseLabels: Record<string, string> = {
  understanding: '理解问题', planning: '制定计划', validating: '检查计划',
  executing: '查询账本', verifying: '核验数据', synthesizing: '整理分析',
  waiting_for_user: '等待补充', completed: '已完成', failed: '执行未完成',
}

function agentPhaseLabel(phase?: string) {
  return agentPhaseLabels[String(phase || '')] || 'Agent 正在处理'
}

function agentStepStatusLabel(status: AIChatAgentTaskStep['status']) {
  const labels: Record<AIChatAgentTaskStep['status'], string> = {
    pending: '等待中',
    running: '执行中',
    succeeded: '完成',
    failed: '失败',
    skipped: '跳过',
  }

  return labels[status]
}

function agentStepStatusClass(status: AIChatAgentTaskStep['status']) {
  return `agent-step--${status}`
}

const agentToolLabels: Record<string, string> = {
  get_financial_overview: '月度收支总览',
  get_category_breakdown: '分类分布',
  analyze_spending_trends: '消费趋势比较',
  detect_spending_anomalies: '异常消费检查',
  get_top_merchants: '商户排名',
  search_transactions: '交易关键词查询',
  get_recent_transactions: '最近交易查询',
  get_transactions_by_date: '指定日期流水',
  get_category_transactions: '分类交易明细',
  get_category_tree: '账本分类树',
  get_history_learning_status: '历史分类学习状态',
  get_pending_history_classifications: '待确认分类查询',
  propose_category_rule: '分类规则提案',
  propose_transaction_reclassification: '单笔分类提案',
  propose_bulk_reclassification: '批量分类提案',
  propose_manual_transaction: '手动记账提案',
}

function agentEvidenceToolLabel(tool: string) {
  return agentToolLabels[tool] || tool
}

function agentEvidenceStateLabel(state: AIChatAgentEvidence['state']) {
  return ({
    verified: '已核验', no_data: '无匹配数据', partial: '部分数据',
    failed: '查询失败', incomplete: '账本不完整', conflict: '证据冲突', proposal: '等待确认', unknown: '未知状态',
  } as Record<AIChatAgentEvidence['state'], string>)[state] || '未知状态'
}

function agentEvidenceScope(scope: AIChatAgentEvidence['scope']) {
  const labels: Record<string, string> = {
    month: '月份', compare_month: '对比月份', date: '日期', date_from: '开始日期',
    date_to: '结束日期', direction: '方向', keyword: '关键词', category: '分类',
  }
  return Object.entries(scope || {}).map(([key, value]) => `${labels[key] || key}：${String(value)}`).join(' · ')
}

function useQuickPrompt(value: string) {
  draft.value = value
}

onMounted(() => {
  restorePosition()
  restorePanelPosition()
  window.addEventListener('resize', onWindowResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerUp)
  window.removeEventListener('pointermove', onPanelPointerMove)
  window.removeEventListener('pointerup', onPanelPointerUp)
  window.removeEventListener('pointercancel', onPanelPointerUp)
  if (speakingTimer !== undefined) window.clearTimeout(speakingTimer)
})
</script>

<template>
  <div class="penguin-companion" :class="{ 'penguin-companion--open': isOpen }">
    <Transition name="chat-pop">
      <section
        v-if="isOpen"
        class="penguin-chat-panel"
        :class="{ 'penguin-chat-panel--dragging': isPanelDragging }"
        :style="{ left: `${panelPosition.left}px`, top: `${panelPosition.top}px` }"
        aria-label="Penny AI Finance 对话"
      >
        <header class="chat-header" @pointerdown="onPanelHeaderPointerDown">
          <div class="chat-identity">
            <span class="chat-avatar">
              <svg viewBox="0 0 64 64" aria-hidden="true">
                <ellipse cx="32" cy="36" rx="21" ry="25" fill="#172A3B"/>
                <ellipse cx="32" cy="42" rx="13" ry="17" fill="#F8FBFF"/>
                <circle cx="32" cy="19" r="15" fill="#172A3B"/>
                <ellipse cx="22" cy="21" rx="5" ry="8" fill="#F7F9FC"/>
                <ellipse cx="42" cy="21" rx="5" ry="8" fill="#F7F9FC"/>
                <circle cx="27" cy="19" r="2" fill="#172A3B"/>
                <circle cx="37" cy="19" r="2" fill="#172A3B"/>
                <path d="M28 25 L36 25 L32 31 Z" fill="#F2A23A"/>
                <path d="M16 57 Q24 52 30 58 L30 61 L15 61 Z" fill="#E89A32"/>
                <path d="M34 58 Q42 52 49 57 L49 61 L34 61 Z" fill="#E89A32"/>
                <path d="M12 34 Q4 42 13 51" fill="none" stroke="#263D50" stroke-width="5" stroke-linecap="round"/>
                <path d="M52 34 Q60 42 51 51" fill="none" stroke="#263D50" stroke-width="5" stroke-linecap="round"/>
              </svg>
            </span>
            <span class="chat-title">
              <strong>Penny</strong>
              <small>AI Finance Companion</small>
            </span>
            <span class="online-dot" title="助手已就绪"></span>
          </div>
          <div class="chat-header-actions">
            <button class="icon-button" type="button" title="新建对话" aria-label="新建对话" @pointerdown.stop @click="createNewConversation">
              <span>＋</span>
            </button>
            <button
              class="icon-button conversation-delete-button"
              type="button"
              title="删除当前对话"
              aria-label="删除当前对话"
              :disabled="!conversationId || isDeletingConversation || isSending || isLoading"
              @pointerdown.stop
              @click="deleteCurrentConversation"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5" />
              </svg>
            </button>
            <button class="icon-button" type="button" title="收起" aria-label="收起对话" @pointerdown.stop @click="isOpen = false">
              <span>－</span>
            </button>
          </div>
        </header>

        <div class="chat-toolbar">
          <select :value="conversationId || ''" aria-label="选择历史对话" @change="onConversationSelect">
            <option v-for="item in conversationOptions" :key="item.id" :value="item.id">
              {{ item.title || '新对话' }}
            </option>
          </select>
          <span v-if="isLoading" class="toolbar-hint">读取中…</span>
          <span v-else class="toolbar-hint">对话自动保存</span>
        </div>

        <div ref="scrollArea" class="chat-messages">
          <div v-if="messages.length === 0" class="welcome-state">
            <div class="welcome-penguin">
              <svg viewBox="0 0 100 116" aria-hidden="true">
                <ellipse cx="50" cy="70" rx="35" ry="42" fill="#172A3B"/>
                <ellipse cx="50" cy="80" rx="22" ry="28" fill="#F8FBFF"/>
                <circle cx="50" cy="29" r="25" fill="#172A3B"/>
                <ellipse cx="34" cy="33" rx="8" ry="13" fill="#F7F9FC"/>
                <ellipse cx="66" cy="33" rx="8" ry="13" fill="#F7F9FC"/>
                <circle cx="42" cy="29" r="3" fill="#172A3B"/>
                <circle cx="58" cy="29" r="3" fill="#172A3B"/>
                <path d="M43 39 L57 39 L50 49 Z" fill="#F2A23A"/>
                <path d="M25 107 Q38 98 48 108 L48 113 L23 113 Z" fill="#E89A32"/>
                <path d="M52 108 Q64 98 77 107 L77 113 L52 113 Z" fill="#E89A32"/>
              </svg>
            </div>
            <h3>你好，我是 Penny</h3>
            <p>我可以查询你的账单、分析消费，也能在你确认后帮你调整分类和保存规则。</p>
            <div class="quick-prompts">
              <button type="button" @click="useQuickPrompt('帮我分析最近一个月的收入和支出。')">分析最近一个月</button>
              <button type="button" @click="useQuickPrompt('我最近消费最多的商户有哪些？')">消费最多的商户</button>
              <button type="button" @click="useQuickPrompt('检查最近一个月的异常消费和疑似重复交易。')">检查异常消费</button>
              <button type="button" @click="useQuickPrompt('查看 AI 历史账单学习的结果。')">查看历史学习结果</button>
            </div>
          </div>

          <template v-for="(message, index) in messages" :key="message.id || `${message.role}-${index}`">
            <div class="message-row" :class="`message-row--${message.role}`">
              <div v-if="message.role === 'assistant'" class="message-penguin">
                <svg viewBox="0 0 64 64" aria-hidden="true">
                  <ellipse cx="32" cy="36" rx="21" ry="25" fill="#172A3B"/>
                  <ellipse cx="32" cy="42" rx="13" ry="17" fill="#F8FBFF"/>
                  <circle cx="32" cy="19" r="15" fill="#172A3B"/>
                  <ellipse cx="22" cy="21" rx="5" ry="8" fill="#F7F9FC"/>
                  <ellipse cx="42" cy="21" rx="5" ry="8" fill="#F7F9FC"/>
                  <circle cx="27" cy="19" r="2" fill="#172A3B"/>
                  <circle cx="37" cy="19" r="2" fill="#172A3B"/>
                  <path d="M28 25 L36 25 L32 31 Z" fill="#F2A23A"/>
                </svg>
              </div>
              <article class="message-bubble">
                <div class="message-content" v-html="renderMarkdown(message.content)"></div>
                <time v-if="message.created_at">{{ formatTime(message.created_at) }}</time>
              </article>
            </div>
          </template>

          <div v-if="isSending" class="message-row message-row--assistant">
            <div class="message-penguin thinking-penguin">
              <span></span><span></span><span></span>
            </div>
            <article class="message-bubble typing-bubble agent-progress-bubble">
              <strong>{{ agentPhaseLabel(agentTask?.phase) }}</strong>
              <span>{{ agentTask?.progress_message || '正在理解你的问题并准备查询…' }}</span>
              <div v-if="agentTask && agentTask.total_steps > 0" class="agent-progress-meter">
                <span>{{ agentTask.completed_steps }}/{{ agentTask.total_steps }} 个步骤完成</span>
                <div class="agent-progress-track"><i :style="{ width: `${Math.round(agentTask.completed_steps / agentTask.total_steps * 100)}%` }"></i></div>
              </div>
              <ol v-if="agentTask?.steps?.length" class="agent-step-list">
                <li v-for="step in agentTask.steps" :key="`${step.generation}-${step.id}`" :class="agentStepStatusClass(step.status)">
                  <span class="agent-step-indicator"></span>
                  <span class="agent-step-copy">{{ step.purpose || step.tool }}</span>
                  <small>{{ agentStepStatusLabel(step.status) }}</small>
                </li>
              </ol>
            </article>
          </div>

          <div v-for="action in actions" :key="action.id" class="action-card">
            <div class="action-card-heading">
              <strong>{{ action.status === 'pending' ? '等待你确认' : action.status === 'confirmed' ? '已确认执行' : '已拒绝' }}</strong>
              <span :class="`action-status action-status--${action.status}`"></span>
            </div>
            <p>{{ actionTitle(action) }}</p>
            <small>{{ String(action.payload.reason || '这是 AI 根据你的要求生成的操作建议。') }}</small>
            <ul v-if="action.action_type === 'bulk_reclassify_transactions' && actionSamples(action).length" class="action-preview-list">
              <li v-for="sample in actionSamples(action)" :key="String(sample.tx_id)">
                {{ String(sample.date) }} · {{ String(sample.merchant) }} · ¥{{ Number(sample.amount || 0).toFixed(2) }}
              </li>
              <li v-if="remainingActionSampleCount(action)">另有 {{ remainingActionSampleCount(action) }} 笔匹配交易</li>
            </ul>
            <div v-if="action.action_type === 'create_manual_transaction'" class="action-preview-meta">
              {{ String(action.payload.date || '') }} {{ String(action.payload.time || '') }} · {{ String(action.payload.direction || '') }} · 分类：{{ String(action.payload.target_path || '其他') }}
            </div>
            <div v-if="action.status === 'pending'" class="action-buttons">
              <button type="button" class="action-confirm" :disabled="isActionBusy === action.id" @click="confirmAction(action)">
                {{ isActionBusy === action.id ? '处理中…' : '确认执行' }}
              </button>
              <button type="button" class="action-reject" :disabled="isActionBusy === action.id" @click="rejectAction(action)">暂不执行</button>
            </div>
            <div v-else class="action-complete">
              {{ action.status === 'confirmed' ? '操作已通过业务服务执行。' : '这项建议没有执行。' }}
            </div>
          </div>

          <section
            v-if="!isSending && agentTask && (agentTask.status === 'completed' || agentTask.status === 'failed' || agentTask.status === 'waiting_for_user') && (agentTask.steps.length || agentTask.evidence?.length)"
            class="agent-result-card"
          >
            <div class="agent-result-heading">
              <strong>{{ agentTask.status === 'completed' ? '本轮任务记录' : agentTask.status === 'waiting_for_user' ? '等待补充信息' : '任务执行记录' }}</strong>
              <span>{{ agentPhaseLabel(agentTask.phase) }}</span>
            </div>
            <ol v-if="agentTask.steps.length" class="agent-step-list agent-result-steps">
              <li v-for="step in agentTask.steps" :key="`${step.generation}-${step.id}`" :class="agentStepStatusClass(step.status)">
                <span class="agent-step-indicator"></span>
                <span class="agent-step-copy">{{ step.purpose || agentEvidenceToolLabel(step.tool) }}</span>
                <small>{{ agentStepStatusLabel(step.status) }}</small>
              </li>
            </ol>
            <div v-if="agentTask.evidence?.length" class="agent-evidence-list">
              <strong>数据来源</strong>
              <div v-for="item in agentTask.evidence" :key="item.evidence_id" class="agent-evidence-item">
                <span>{{ agentEvidenceToolLabel(item.tool) }}</span>
                <small>{{ agentEvidenceScope(item.scope) || '当前账本' }}</small>
                <em :class="`agent-evidence-state--${item.state}`">{{ agentEvidenceStateLabel(item.state) }}</em>
                <small v-if="item.reason">{{ item.reason }}</small>
              </div>
            </div>
          </section>
        </div>

        <div v-if="errorMessage" class="chat-error">{{ errorMessage }}</div>

        <footer class="chat-composer">
          <textarea
            v-model="draft"
            rows="2"
            maxlength="4000"
            placeholder="问问你的财务助手…"
            :disabled="isSending"
            @keydown.enter.exact.prevent="sendMessage"
          ></textarea>
          <div class="composer-bottom">
            <span>Enter 发送 · Shift + Enter 换行</span>
            <button type="button" :disabled="!draft.trim() || isSending" @click="sendMessage">
              {{ isSending ? '思考中…' : '发送 ↑' }}
            </button>
          </div>
        </footer>
      </section>
    </Transition>

    <button
      class="penguin-launcher"
      :class="{ 'penguin-launcher--dragging': isDragging, 'penguin-launcher--thinking': isSending, 'penguin-launcher--speaking': isSpeaking }"
      :style="{ left: `${position.left}px`, top: `${position.top}px` }"
      type="button"
      aria-label="打开 AI Finance Penny 助手，可拖动"
      title="拖动我，点击和我聊天"
      @pointerdown="onPointerDown"
      @click="openChat"
    >
      <span class="launcher-glow"></span>
      <svg class="launcher-penguin" viewBox="0 0 100 116" aria-hidden="true">
        <defs>
          <linearGradient id="penguin-body" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#304A61"/>
            <stop offset="1" stop-color="#101F2D"/>
          </linearGradient>
          <linearGradient id="penguin-gold" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#FFD17B"/>
            <stop offset="1" stop-color="#E58A2D"/>
          </linearGradient>
        </defs>
        <ellipse cx="50" cy="72" rx="35" ry="41" fill="url(#penguin-body)"/>
        <ellipse cx="50" cy="81" rx="22" ry="27" fill="#F8FBFF"/>
        <circle cx="50" cy="30" r="25" fill="url(#penguin-body)"/>
        <path d="M30 28 Q33 8 49 9 Q65 10 70 28 Q62 22 56 27 Q50 34 44 27 Q37 23 30 28Z" fill="#24394D"/>
        <ellipse cx="34" cy="34" rx="8" ry="13" fill="#FFF9EF"/>
        <ellipse cx="66" cy="34" rx="8" ry="13" fill="#FFF9EF"/>
        <g class="penguin-eyes">
          <circle cx="42" cy="30" r="3.2" fill="#13202C"/>
          <circle cx="58" cy="30" r="3.2" fill="#13202C"/>
          <circle cx="43" cy="29" r="1" fill="#FFFFFF"/>
          <circle cx="59" cy="29" r="1" fill="#FFFFFF"/>
        </g>
        <path class="penguin-beak" d="M43 40 L57 40 L50 51 Z" fill="url(#penguin-gold)"/>
        <path class="penguin-flipper penguin-flipper--left" d="M18 55 Q2 67 16 85 Q22 87 27 77" fill="#1B3043"/>
        <path class="penguin-flipper penguin-flipper--right" d="M82 55 Q98 67 84 85 Q78 87 73 77" fill="#1B3043"/>
        <path d="M26 107 Q38 100 48 108 L48 113 L23 113 Z" fill="#E99435"/>
        <path d="M52 108 Q64 100 77 107 L77 113 L52 113 Z" fill="#E99435"/>
        <path d="M31 66 Q50 54 69 66" fill="none" stroke="#FFFFFF" stroke-opacity=".18" stroke-width="2"/>
      </svg>
      <span v-if="!isOpen" class="launcher-label">Penny · 和我聊聊</span>
      <span v-if="isSending" class="launcher-thinking-dot"></span>
      <span v-else class="launcher-status-dot"></span>
    </button>
  </div>
</template>

<style scoped>
.penguin-launcher {
  position: fixed;
  z-index: 10020;
  width: 76px;
  height: 88px;
  border: 0;
  padding: 0;
  background: transparent;
  cursor: grab;
  touch-action: none;
  user-select: none;
  filter: drop-shadow(0 9px 14px rgba(22, 43, 64, .20));
  transition: filter .18s ease, transform .18s ease;
}
.penguin-launcher:hover { filter: drop-shadow(0 12px 18px rgba(22, 43, 64, .28)); transform: translateY(-3px); }
.penguin-launcher--dragging { cursor: grabbing; transition: none; transform: scale(1.06); }
.launcher-penguin { position: relative; z-index: 1; width: 100%; height: 100%; overflow: visible; animation: penguin-float 3.6s ease-in-out infinite; }
.penguin-eyes { transform-box: fill-box; transform-origin: center; animation: penguin-blink 5.8s infinite; }
.penguin-beak, .penguin-flipper { transform-box: fill-box; transform-origin: center top; }
.launcher-glow {
  position: absolute; inset: 13px 4px 2px; border-radius: 48% 48% 44% 44%;
  background: radial-gradient(ellipse, rgba(95, 164, 226, .28), rgba(95, 164, 226, 0) 70%);
}
.launcher-label {
  position: absolute; z-index: 2; right: 68px; top: 26px; white-space: nowrap;
  border: 1px solid rgba(214, 226, 239, .9); border-radius: 999px; padding: 6px 11px;
  color: #35516D; background: rgba(255,255,255,.94); font-size: 12px; font-weight: 650;
  box-shadow: 0 5px 16px rgba(31, 59, 85, .10); opacity: 0; transform: translateX(5px);
  transition: .18s ease; pointer-events: none;
}
.penguin-launcher:hover .launcher-label { opacity: 1; transform: translateX(0); }
.launcher-status-dot, .launcher-thinking-dot {
  position: absolute; right: 5px; top: 9px; z-index: 3; width: 11px; height: 11px;
  border: 2px solid #fff; border-radius: 50%; background: #27B27B;
}
.launcher-thinking-dot { background: #438FE2; animation: pulse 1s infinite; }
.penguin-launcher--thinking .launcher-penguin { animation: penguin-think 1.1s ease-in-out infinite; }
.penguin-launcher--thinking .penguin-flipper--left { animation: flipper-think .55s ease-in-out infinite alternate; }
.penguin-launcher--thinking .penguin-flipper--right { animation: flipper-think .55s ease-in-out infinite alternate-reverse; }
.penguin-launcher--speaking .penguin-beak { animation: beak-talk .22s ease-in-out 5 alternate; }
.penguin-launcher--speaking .penguin-flipper--left { animation: flipper-talk .32s ease-in-out 3 alternate; }
.penguin-launcher--speaking .penguin-flipper--right { animation: flipper-talk .32s ease-in-out 3 alternate-reverse; }
.penguin-chat-panel {
  position: fixed; z-index: 10019;
  display: flex; flex-direction: column; width: min(410px, calc(100vw - 24px)); height: min(620px, calc(100vh - 24px));
  overflow: hidden; border: 1px solid rgba(211, 222, 234, .9); border-radius: 22px;
  background: #F8FAFD; box-shadow: 0 22px 70px rgba(22, 42, 62, .24);
  color: #21364A;
}
.penguin-chat-panel--dragging { box-shadow: 0 28px 80px rgba(22, 42, 62, .30); }
.chat-header {
  display: flex; align-items: center; justify-content: space-between; padding: 14px 16px;
  background: linear-gradient(120deg, #F7FBFF, #EAF3FC); border-bottom: 1px solid #E1EAF3;
  cursor: grab; touch-action: none; user-select: none;
}
.penguin-chat-panel--dragging .chat-header { cursor: grabbing; }
.chat-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.chat-avatar { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 14px; background: #DCEBFA; }
.chat-avatar svg { width: 36px; height: 36px; }
.chat-title { display: flex; flex-direction: column; gap: 2px; }
.chat-title strong { font-size: 14px; color: #1D344A; }
.chat-title small { font-size: 10px; color: #7790A8; letter-spacing: .4px; }
.online-dot { width: 8px; height: 8px; border-radius: 50%; background: #31B77E; margin-left: 2px; }
.chat-header-actions { display: flex; gap: 6px; }
.icon-button {
  display: grid; place-items: center; width: 30px; height: 30px; border: 0; border-radius: 9px;
  background: rgba(255,255,255,.8); color: #617B94; cursor: pointer; font-size: 19px;
}
.icon-button:hover { background: #fff; color: #255D91; }
.conversation-delete-button { color: #A46B6B; }
.conversation-delete-button:hover:not(:disabled) { background: #FFF1F1; color: #B54747; }
.conversation-delete-button:disabled { opacity: .35; cursor: not-allowed; }
.conversation-delete-button svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.chat-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 14px; background: #fff; border-bottom: 1px solid #EDF1F5; }
.chat-toolbar select { min-width: 0; max-width: 68%; border: 0; outline: 0; background: transparent; color: #617A92; font-size: 11px; }
.toolbar-hint { color: #9AAABD; font-size: 10px; }
.chat-messages { flex: 1; min-height: 0; overflow-y: auto; padding: 16px 13px; scroll-behavior: smooth; }
.welcome-state { display: flex; flex-direction: column; align-items: center; padding: 20px 7px 10px; text-align: center; }
.welcome-penguin { width: 88px; height: 102px; margin-bottom: 10px; filter: drop-shadow(0 8px 10px rgba(23,42,59,.13)); }
.welcome-penguin svg { width: 100%; height: 100%; }
.welcome-state h3 { margin: 2px 0 7px; color: #263F56; font-size: 16px; }
.welcome-state p { max-width: 300px; margin: 0; color: #7B91A7; font-size: 12px; line-height: 1.7; }
.quick-prompts { display: flex; flex-wrap: wrap; justify-content: center; gap: 7px; margin-top: 18px; }
.quick-prompts button {
  border: 1px solid #DCE7F1; border-radius: 999px; padding: 7px 10px;
  background: #fff; color: #55718D; cursor: pointer; font-size: 11px;
}
.quick-prompts button:hover { border-color: #9BBFE1; color: #2D6EA8; }
.message-row { display: flex; align-items: flex-end; gap: 7px; margin: 12px 0; }
.message-row--user { justify-content: flex-end; }
.message-penguin { display: grid; place-items: center; flex: 0 0 28px; width: 28px; height: 28px; border-radius: 10px; background: #E0ECF8; }
.message-penguin svg { width: 25px; height: 25px; }
.message-bubble { max-width: 84%; border-radius: 14px 14px 14px 4px; padding: 10px 12px; background: #fff; border: 1px solid #E7EDF3; box-shadow: 0 2px 5px rgba(25,52,78,.035); }
.message-row--user .message-bubble { border: 0; border-radius: 14px 14px 4px 14px; background: #397BB8; color: #fff; }
.message-content { overflow-wrap: anywhere; font-size: 12px; line-height: 1.7; }
.message-content :deep(p) { margin: 0 0 8px; white-space: normal; }
.message-content :deep(p:last-child) { margin-bottom: 0; }
.message-content :deep(strong) { color: #203E5A; font-weight: 750; }
.message-row--user .message-content :deep(strong) { color: #fff; }
.message-content :deep(h1), .message-content :deep(h2), .message-content :deep(h3), .message-content :deep(h4) {
  margin: 10px 0 6px; color: #254B6D; line-height: 1.4; font-weight: 750;
}
.message-content :deep(h1) { font-size: 16px; }
.message-content :deep(h2) { font-size: 14px; }
.message-content :deep(h3), .message-content :deep(h4) { font-size: 13px; }
.message-content :deep(ul), .message-content :deep(ol) { margin: 5px 0 9px; padding-left: 19px; }
.message-content :deep(li) { margin: 3px 0; }
.message-content :deep(code) { padding: 1px 4px; border-radius: 5px; background: #EDF3F8; color: #315E82; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .92em; }
.message-content :deep(pre) { overflow-x: auto; margin: 8px 0; padding: 10px; border-radius: 9px; background: #172A3B; color: #EAF2FA; }
.message-content :deep(pre code) { padding: 0; background: transparent; color: inherit; }
.message-content :deep(blockquote) { margin: 8px 0; padding: 4px 9px; border-left: 3px solid #9BBFE1; color: #6D8499; background: #F3F7FB; }
.message-content :deep(hr) { margin: 9px 0; border: 0; border-top: 1px solid #E2EAF1; }
.message-content :deep(a) { color: #2C75B4; text-decoration: underline; text-underline-offset: 2px; }
.message-content :deep(del) { color: #8395A6; }
.message-content :deep(table) { width: 100%; border-collapse: collapse; display: block; overflow-x: auto; margin: 8px 0 10px; font-size: 10px; }
.message-content :deep(th), .message-content :deep(td) { padding: 6px 8px; border: 1px solid #E2EAF1; text-align: left; vertical-align: top; white-space: normal; }
.message-content :deep(th) { background: #F1F6FB; color: #34536E; font-weight: 650; }
.message-content :deep(tbody tr:nth-child(even)) { background: #F8FAFC; }
.message-bubble time { display: block; margin-top: 5px; text-align: right; color: #A1AFBC; font-size: 9px; }
.message-row--user .message-bubble time { color: rgba(255,255,255,.7); }
.agent-result-card { margin: 10px 0 10px 35px; padding: 10px 11px; border: 1px solid #E2EAF1; border-radius: 12px; background: #F8FAFC; }
.agent-result-heading { display: flex; justify-content: space-between; gap: 8px; color: #34536E; font-size: 11px; }
.agent-result-heading span { color: #8297AA; font-size: 10px; }
.agent-result-steps { margin-top: 9px; }
.agent-evidence-list { display: grid; gap: 7px; margin-top: 10px; padding-top: 9px; border-top: 1px solid #E2EAF1; }
.agent-evidence-list > strong { color: #34536E; font-size: 10px; }
.agent-evidence-item { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 2px 8px; color: #526B82; font-size: 10px; }
.agent-evidence-item > span { font-weight: 650; }
.agent-evidence-item > small { color: #8297AA; font-size: 9px; overflow-wrap: anywhere; }
.agent-evidence-item > em { grid-column: 2; grid-row: 1; align-self: center; color: #64816F; font-size: 9px; font-style: normal; }
.agent-evidence-state--failed, .agent-evidence-state--incomplete { color: #C64A4A !important; }
.agent-evidence-state--partial { color: #A7792C !important; }
.agent-evidence-state--proposal { color: #347DB9 !important; }
.agent-progress-bubble { display: flex; flex-direction: column; gap: 7px; min-width: 210px; }
.agent-progress-bubble > strong { color: #254B6D; font-size: 12px; }
.agent-progress-bubble > span { color: #6D8499; font-size: 12px; line-height: 1.5; }
.agent-progress-meter { display: grid; gap: 5px; color: #8297AA; font-size: 10px; }
.agent-progress-track { height: 4px; overflow: hidden; border-radius: 99px; background: #E2EAF1; }
.agent-progress-track i { display: block; height: 100%; border-radius: inherit; background: #4C8BC3; transition: width .2s ease; }
.agent-step-list { display: grid; gap: 5px; margin: 2px 0 0; padding: 0; list-style: none; }
.agent-step-list li { display: grid; grid-template-columns: 7px minmax(0,1fr) auto; align-items: center; gap: 6px; color: #6D8499; font-size: 10px; }
.agent-step-indicator { width: 6px; height: 6px; border-radius: 50%; background: #8297AA; }
.agent-step-copy { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-step-list small { color: #8297AA; font-size: 9px; }
.agent-step--running .agent-step-indicator { background: #4C8BC3; box-shadow: 0 0 0 3px color-mix(in srgb, #4C8BC3 16%, transparent); }
.agent-step--succeeded .agent-step-indicator { background: #269c6a; }
.agent-step--failed .agent-step-indicator { background: #d65a5a; }
.agent-step--skipped .agent-step-indicator { background: #b68b45; }
.typing-bubble { color: #8297AA; font-size: 11px; }
.thinking-penguin { display: flex; flex-direction: row; gap: 3px; align-items: center; }
.thinking-penguin span { width: 4px; height: 4px; border-radius: 50%; background: #4C8BC3; animation: typing 1s infinite ease-in-out; }
.thinking-penguin span:nth-child(2) { animation-delay: .15s; }
.thinking-penguin span:nth-child(3) { animation-delay: .3s; }
.action-card { margin: 12px 0 12px 35px; padding: 12px; border: 1px solid #E5D5A8; border-radius: 13px; background: #FFFCF3; }
.action-card-heading { display: flex; justify-content: space-between; align-items: center; color: #8D6B2A; font-size: 11px; }
.action-status { width: 7px; height: 7px; border-radius: 50%; background: #D6A43B; }
.action-status--confirmed { background: #28A879; }
.action-status--rejected { background: #AAB5C0; }
.action-card p { margin: 8px 0 4px; color: #394F64; font-size: 12px; line-height: 1.6; }
.action-card small { color: #8A9BAB; font-size: 10px; line-height: 1.5; }
.action-preview-list { margin: 8px 0 0; padding-left: 17px; color: #6E8295; font-size: 10px; line-height: 1.7; }
.action-preview-meta { margin-top: 8px; color: #6E8295; font-size: 10px; }
.action-buttons { display: flex; gap: 7px; margin-top: 10px; }
.action-buttons button { border: 0; border-radius: 8px; padding: 7px 10px; cursor: pointer; font-size: 11px; }
.action-confirm { background: #347DB9; color: #fff; }
.action-confirm:disabled, .action-reject:disabled { opacity: .55; cursor: wait; }
.action-reject { background: #EEF2F6; color: #61758A; }
.action-complete { margin-top: 8px; color: #6D879E; font-size: 10px; }
.chat-error { flex: 0 0 auto; margin: 0 12px 8px; padding: 8px 10px; border-radius: 9px; background: #FFF0F0; color: #C64A4A; font-size: 11px; line-height: 1.5; }
.chat-composer { padding: 10px 12px 12px; background: #fff; border-top: 1px solid #E5ECF3; }
.chat-composer textarea { display: block; width: 100%; min-height: 52px; max-height: 110px; resize: vertical; border: 0; outline: 0; background: transparent; color: #273D52; font: inherit; font-size: 12px; line-height: 1.6; }
.chat-composer textarea::placeholder { color: #A1B0BE; }
.composer-bottom { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: #A0AFBE; font-size: 9px; }
.composer-bottom button { border: 0; border-radius: 9px; padding: 8px 15px; background: #367BB7; color: #fff; font-size: 11px; font-weight: 650; cursor: pointer; }
.composer-bottom button:disabled { background: #B8C9D8; cursor: not-allowed; }
.chat-pop-enter-active, .chat-pop-leave-active { transition: opacity .16s ease, transform .16s ease; }
.chat-pop-enter-from, .chat-pop-leave-to { opacity: 0; transform: translateY(12px) scale(.98); }
@keyframes penguin-float { 0%,100% { transform: translateY(0) rotate(-1deg); } 50% { transform: translateY(-5px) rotate(1deg); } }
@keyframes penguin-think { 0%,100% { transform: rotate(-3deg); } 50% { transform: rotate(3deg) translateY(-3px); } }
@keyframes penguin-blink { 0%, 43%, 47%, 76%, 80%, 100% { transform: scaleY(1); } 45%, 78% { transform: scaleY(.12); } }
@keyframes flipper-think { from { transform: rotate(-5deg); } to { transform: rotate(9deg); } }
@keyframes beak-talk { from { transform: scaleY(1); } to { transform: scaleY(.72) translateY(2px); } }
@keyframes flipper-talk { from { transform: rotate(0); } to { transform: rotate(12deg); } }
@keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(67,143,226,.3); } 50% { box-shadow: 0 0 0 6px rgba(67,143,226,0); } }
@keyframes typing { 0%,60%,100% { opacity: .35; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }
@media (max-width: 560px) {
  .penguin-chat-panel { width: calc(100vw - 16px); height: calc(100vh - 16px); border-radius: 17px; }
  .penguin-companion--open .penguin-launcher { display: none; }
  .chat-messages { padding: 12px 10px; }
  .penguin-launcher { width: 68px; height: 80px; }
}
@media (prefers-reduced-motion: reduce) {
  .launcher-penguin, .penguin-eyes, .penguin-launcher--thinking .launcher-penguin,
  .penguin-launcher--thinking .penguin-flipper, .penguin-launcher--speaking .penguin-beak,
  .penguin-launcher--speaking .penguin-flipper, .thinking-penguin span { animation: none !important; }
}
</style>
