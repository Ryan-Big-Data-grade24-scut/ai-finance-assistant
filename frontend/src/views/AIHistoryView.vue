<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  commitAIHistorySession, getAIHistorySession, getAIHistoryStatus, startAIHistoryAnalysis,
  type AIHistorySession, type AIHistoryStatus, type AIHistorySuggestion,
} from '@/api/aiHistory'
import { getAISettings, type AISettings } from '@/api/ai'

const status = ref<AIHistoryStatus | null>(null)
const session = ref<AIHistorySession | null>(null)
const settings = ref<AISettings | null>(null)
const selectedIds = ref<number[]>([])
const rememberIds = ref<number[]>([])
const approvedCategoryPaths = ref<string[]>([])
const busy = ref(false)
const committing = ref(false)
const error = ref('')
const notice = ref('')
const search = ref('')
const page = ref(1)
const pageSize = 200
let pollTimer: number | undefined
let disposed = false

const analysis = computed(() => session.value?.analysis ?? null)
const suggestions = computed(() => analysis.value?.suggestions ?? [])
const filteredSuggestions = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return suggestions.value
  return suggestions.value.filter(item => [item.counterparty, item.item, item.target_path, item.current_path, item.reason]
    .some(value => String(value ?? '').toLowerCase().includes(q)))
})
const pageCount = computed(() => Math.max(1, Math.ceil(filteredSuggestions.value.length / pageSize)))
const pageSuggestions = computed(() => filteredSuggestions.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const selectedCount = computed(() => selectedIds.value.length)
const sourceSummary = computed(() => Object.entries(analysis.value?.source_counts ?? {})
  .map(([name, count]) => `${name} ${count} 笔`).join('、'))
function formatDateTime(value?: string): string {
  if (!value) return '时间未知'
  const normalized = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}
const isRunning = computed(() => session.value?.status === 'queued' || session.value?.status === 'running')
const hasReviewableSession = computed(() => ['preview', 'partially_applied'].includes(session.value?.status ?? ''))

function isNewPath(path: string | null): boolean {
  if (!path) return false
  return Boolean(analysis.value?.taxonomy.find(item => item.path === path)?.is_new)
}
function isGenericFallback(path: string | null | undefined): boolean {
  return ['其他', '其他 / 未细分', '其他 / 待确认', '未分类', '未分类 / 未细分']
    .includes(String(path ?? '').trim())
}
function canSelect(item: AIHistorySuggestion): boolean {
  return !item.excluded && Boolean(item.target_path) && !isGenericFallback(item.target_path)
    && (!isNewPath(item.target_path) || approvedCategoryPaths.value.includes(item.target_path!))
}
function toggleId(list: number[], id: number, checked: boolean): number[] {
  return checked ? (list.includes(id) ? list : [...list, id]) : list.filter(value => value !== id)
}
function toggleSelected(item: AIHistorySuggestion, checked: boolean) {
  selectedIds.value = toggleId(selectedIds.value, item.tx_id, checked)
  if (!checked) rememberIds.value = rememberIds.value.filter(id => id !== item.tx_id)
}
function toggleRemember(item: AIHistorySuggestion, checked: boolean) {
  rememberIds.value = toggleId(rememberIds.value, item.tx_id, checked)
}
function toggleCategory(path: string, checked: boolean) {
  approvedCategoryPaths.value = checked
    ? [...new Set([...approvedCategoryPaths.value, path])]
    : approvedCategoryPaths.value.filter(value => value !== path)
  if (!checked) {
    selectedIds.value = selectedIds.value.filter(id => {
      const item = suggestions.value.find(value => value.tx_id === id)
      return item?.target_path !== path
    })
  }
}

function selectHighConfidence() {
  const threshold = settings.value?.confidence_threshold ?? 0.9
  const ids = suggestions.value.filter(item => canSelect(item) && item.confidence >= threshold).map(item => item.tx_id)
  selectedIds.value = [...new Set(ids)]
  rememberIds.value = rememberIds.value.filter(id => selectedIds.value.includes(id))
}

function clearSelection() {
  selectedIds.value = []
  rememberIds.value = []
}

async function refreshStatus() {
  status.value = await getAIHistoryStatus()
}

async function refreshOverview() {
  error.value = ''
  try {
    await refreshStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '刷新概况失败'
  }
}

async function loadSession(sessionId: string) {
  const value = await getAIHistorySession(sessionId)
  session.value = value
  if (value.commit_request) {
    // 分块提交若中途失败，恢复原提交选择，保证幂等重试。
    approvedCategoryPaths.value = value.commit_request.approved_category_paths
    selectedIds.value = value.commit_request.approved_tx_ids
    rememberIds.value = value.commit_request.remember_rule_tx_ids
  } else if (value.status === 'preview' && value.analysis && selectedIds.value.length === 0) {
    const threshold = settings.value?.confidence_threshold ?? 0.9
    selectedIds.value = value.analysis.suggestions
      .filter(item => !item.excluded && item.target_path && !isGenericFallback(item.target_path)
        && !item.is_new_category && item.confidence >= threshold)
      .map(item => item.tx_id)
  }
  page.value = 1
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearTimeout(pollTimer)
    pollTimer = undefined
  }
}

