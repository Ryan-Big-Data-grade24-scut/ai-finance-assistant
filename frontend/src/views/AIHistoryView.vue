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
  <div class="history-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">AI FINANCE · PERSONALIZATION</p>
        <h1>历史账单学习</h1>
        <p class="muted">让 AI 分析已经导入的历史交易，形成分类建议。原始账单不会被重新导入，分类也不会在你确认前被修改。</p>
      </div>
      <RouterLink to="/ai/settings" class="settings-link">AI 模型设置</RouterLink>
    </header>

    <section class="card overview-card">
      <div class="section-heading">
        <div><h2>历史数据概况</h2><p>统计当前星云账本中的可见交易。</p></div>
        <span class="status-pill" :class="settings?.ai_ready ? 'ready' : 'warning'">{{ settings?.ai_ready ? 'AI 已就绪' : '需要配置 AI' }}</span>
      </div>
      <div class="stats-grid" v-if="status">
        <div><strong>{{ status.total_transactions }}</strong><span>历史交易总数</span></div>
        <div><strong>{{ status.pending_transactions }}</strong><span>尚未 AI 确认</span></div>
        <div><strong>{{ status.already_reviewed }}</strong><span>已确认分类</span></div>
        <div><strong>{{ status.protected_manual }}</strong><span>人工覆盖总数</span></div>
      </div>
      <p v-if="status" class="notice">人工覆盖总数包含当前账本中的全部人工分类覆盖；其中 {{ status.protected_manual_pending }} 笔原本处于待分类候选中，已从 AI 待处理队列排除。用户已确认的 AI 分类也会受到保护。退款、内部转账等特殊交易由规则排除。</p>
      <div class="actions">
        <button class="primary" type="button" :disabled="busy || isRunning || hasReviewableSession || !settings?.ai_ready || !status?.pending_transactions" @click="startAnalysis">
          {{ busy ? '准备中…' : isRunning ? 'AI 正在分析…' : hasReviewableSession ? '请先完成当前审核任务' : '开始分析历史账单' }}
        </button>
        <button class="secondary" type="button" :disabled="busy" @click="refreshStatus">刷新数据概况</button>
      </div>
      <p v-if="!settings?.configured" class="hint">请先配置模型并保存 API Key。</p>
      <p v-else-if="!settings?.allow_external_processing" class="hint">请先在 AI 设置中勾选数据处理授权。</p>
      <p v-else-if="status && !status.pending_transactions" class="hint">当前没有待分析的历史交易。已确认和人工覆盖的分类会受到保护。</p>
    </section>

    <section v-if="session" class="card result-card" aria-live="polite">
      <div class="section-heading">
        <div><h2>历史学习任务</h2><p>任务编号：{{ session.session_id }}</p></div>
        <span class="status-pill" :class="session.status === 'preview' ? 'ready' : session.status === 'failed' ? 'warning' : 'working'">{{ session.status === 'preview' ? '分析完成，等待审核' : session.status === 'applied' ? '已应用' : session.status === 'failed' ? '分析失败' : session.status === 'partially_applied' ? '部分已应用' : '处理中' }}</span>
      </div>
      <div v-if="isRunning" class="progress-panel">
        <div class="spinner"></div>
        <div><b>AI 正在分析你的历史账单</b><p>系统正在汇总交易分布、参考现有分类和已确认规则，再生成分类建议。请保持后端运行，稍后页面会自动刷新。</p></div>
      </div>
      <p v-if="session.error_message" class="error-message">{{ session.error_message }}</p>
      <template v-if="analysis">
        <div class="analysis-summary">
          <b>AI 初步分析（模型生成，尚未独立核验）</b>
          <p>{{ analysis.summary }}</p>
          <small>商户用途、消费场景和商品属性可能包含模型推断，不是原始账单字段。请以真实交易记录、可核验历史依据和你的确认结果为准。</small>
        </div>
        <p class="notice" v-if="analysis.date_from">本次分析覆盖 {{ analysis.date_from }} 至 {{ analysis.date_to }} · 平台分布：{{ sourceSummary }} · 任务快照时间：{{ formatDateTime(session.created_at) }}</p>
        <div class="stats-grid result-stats">
          <div><strong>{{ analysis.counts.total }}</strong><span>参与分析</span></div>
          <div><strong>{{ analysis.counts.suggested }}</strong><span>生成分类建议</span></div>
          <div><strong>{{ analysis.counts.high_confidence }}</strong><span>高置信度建议</span></div>
          <div><strong>{{ analysis.counts.needs_review }}</strong><span>AI 标记需澄清</span></div>
        </div>
        <p class="notice">另有 {{ analysis.counts.excluded }} 笔特殊交易不参与普通分类；任务开始时 {{ analysis.protected_manual }} 笔人工覆盖、{{ analysis.already_reviewed }} 笔已确认交易受到保护（这是任务快照，不代表当前实时总数）。高置信度只是模型自评，不是准确率保证；所有历史建议仍须你明确确认后才会写入账本。</p>

        <section v-if="analysis.taxonomy.length" class="taxonomy-section">
          <div class="section-heading"><div><h3>AI 建议分类体系</h3><p>已有分类优先复用；新增分类必须由你批准后才会创建。</p></div></div>
          <div class="taxonomy-list">
            <label v-for="category in analysis.taxonomy" :key="category.path" class="taxonomy-item">
              <input v-if="category.is_new" type="checkbox" :checked="approvedCategoryPaths.includes(category.path)" @change="toggleCategory(category.path, ($event.target as HTMLInputElement).checked)" />
              <span v-else class="existing-dot">✓</span>
              <span class="taxonomy-copy"><b>{{ category.path }}</b><small>{{ category.description }}</small></span>
              <span class="category-tag" :class="category.is_new ? 'new' : 'existing'">{{ category.is_new ? '待批准新增' : '复用现有' }}</span>
            </label>
          </div>
        </section>

        <section class="suggestions-section">
          <div class="section-heading"><div><h3>历史交易分类建议</h3><p>默认选中高置信度且使用现有分类的建议。最终仍由你点击确认。</p></div><b>{{ selectedCount }} 笔已选择</b></div>
          <div class="toolbar">
            <input v-model="search" type="search" placeholder="搜索商户、商品或分类" @input="page = 1" />
            <button class="secondary" type="button" @click="selectHighConfidence">选择高置信度建议（仍需你确认）</button>
            <button class="secondary" type="button" @click="clearSelection">清空选择</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>应用</th><th>交易</th><th>当前分类</th><th>AI 建议分类</th><th>置信度</th><th>判断依据</th><th>记住规则</th></tr></thead>
              <tbody>
                <tr v-for="item in pageSuggestions" :key="item.tx_id" :class="{ excluded: item.excluded }">
                  <td><input type="checkbox" :disabled="!canSelect(item) || session.status === 'applied'" :checked="selectedIds.includes(item.tx_id)" @change="toggleSelected(item, ($event.target as HTMLInputElement).checked)" /></td>
                  <td><b>{{ item.counterparty || '未知商户' }}</b><small>{{ item.tx_time }} · {{ item.source }} · ¥{{ Number(item.amount).toFixed(2) }}</small><small>{{ item.item }}</small></td>
                  <td>{{ item.current_path || '未分类' }}</td>
                  <td>
                    {{ item.excluded ? '特殊交易' : item.target_path || '待判断' }}
                    <small v-if="isGenericFallback(item.target_path)">通用兜底分类，不能作为确定结果；请与 Penny 澄清后再确认。</small>
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
            <button class="secondary" :disabled="page <= 1" @click="page--">上一页</button>
            <span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ filteredSuggestions.length }} 笔</span>
            <button class="secondary" :disabled="page >= pageCount" @click="page++">下一页</button>
          </div>
          <div class="actions apply-actions">
            <button class="primary" type="button" :disabled="committing || !selectedIds.length || session.status === 'applied'" @click="applySelected">{{ committing ? '正在通过 Ledger Engine 应用…' : `确认应用所选 ${selectedCount} 笔历史分类` }}</button>
            <button class="secondary" type="button" :disabled="committing" @click="refreshSession">刷新任务</button>
          </div>
          <p class="hint">确认后只修改 Ledger 分类归属，不修改 transactions_raw 原始流水。选择“记住”后，会把对应商户或商品规则保存到现有分类规则库，供未来导入参考。</p>
        </section>
      </template>
      <div v-if="session.result" class="success-message">{{ session.result.message }}</div>
    </section>

    <p v-if="notice" class="success-message">{{ notice }}</p>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.history-page{max-width:1180px;margin:0 auto;color:var(--color-text)}.page-heading,.section-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}.page-heading{margin:10px 0 22px}.eyebrow{font-size:11px;letter-spacing:.13em;font-weight:700;color:#527ba5;margin-bottom:7px}h1{font-size:28px;margin:0 0 8px}h2{font-size:19px;margin:0 0 5px}h3{font-size:15px;margin:0 0 5px}.muted,.section-heading p,.hint{color:var(--color-muted);line-height:1.55}.settings-link{color:#376fae;text-decoration:none;white-space:nowrap}.card{background:#fff;border:1px solid var(--color-border);border-radius:14px;padding:22px;margin-bottom:16px;box-shadow:0 8px 28px rgba(28,45,70,.04)}.section-heading p{font-size:12px;margin:4px 0 0}.status-pill{padding:5px 10px;border-radius:99px;font-size:12px;white-space:nowrap}.status-pill.ready{background:#e8f7ef;color:#13784a}.status-pill.warning{background:#fff5dc;color:#946200}.status-pill.working{background:#edf3fb;color:#376fae}.stats-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin:18px 0}.stats-grid>div{padding:13px;border-radius:10px;background:#f4f7fb}.stats-grid strong,.stats-grid span{display:block}.stats-grid strong{font-size:23px}.stats-grid span{font-size:11px;color:var(--color-muted);margin-top:3px}.notice{font-size:12px;color:#657487;background:#f6f8fb;padding:10px 12px;border-radius:8px;line-height:1.55}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:16px}button{border:0;border-radius:8px;padding:10px 14px;font-weight:650;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}.primary{background:#376fae;color:white}.secondary{background:#eef3f8;color:#35516f}.hint{font-size:12px;margin-top:10px}.progress-panel{display:flex;align-items:center;gap:14px;padding:18px;margin:16px 0;background:#f4f8fd;border-radius:10px}.progress-panel p{margin:5px 0 0;color:var(--color-muted);font-size:12px;line-height:1.55}.spinner{width:24px;height:24px;border:3px solid #d7e3f0;border-top-color:#376fae;border-radius:50%;animation:spin .8s linear infinite;flex:none}@keyframes spin{to{transform:rotate(360deg)}}.analysis-summary{margin:18px 0;padding:12px 14px;border-radius:9px;background:#f7f9fc;line-height:1.6}.analysis-summary>b{display:block;color:#376fae}.analysis-summary p{margin:5px 0;font-weight:600}.analysis-summary small{display:block;color:var(--color-muted);font-size:11px}.result-stats{margin-top:12px}.taxonomy-section,.suggestions-section{margin-top:22px;padding-top:18px;border-top:1px solid var(--color-border)}.taxonomy-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}.taxonomy-item{display:flex;align-items:center;gap:9px;border:1px solid var(--color-border);border-radius:9px;padding:10px;min-width:0}.taxonomy-copy{min-width:0;flex:1}.taxonomy-copy b,.taxonomy-copy small{display:block}.taxonomy-copy small{font-size:11px;color:var(--color-muted);margin-top:3px}.category-tag{font-size:10px;padding:4px 7px;border-radius:99px;white-space:nowrap}.category-tag.new{background:#fff4db;color:#8c5d0a}.category-tag.existing{background:#eaf6ef;color:#23764b}.existing-dot{color:#238454;font-weight:800}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.toolbar input{flex:1;min-width:220px;min-height:38px;padding:8px 10px;border:1px solid var(--color-border);border-radius:8px}.table-wrap{overflow:auto;max-height:620px;border:1px solid var(--color-border);border-radius:9px}table{width:100%;border-collapse:collapse;min-width:1050px;font-size:12px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid #edf0f4;vertical-align:top}th{position:sticky;top:0;background:#f6f8fb;z-index:1;color:#657487}td small{display:block;color:var(--color-muted);margin-top:4px;line-height:1.4}.reason-cell{max-width:300px;white-space:normal;line-height:1.5}.evidence-list{margin-top:7px;padding:7px;background:#f4f7fb;border-radius:6px}.evidence-list b{display:block;color:#376fae}.evidence-list small{overflow-wrap:anywhere}.excluded{opacity:.55}.remember{white-space:nowrap}.pagination{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:12px;font-size:12px;color:var(--color-muted)}.apply-actions{margin-top:15px}.success-message{color:#16804c;margin-top:12px}.error-message{color:#c33;margin-top:12px;line-height:1.5}@media(max-width:720px){.page-heading{display:block}.settings-link{display:inline-block;margin-top:10px}.card{padding:15px}.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.taxonomy-list{grid-template-columns:1fr}.section-heading{align-items:flex-start}.toolbar{flex-direction:column}.toolbar input{min-width:0}.actions button{flex:1}}
</style>