function schedulePoll(sessionId: string) {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    if (disposed) return
    try {
      await loadSession(sessionId)
      if (session.value && ['queued', 'running'].includes(session.value.status)) schedulePoll(sessionId)
      else await refreshStatus()
    } catch (err) {
      error.value = err instanceof Error ? err.message : '读取历史学习进度失败'
    }
  }, 1800)
}

async function startAnalysis() {
  if (busy.value || isRunning.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  selectedIds.value = []
  rememberIds.value = []
  approvedCategoryPaths.value = []
  try {
    const started = await startAIHistoryAnalysis()
    await loadSession(started.session_id)
    if (['queued', 'running'].includes(started.status)) schedulePoll(started.session_id)
    await refreshStatus()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '无法启动历史账单学习'
  } finally {
    busy.value = false
  }
}

async function refreshSession() {
  if (!session.value) return
  error.value = ''
  try {
    await loadSession(session.value.session_id)
    await refreshStatus()
    if (isRunning.value) schedulePoll(session.value.session_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '刷新失败'
  }
}

async function applySelected() {
  if (!session.value || !selectedIds.value.length || committing.value) return
  committing.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await commitAIHistorySession(session.value.session_id, {
      approved_category_paths: approvedCategoryPaths.value,
      approved_tx_ids: selectedIds.value,
      remember_rule_tx_ids: rememberIds.value,
    })
    notice.value = result.message
    await loadSession(session.value.session_id)
    await refreshStatus()
    clearSelection()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '应用历史分类失败'
    if (session.value) {
      try { await loadSession(session.value.session_id) } catch { /* keep original error */ }
    }
  } finally {
    committing.value = false
  }
}

onMounted(async () => {
  busy.value = true
  try {
    const values = await Promise.all([getAIHistoryStatus(), getAISettings()])
    status.value = values[0]
    settings.value = values[1]
    const latest = values[0].latest_session
    if (latest?.id) {
      await loadSession(latest.id)
      if (['queued', 'running'].includes(latest.status)) schedulePoll(latest.id)
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '页面初始化失败'
  } finally {
    busy.value = false
  }
})

onUnmounted(() => {
  disposed = true
  stopPolling()
})
</script>

<template>
  <div class="workspace-page history-page">
    <header class="workspace-heading">
      <div class="workspace-title">
        <p class="workspace-kicker">AI 工具 / 分类审核</p>
        <h1>历史账单学习</h1>
        <p class="workspace-lede">让 AI 分析已经导入的历史交易，形成分类建议。原始账单不会被重新导入，分类也不会在你确认前被修改。</p>
      </div>
      <RouterLink to="/ai/settings" class="workspace-button">AI 模型设置</RouterLink>
    </header>

    <section class="workspace-panel card overview-card">
      <div class="section-heading">
        <div><h2>历史数据概况</h2><p>统计当前账本中的可见交易。</p></div>
        <span class="workspace-badge" :class="settings?.ai_ready ? 'workspace-badge--ready' : 'workspace-badge--warn'">{{ settings?.ai_ready ? 'AI 已就绪' : busy && !settings ? '读取中…' : 'AI 未就绪' }}</span>
      </div>
      <div class="stats-grid" v-if="status">
        <div><strong>{{ status.total_transactions }}</strong><span>历史交易总数</span></div>
        <div><strong>{{ status.pending_transactions }}</strong><span>尚未 AI 确认</span></div>
        <div><strong>{{ status.already_reviewed }}</strong><span>已确认分类</span></div>
        <div><strong>{{ status.protected_manual }}</strong><span>人工覆盖总数</span></div>
      </div>
      <p v-if="status" class="workspace-note notice">人工覆盖总数包含当前账本中的全部人工分类覆盖；其中 {{ status.protected_manual_pending }} 笔原本处于待分类候选中，已从 AI 待处理队列排除。用户已确认的 AI 分类也会受到保护。退款、内部转账等特殊交易由规则排除。</p>
      <div class="actions">
        <button class="workspace-button workspace-button--primary" type="button" :disabled="busy || isRunning || hasReviewableSession || !settings?.ai_ready || !status?.pending_transactions" @click="startAnalysis">
          {{ busy ? '准备中…' : isRunning ? 'AI 正在分析…' : hasReviewableSession ? '请先完成当前审核任务' : '开始分析历史账单' }}
        </button>
        <button class="workspace-button" type="button" :disabled="busy" @click="refreshOverview">刷新数据概况</button>
      </div>
      <p v-if="settings && !settings.configured" class="hint">请先配置模型并保存 API Key。</p>
      <p v-else-if="settings && !settings.allow_external_processing" class="hint">请先在 AI 设置中勾选数据处理授权。</p>
      <p v-else-if="status && !status.pending_transactions" class="hint">当前没有待分析的历史交易。已确认和人工覆盖的分类会受到保护。</p>
    </section>

    <section v-if="session" class="workspace-panel card result-card">
      <div class="section-heading">
        <div><h2>历史学习任务</h2><p>任务编号：{{ session.session_id }}</p></div>
        <span class="workspace-badge" :class="['preview', 'applied'].includes(session.status) ? 'workspace-badge--ready' : session.status === 'failed' ? 'workspace-badge--warn' : ''">{{ session.status === 'preview' ? '分析完成，等待审核' : session.status === 'applied' ? '已应用' : session.status === 'failed' ? '分析失败' : session.status === 'partially_applied' ? '部分已应用' : '处理中' }}</span>
      </div>
      <div v-if="isRunning" class="workspace-note progress-panel" role="status">
        <span class="spinner" aria-hidden="true" />
        <div><b>AI 正在分析你的历史账单</b><p>系统正在汇总交易分布、参考现有分类和已确认规则，再生成分类建议。请保持后端运行，稍后页面会自动刷新。</p></div>
      </div>
      <p v-if="session.error_message" class="workspace-note workspace-note--error feedback">{{ session.error_message }}</p>
      <template v-if="analysis">
        <div class="workspace-note analysis-summary">
          <b>AI 初步分析（模型生成，尚未独立核验）</b>
          <p>{{ analysis.summary }}</p>
          <small>商户用途、消费场景和商品属性可能包含模型推断，不是原始账单字段。请以真实交易记录、可核验历史依据和你的确认结果为准。</small>
        </div>
        <p class="workspace-note notice" v-if="analysis.date_from">本次分析覆盖 {{ analysis.date_from }} 至 {{ analysis.date_to }} · 平台分布：{{ sourceSummary }} · 任务快照时间：{{ formatDateTime(session.created_at) }}</p>
        <div class="stats-grid result-stats">
          <div><strong>{{ analysis.counts.total }}</strong><span>参与分析</span></div>
          <div><strong>{{ analysis.counts.suggested }}</strong><span>生成分类建议</span></div>
          <div><strong>{{ analysis.counts.high_confidence }}</strong><span>高置信度建议</span></div>
          <div><strong>{{ analysis.counts.needs_review }}</strong><span>AI 标记需澄清</span></div>
        </div>
        <p class="workspace-note notice">另有 {{ analysis.counts.excluded }} 笔特殊交易不参与普通分类；任务开始时 {{ analysis.protected_manual }} 笔人工覆盖、{{ analysis.already_reviewed }} 笔已确认交易受到保护（这是任务快照，不代表当前实时总数）。高置信度只是模型自评，不是准确率保证；所有历史建议仍须你明确确认后才会写入账本。</p>

        <section v-if="analysis.taxonomy.length" class="taxonomy-section">
          <div class="section-heading"><div><h3>AI 建议分类体系</h3><p>已有分类优先复用；新增分类必须由你批准后才会创建。</p></div></div>
          <div class="taxonomy-list">
            <label v-for="category in analysis.taxonomy" :key="category.path" class="taxonomy-item">
              <input v-if="category.is_new" type="checkbox" :checked="approvedCategoryPaths.includes(category.path)" @change="toggleCategory(category.path, ($event.target as HTMLInputElement).checked)" />
              <span v-else class="existing-dot">✓</span>
              <span class="taxonomy-copy"><b>{{ category.path }}</b><small>{{ category.description }}</small></span>
              <span class="workspace-badge category-tag" :class="category.is_new ? 'workspace-badge--warn' : 'workspace-badge--ready'">{{ category.is_new ? '待批准新增' : '复用现有' }}</span>
            </label>
          </div>
        </section>

        <section class="suggestions-section">
          <div class="section-heading"><div><h3>历史交易分类建议</h3><p>默认选中高置信度且使用现有分类的建议。最终仍由你点击确认。</p></div><span class="workspace-badge workspace-badge--ready">{{ selectedCount }} 笔已选择</span></div>
          <div class="toolbar">
            <input v-model="search" class="workspace-field" type="search" aria-label="搜索分类建议" placeholder="搜索商户、商品或分类" @input="page = 1" />
            <button class="workspace-button" type="button" @click="selectHighConfidence">选择高置信度建议</button>
            <button class="workspace-button" type="button" @click="clearSelection">清空选择</button>
          </div>
          <p v-if="pageSuggestions.length" class="table-hint">左右滑动表格可查看判断依据和规则选项。</p>
          <p v-if="!pageSuggestions.length" class="workspace-note notice">{{ search.trim() ? '没有符合搜索条件的建议' : '此任务还没有分类建议' }}</p>
          <div v-else class="table-wrap" tabindex="0" role="region" aria-label="分类建议表格（可滚动）">
            <table>
              <thead><tr><th scope="col">应用</th><th scope="col">交易</th><th scope="col">当前分类</th><th scope="col">AI 建议分类</th><th scope="col">置信度</th><th scope="col">判断依据</th><th scope="col">记住规则</th></tr></thead>
              <tbody>
                <tr v-for="item in pageSuggestions" :key="item.tx_id" :class="{ excluded: item.excluded }">
                  <td><input type="checkbox" :aria-label="`应用交易 ${item.tx_id} 的分类建议`" :disabled="!canSelect(item) || session.status === 'applied'" :checked="selectedIds.includes(item.tx_id)" @change="toggleSelected(item, ($event.target as HTMLInputElement).checked)" /></td>
                  <td><b>{{ item.counterparty || '未知商户' }}</b><small>{{ item.tx_time }} · {{ item.source }} · ¥{{ Number(item.amount).toFixed(2) }}</small><small>{{ item.item }}</small></td>
                  <td>{{ item.current_path || '未分类' }}</td>
                  <td>
                    {{ item.excluded ? '特殊交易' : item.target_path || '待判断' }}
                    <small v-if="isGenericFallback(item.target_path)">通用兜底分类，不能作为确定结果；请核对交易用途后再确认。</small>
                    <small v-else-if="item.question">{{ item.question }}</small>
                  </td>
                  <td>{{ Math.round(item.confidence * 100) }}%</td>
                  <td class="reason-cell">
                    <b>AI 解释（模型生成）</b>
                    <span>{{ item.reason }}</span>
                    <div v-if="item.history_evidence?.length" class="evidence-list">
                      <b>可核验的历史确认依据</b>
                      <small v-for="evidence in item.history_evidence" :key="evidence.tx_id">
                        #{{ evidence.tx_id }} · {{ evidence.date }} · {{ evidence.counterparty }}
                        <template v-if="evidence.item"> · {{ evidence.item }}</template>
                        · {{ evidence.confirmed_category }}（{{ evidence.evidence_type }}；{{ evidence.match_basis }}）
                      </small>
                    </div>
                    <small v-else>未找到同商户且方向一致的已确认历史样本；本条分类目前只是模型建议，不代表已形成个人习惯。</small>
                  </td>
                  <td><label v-if="selectedIds.includes(item.tx_id) && !item.excluded" class="remember"><input type="checkbox" :checked="rememberIds.includes(item.tx_id)" @change="toggleRemember(item, ($event.target as HTMLInputElement).checked)" />保存</label><span v-else>—</span></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pagination" v-if="pageCount > 1">
            <button class="workspace-button" :disabled="page <= 1" @click="page--">上一页</button>
            <span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ filteredSuggestions.length }} 笔</span>
            <button class="workspace-button" :disabled="page >= pageCount" @click="page++">下一页</button>
          </div>
          <div class="actions apply-actions">
            <button class="workspace-button workspace-button--primary" type="button" :disabled="committing || !selectedIds.length || session.status === 'applied'" @click="applySelected">{{ committing ? '正在通过 Ledger Engine 应用…' : `确认应用所选 ${selectedCount} 笔历史分类` }}</button>
            <button class="workspace-button" type="button" :disabled="committing" @click="refreshSession">刷新任务</button>
          </div>
          <p class="hint">确认后只修改 Ledger 分类归属，不修改 transactions_raw 原始流水。选择“记住”后，会把对应商户或商品规则保存到现有分类规则库，供未来导入参考。</p>
        </section>
      </template>
      <div v-if="session.result" class="workspace-note feedback" role="status">{{ session.result.message }}</div>
    </section>

    <p v-if="notice" class="workspace-note feedback" role="status">{{ notice }}</p>
    <p v-if="error" class="workspace-note workspace-note--error feedback" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.history-page { overflow-wrap: anywhere; }
.card { padding: clamp(14px, 2.4vw, 24px); }
.card + .card { margin-top: 16px; }
.section-heading { display: flex; align-items: start; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; }
.section-heading > div { flex: 1 1 240px; min-width: 0; }
h2 { font-size: 18px; }
h3 { font-size: 16px; }
.section-heading p, .hint { margin-top: 4px; color: var(--workspace-muted); font-size: 13px; line-height: 1.55; }
.stats-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 14px 0; }
.stats-grid > div { padding: 12px; border-radius: 10px; background: var(--workspace-soft); }
.stats-grid strong, .stats-grid span { display: block; }
.stats-grid strong { font-size: 22px; font-variant-numeric: tabular-nums; }
.stats-grid span { margin-top: 3px; color: var(--workspace-muted); font-size: 12px; }
.notice, .hint { font-size: 12px; }
.actions, .toolbar, .pagination { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.hint { margin-top: 10px; }
.progress-panel { display: flex; align-items: center; gap: 12px; margin-top: 14px; }
.progress-panel p { margin-top: 4px; font-size: 12px; }
.spinner { flex: none; width: 22px; height: 22px; border: 3px solid var(--workspace-line); border-top-color: var(--workspace-accent); border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.analysis-summary { margin: 14px 0; }
.analysis-summary b, .evidence-list b, .existing-dot { color: var(--workspace-accent); }
.analysis-summary p { margin: 5px 0; color: var(--workspace-ink); }
.analysis-summary small { display: block; }
.taxonomy-section, .suggestions-section { min-width: 0; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--workspace-line); }
.taxonomy-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.taxonomy-item { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 8px; padding: 10px; border: 1px solid var(--workspace-line); border-radius: 9px; }
.taxonomy-item input { margin-top: 4px; }
.taxonomy-copy b, .taxonomy-copy small { display: block; }
.taxonomy-copy small, td small { margin-top: 3px; color: var(--workspace-muted); line-height: 1.5; }
.category-tag { white-space: nowrap; }
.toolbar { margin-bottom: 12px; }
.toolbar input { flex: 1 1 210px; width: auto; }
.table-hint { display: none; margin-bottom: 8px; color: var(--workspace-muted); font-size: 12px; }
.table-wrap { max-width: 100%; max-height: 620px; overflow: auto; border: 1px solid var(--workspace-line); border-radius: 9px; }
table { width: 100%; min-width: 840px; table-layout: fixed; border-collapse: collapse; font-size: 12px; }
th, td { padding: 10px 8px; border-bottom: 1px solid var(--workspace-line); text-align: left; vertical-align: top; }
th { position: sticky; top: 0; z-index: 1; background: var(--workspace-soft); color: var(--workspace-muted); }
th:first-child { width: 50px; }
th:nth-child(2) { width: 19%; }
th:nth-child(3), th:nth-child(4) { width: 14%; }
th:nth-child(5), th:last-child { width: 68px; }
td small, .reason-cell > b, .evidence-list > b { display: block; }
.evidence-list { margin-top: 8px; padding: 8px; border-radius: 6px; background: var(--workspace-soft); }
.excluded { background: var(--workspace-soft); color: var(--workspace-muted); }
.remember { display: inline-flex; align-items: center; gap: 4px; }
input[type="checkbox"] { accent-color: var(--workspace-accent); }
.pagination { justify-content: center; color: var(--workspace-muted); font-size: 12px; }
.feedback { margin-top: 12px; }

@media (max-width: 960px) {
  .table-hint { display: block; }
}
@media (max-width: 720px) {
  .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .taxonomy-list { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 440px) {
  .taxonomy-item { grid-template-columns: auto minmax(0, 1fr); }
  .category-tag { grid-column: 2; }
  .toolbar input { flex: 1 1 100%; width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .spinner { animation: none; }
}
</style>
