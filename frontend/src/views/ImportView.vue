<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { commitImport, getImportHistory, previewImport } from '@/api/imports'
import {
  analyzeAIImport, calibrateAIImport, commitAIImport, getAISettings,
  getAIProposals, getAIProposal, confirmAIProposal, resolveAIProposal, rejectAIProposal,
  type AIImportAnalysis, type AIImportCommitResp, type AIImportMessage, type AISettings,
  type AIProposalSummary, type AIProposalDetail, type AIProposalSuggestion,
} from '@/api/ai'
import type { ImportBatch, ImportCommitResp, ImportPreviewResp } from '@/types/import'

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024
const file = ref<File | null>(null)
const alipayPassword = ref('')
const preview = ref<ImportPreviewResp | null>(null)
const result = ref<ImportCommitResp | null>(null)
const aiCommitResult = ref<AIImportCommitResp | null>(null)
const aiSettings = ref<AISettings | null>(null)
const aiSessionId = ref('')
const aiAnalysis = ref<AIImportAnalysis | null>(null)
const aiMessages = ref<AIImportMessage[]>([])
const chatMessage = ref('')
const pendingProposals = ref<AIProposalSummary[]>([])
const activeProposal = ref<AIProposalDetail | null>(null)
const resolutionTargets = ref<Record<number, string>>({})
const resolutionAnswers = ref<Record<number, string>>({})
const resolutionRemember = ref<Record<number, boolean>>({})
const reviewBusy = ref(false)
const reviewError = ref('')
const approvedCategoryPaths = ref<string[]>([])
const approvedRowIndices = ref<number[]>([])
const rememberRuleRowIndices = ref<number[]>([])
const history = ref<ImportBatch[]>([])
const busy = ref<'preview' | 'commit' | null>(null)
const chatBusy = ref(false)
const error = ref('')
const historyError = ref('')
const historyBusy = ref(false)
const confirmedLowConfidence = ref(false)
const dragActive = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

let lastPreviewFile: File | null = null
let lastPreviewPassword = ''
let previewController: AbortController | null = null

const isZip = computed(() => file.value?.name.toLowerCase().endsWith('.zip') ?? false)
const lowConfidence = computed(() => (preview.value?.confidence ?? 1) < 0.6)
const readyToCommit = computed(() => Boolean(
  file.value && preview.value && !busy.value && !result.value && !aiCommitResult.value &&
  lastPreviewFile === file.value && lastPreviewPassword === alipayPassword.value &&
  (!lowConfidence.value || confirmedLowConfidence.value),
))
const selectedRowsCount = computed(() => approvedRowIndices.value.length)

function resetForChange() {
  previewController?.abort()
  previewController = null
  busy.value = null
  preview.value = null
  result.value = null
  aiCommitResult.value = null
  aiSessionId.value = ''
  aiAnalysis.value = null
  aiMessages.value = []
  chatMessage.value = ''
  approvedCategoryPaths.value = []
  approvedRowIndices.value = []
  rememberRuleRowIndices.value = []
  error.value = ''
  confirmedLowConfidence.value = false
  lastPreviewFile = null
  lastPreviewPassword = ''
}

function selectFile(selected: File | null) {
  resetForChange()
  file.value = selected
  alipayPassword.value = ''
  if (selected && !/\.(xlsx|csv|zip)$/i.test(selected.name)) {
    error.value = '仅支持微信 XLSX、支付宝 CSV / ZIP 账单文件。'
    file.value = null
  } else if (selected && selected.size > MAX_UPLOAD_BYTES) {
    error.value = '文件不能超过 25 MB。'
    file.value = null
  } else if (selected && selected.size === 0) {
    error.value = '文件为空，请重新选择。'
    file.value = null
  }
}

function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  selectFile(input.files?.[0] ?? null)
  input.value = ''
}
function openFilePicker() { if (busy.value !== 'commit') fileInput.value?.click() }
function onDrop(event: DragEvent) {
  dragActive.value = false
  if (busy.value === 'commit') return
  selectFile(event.dataTransfer?.files?.[0] ?? null)
}
function onPasswordInput(event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value !== alipayPassword.value) { resetForChange(); alipayPassword.value = value }
}

async function loadHistory() {
  historyBusy.value = true
  historyError.value = ''
  try { history.value = (await getImportHistory()).batches }
  catch (err) { historyError.value = err instanceof Error ? err.message : '无法加载导入历史' }
  finally { historyBusy.value = false }
}

async function loadPendingProposals() {
  try {
    const response = await getAIProposals('draft', 50)
    pendingProposals.value = response.proposals
    if (activeProposal.value && !response.proposals.some(item => item.id === activeProposal.value?.id)) {
      activeProposal.value = null
    }
  } catch {
    pendingProposals.value = []
  }
}

async function openProposal(proposalId: string) {
  reviewBusy.value = true
  reviewError.value = ''
  try {
    activeProposal.value = await getAIProposal(proposalId)
    resolutionTargets.value = {}
    resolutionAnswers.value = {}
    resolutionRemember.value = {}
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : '无法读取分类草案'
  } finally { reviewBusy.value = false }
}

async function resolveProposalItem(item: AIProposalSuggestion) {
  const proposal = activeProposal.value
  if (!proposal) return
  const targetNodeId = resolutionTargets.value[item.tx_id]
  if (!targetNodeId) { reviewError.value = '请先选择一个目标分类。'; return }
  const userAnswer = resolutionAnswers.value[item.tx_id]?.trim() || `用户确认归入 ${proposal.available_categories.find(x => x.node_id === targetNodeId)?.path || '所选分类'}`
  const field = item.counterparty ? 'counterparty' : item.item ? 'item' : undefined
  const pattern = item.counterparty || item.item || undefined
  reviewBusy.value = true
  reviewError.value = ''
  try {
    activeProposal.value = await resolveAIProposal(proposal.id, {
      tx_id: item.tx_id, target_node_id: targetNodeId, confidence: item.confidence,
      reason: item.reason || '用户通过网页审核完成澄清。', user_answer: userAnswer,
      remember_rule: Boolean(resolutionRemember.value[item.tx_id] && field && pattern),
      rule_field: resolutionRemember.value[item.tx_id] ? field : undefined,
      rule_pattern: resolutionRemember.value[item.tx_id] ? pattern : undefined,
      expected_seq: proposal.current_seq,
    })
    reviewError.value = ''
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : '澄清保存失败'
  } finally { reviewBusy.value = false }
}

async function confirmActiveProposal() {
  const proposal = activeProposal.value
  if (!proposal) return
  const pending = proposal.suggestions.filter(item => item.status === 'pending')
  if (pending.some(item => item.needs_clarification)) {
    reviewError.value = '请先完成所有待澄清交易的分类选择，再确认该草案。'
    return
  }
  const ids = pending.map(item => item.tx_id)
  if (!ids.length) { reviewError.value = '该草案没有可确认的待处理交易。'; return }
  reviewBusy.value = true
  reviewError.value = ''
  try {
    await confirmAIProposal(proposal.id, ids, proposal.current_seq)
    activeProposal.value = null
    await loadPendingProposals()
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : '确认分类失败'
  } finally { reviewBusy.value = false }
}

async function rejectActiveProposal() {
  const proposal = activeProposal.value
  if (!proposal) return
  reviewBusy.value = true
  reviewError.value = ''
  try {
    await rejectAIProposal(proposal.id, '用户在网页审核工作区拒绝该批分类建议')
    activeProposal.value = null
    await loadPendingProposals()
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : '拒绝分类草案失败'
  } finally { reviewBusy.value = false }
}

async function loadAISettings() {
  try { aiSettings.value = await getAISettings() }
  catch { aiSettings.value = null }
}

async function startPreview(forceLegacy = false) {
  const selected = file.value
  if (!selected || busy.value) return
  const password = alipayPassword.value
  const controller = new AbortController()
  previewController = controller
  preview.value = null
  result.value = null
  aiCommitResult.value = null
  aiSessionId.value = ''
  aiAnalysis.value = null
  aiMessages.value = []
  chatMessage.value = ''
  approvedCategoryPaths.value = []
  approvedRowIndices.value = []
  rememberRuleRowIndices.value = []
  error.value = ''
  busy.value = 'preview'
  confirmedLowConfidence.value = false
  try {
    if (aiSettings.value?.configured && aiSettings.value.allow_external_processing && !forceLegacy) {
      const response = await analyzeAIImport(selected, password, controller.signal)
      if (previewController !== controller || file.value !== selected || alipayPassword.value !== password) return
      aiSessionId.value = response.session_id
      aiAnalysis.value = response.analysis
      preview.value = {
        batch_id: response.session_id,
        platform_id: response.platform_id,
        platform_name: response.platform_name,
        confidence: response.platform_confidence,
        total: response.total,
        by_status: response.by_status,
        by_direction: response.by_direction,
        date_from: response.date_from,
        date_to: response.date_to,
        amount_sum: response.amount_sum,
        sample: response.sample,
        warnings: response.warnings,
      }
      aiMessages.value = [{ role: 'assistant', content: '我已经分析整批交易。你可以直接告诉我哪些商户或交易需要调整分类。', created_at: new Date().toISOString() }]
    } else {
      preview.value = await previewImport(selected, password, controller.signal)
    }
    if (previewController !== controller || file.value !== selected || alipayPassword.value !== password) return
    lastPreviewFile = selected
    lastPreviewPassword = password
  } catch (err) {
    if (controller.signal.aborted) return
    error.value = err instanceof Error ? err.message : '账单预览失败'
  } finally {
    if (previewController === controller) { previewController = null; busy.value = null }
  }
}

function toggleNumber(list: number[], value: number, checked: boolean) {
  const index = list.indexOf(value)
  if (checked && index === -1) {
    list.push(value)
  } else if (!checked && index !== -1) {
    list.splice(index, 1)
  }
}
function togglePath(path: string, checked: boolean) {
  approvedCategoryPaths.value = checked
    ? [...new Set([...approvedCategoryPaths.value, path])]
    : approvedCategoryPaths.value.filter(item => item !== path)
}

async function sendCalibration() {
  const message = chatMessage.value.trim()
  if (!message || !aiSessionId.value || chatBusy.value || aiCommitResult.value) return
  chatBusy.value = true
  error.value = ''
  try {
    const response = await calibrateAIImport(aiSessionId.value, message)
    aiAnalysis.value = response.analysis
    aiMessages.value = response.messages
    chatMessage.value = ''
    approvedRowIndices.value = []
    rememberRuleRowIndices.value = []
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'AI 校准失败'
  } finally { chatBusy.value = false }
}

async function commit() {
  if (!readyToCommit.value || !file.value) return
  busy.value = 'commit'
  error.value = ''
  try {
    if (aiSessionId.value) {
      aiCommitResult.value = await commitAIImport(aiSessionId.value, {
        approved_category_paths: approvedCategoryPaths.value,
        approved_row_indices: approvedRowIndices.value,
        remember_rule_row_indices: rememberRuleRowIndices.value,
      })
    } else {
      result.value = await commitImport(file.value, alipayPassword.value)
    }
    alipayPassword.value = ''
    lastPreviewFile = null
    lastPreviewPassword = ''
    await loadHistory()
    await loadPendingProposals()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认导入失败'
  } finally { busy.value = null }
}

onMounted(async () => { await Promise.all([loadHistory(), loadAISettings(), loadPendingProposals()]) })
</script>

<template>
  <div class="import-view">
    <section class="card">
      <h2>导入账单</h2>
      <p class="hint">支持微信支付 XLSX、支付宝 CSV / ZIP。配置 AI 后，系统会先分析整批交易并生成分类建议，再由你审核导入。</p>
      <div class="ai-config-banner" v-if="aiSettings?.configured && aiSettings.allow_external_processing">
        <span class="ai-dot"></span><span>AI 已配置：{{ aiSettings.provider_label }} · {{ aiSettings.model }}</span>
        <RouterLink to="/ai/settings">模型设置</RouterLink>
      </div>
      <div class="ai-config-banner needs-config" v-else-if="aiSettings?.configured">
        <span>模型已保存，但尚未授权将必要交易字段发送到模型接口。当前仍可普通导入。</span>
        <RouterLink to="/ai/settings">查看数据授权</RouterLink>
      </div>
      <div class="ai-config-banner needs-config" v-else>
        <span>尚未配置 AI 模型。你仍可按原方式导入账单。</span>
        <RouterLink to="/ai/settings">配置网页 AI</RouterLink>
      </div>

      <label class="input-label" for="bill-file">账单文件</label>
      <input ref="fileInput" id="bill-file" class="file-input" type="file" accept=".xlsx,.csv,.zip" :disabled="busy === 'commit'" @change="onFile" />
      <div
        class="drop-zone"
        :class="{ active: dragActive, disabled: busy === 'commit' }"
        role="button"
        tabindex="0"
        @click="openFilePicker"
        @keydown.enter.prevent="openFilePicker"
        @keydown.space.prevent="openFilePicker"
        @dragenter.prevent="dragActive = true"
        @dragover.prevent="dragActive = true"
        @dragleave.prevent="dragActive = false"
        @drop.prevent="onDrop"
      >
        <span class="drop-icon">⇩</span>
        <strong>把账单文件拖到这里</strong>
        <small>也可以点击选择文件 · 支持 XLSX、CSV、ZIP · 最大 25 MB</small>
      </div>
      <p v-if="file" class="selected-file">已选择：{{ file.name }}（{{ (file.size / 1024).toFixed(1) }} KB）</p>

      <div v-if="isZip" class="password-field">
        <label class="input-label" for="alipay-password">支付宝 ZIP 密码（没有密码可留空）</label>
        <input id="alipay-password" type="password" autocomplete="off" :value="alipayPassword" :disabled="busy === 'commit'" @input="onPasswordInput" />
        <p class="hint">密码只用于本次上传，不会保存在页面或浏览器存储中。</p>
      </div>

      <button class="primary" type="button" :disabled="!file || !!busy" @click="startPreview()">
        {{ busy === 'preview' ? '正在解析…' : '预览账单' }}
      </button>
      <button v-if="aiSettings?.configured && aiSettings.allow_external_processing" class="secondary legacy-preview" type="button" :disabled="!file || !!busy" @click="startPreview(true)">
        不使用 AI，按普通模式预览
      </button>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>

    <section v-if="preview" class="card" aria-live="polite">
      <h3>账单预览 <span class="muted">（尚未写入数据库）</span></h3>
      <dl class="summary">
        <dt>识别平台</dt><dd>{{ preview.platform_name }}</dd>
        <dt>有效交易</dt><dd>{{ preview.total }} 笔</dd>
        <dt>日期范围</dt><dd>{{ preview.date_from }} — {{ preview.date_to }}</dd>
        <dt>支出合计</dt><dd>¥{{ preview.amount_sum.toFixed(2) }}（仅供参考）</dd>
      </dl>
      <div v-if="Object.keys(preview.by_direction).length" class="chips">
        <span v-for="(count, direction) in preview.by_direction" :key="direction" class="chip">
          {{ direction }}：{{ count }} 笔
        </span>
      </div>
      <div v-if="preview.warnings.length" class="warnings" role="status">
        <strong>解析提示</strong>
        <p v-for="(warning, index) in preview.warnings" :key="index">{{ warning }}</p>
      </div>
      <details v-if="preview.sample.length" class="sample-details">
        <summary>查看前 {{ preview.sample.length }} 行原始样例（包含交易信息）</summary>
        <div class="sample-table-wrap">
          <table class="sample-table">
            <thead>
              <tr><th v-for="key in Object.keys(preview.sample[0])" :key="key">{{ key }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(item, index) in preview.sample" :key="index">
                <td v-for="key in Object.keys(preview.sample[0])" :key="key">{{ item[key] ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      <section v-if="aiAnalysis && aiSessionId && !aiCommitResult" class="ai-analysis-panel">
        <div class="ai-panel-heading">
          <div><p class="ai-kicker">AI BATCH ANALYSIS</p><h3>整批智能分类结果</h3><p class="hint">{{ aiAnalysis.summary }}</p></div>
          <span class="ai-count">{{ aiAnalysis.suggestions.length }} 条建议</span>
        </div>
        <div class="ai-stats">
          <div><strong>{{ aiAnalysis.counts.auto_apply }}</strong><span>命中已授权规则</span></div>
          <div><strong>{{ aiAnalysis.counts.review }}</strong><span>等待审核</span></div>
          <div><strong>{{ aiAnalysis.counts.excluded }}</strong><span>特殊交易排除</span></div>
          <div><strong>{{ aiAnalysis.counts.new_categories }}</strong><span>建议新增分类</span></div>
        </div>

        <div v-if="aiAnalysis.taxonomy.length" class="taxonomy-block">
          <div class="subheading"><h4>AI 建议分类体系</h4><span>新增分类需要你批准后才会创建</span></div>
          <div class="taxonomy-list">
            <label v-for="category in aiAnalysis.taxonomy" :key="category.path" class="taxonomy-item">
              <input v-if="category.is_new" type="checkbox" :checked="approvedCategoryPaths.includes(category.path)" @change="togglePath(category.path, ($event.target as HTMLInputElement).checked)" />
              <span v-else class="existing-mark">✓</span>
              <span class="taxonomy-name">{{ category.path }}</span>
              <span class="taxonomy-description">{{ category.description }}</span>
              <span class="new-tag" v-if="category.is_new">待批准新增</span>
              <span class="existing-tag" v-else>已有分类</span>
            </label>
          </div>
        </div>

        <div class="subheading suggestions-heading"><h4>逐笔分类建议</h4><span>勾选后随本次导入确认；不勾选的建议会保留待审核草案</span></div>
        <div class="suggestion-table-wrap">
          <table class="suggestion-table">
            <thead><tr><th>确认</th><th>交易</th><th>金额</th><th>AI 建议分类</th><th>置信度</th><th>状态 / 理由</th><th>长期规则</th></tr></thead>
            <tbody>
              <tr v-for="suggestion in aiAnalysis.suggestions" :key="suggestion.row_index" :class="{ excluded: suggestion.excluded }">
                <td><input type="checkbox" :disabled="suggestion.excluded || !suggestion.target_path || (suggestion.is_new_category && !approvedCategoryPaths.includes(suggestion.target_path))" :checked="approvedRowIndices.includes(suggestion.row_index)" @change="toggleNumber(approvedRowIndices, suggestion.row_index, ($event.target as HTMLInputElement).checked)" /></td>
                <td><b>{{ suggestion.counterparty || '未知商户' }}</b><small>{{ suggestion.item || '无商品描述' }}</small></td>
                <td>¥{{ Number(suggestion.effective_amount ?? suggestion.amount ?? 0).toFixed(2) }}<small v-if="(suggestion.refunded_amount ?? 0) > 0">已退款 ¥{{ Number(suggestion.refunded_amount).toFixed(2) }}</small></td>
                <td>{{ suggestion.target_path || '待判断' }}</td>
                <td>{{ Math.round(suggestion.confidence * 100) }}%</td>
                <td><span v-if="suggestion.auto_apply_eligible" class="status-tag auto">规则自动落账</span><span v-else-if="suggestion.excluded" class="status-tag excluded-tag">特殊交易</span><span v-else class="status-tag review">待审核</span><small>{{ suggestion.reason }}</small></td>
                <td><label v-if="!suggestion.excluded && approvedRowIndices.includes(suggestion.row_index) && suggestion.target_path" class="remember-choice"><input type="checkbox" :checked="rememberRuleRowIndices.includes(suggestion.row_index)" @change="toggleNumber(rememberRuleRowIndices, suggestion.row_index, ($event.target as HTMLInputElement).checked)" />保存</label><span v-else class="muted">—</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint">已选择 {{ selectedRowsCount }} 笔立即确认。只有命中唯一启用的用户规则且已开启自动规则授权的交易，才会自动落账；模型单独给出的高置信度不会绕过审核。</p>

        <div class="calibration-chat">
          <div class="subheading"><h4>通过对话校准本批次</h4><span>校准只修改本批次建议，不会直接写入账本</span></div>
          <div class="chat-history">
            <div v-for="(message, index) in aiMessages" :key="index" class="chat-message" :class="message.role">
              <b>{{ message.role === 'user' ? '你' : 'AI 助手' }}</b><p>{{ message.content }}</p>
            </div>
          </div>
          <div class="chat-compose">
            <textarea v-model="chatMessage" rows="2" placeholder="例如：Amazon 的电子产品归入数码产品，Walmart 暂时都不要自动分类。" @keydown.ctrl.enter.prevent="sendCalibration"></textarea>
            <button class="secondary" type="button" :disabled="chatBusy || !chatMessage.trim()" @click="sendCalibration">{{ chatBusy ? '分析中…' : '发送校准指令' }}</button>
          </div>
        </div>
      </section>

      <label v-if="lowConfidence" class="low-confidence">
        <input v-model="confirmedLowConfidence" type="checkbox" />
        平台识别置信度较低（{{ preview.confidence.toFixed(2) }}），我已确认识别的平台正确。
      </label>
      <button class="primary" type="button" :disabled="!readyToCommit" @click="commit">
        {{ busy === 'commit' ? '正在写入数据库…' : (aiSessionId ? '确认导入并应用已选分类' : '确认导入数据库') }}
      </button>
      <p class="hint">AI 模式会使用本次暂存会话提交，不会重新上传文件；重复交易会跳过，不会修改原始交易记录。</p>
    </section>

    <section v-if="aiCommitResult" class="card ai-result-card" aria-live="polite">
      <h3>AI 智能导入完成</h3>
      <div class="result-grid">
        <div><strong>{{ aiCommitResult.inserted }}</strong><span>新增交易</span></div>
        <div><strong>{{ aiCommitResult.auto_applied }}</strong><span>规则自动归类</span></div>
        <div><strong>{{ aiCommitResult.pending_review }}</strong><span>待审核建议</span></div>
      </div>
      <p class="import-info">{{ aiCommitResult.message }}</p>
      <p class="muted">重复 {{ aiCommitResult.skipped }} 笔 · 特殊交易排除 {{ aiCommitResult.excluded_special }} 笔 · 批次号：{{ aiCommitResult.batch_id }}</p>
      <div v-if="aiCommitResult.pending_proposal_ids.length" class="pending-proposals">
        <b>待审核分类草案</b><code v-for="id in aiCommitResult.pending_proposal_ids" :key="id">{{ id }}</code>
        <p class="hint">当前版本已保存待审核草案。下一阶段会把这些草案直接呈现在网页审核工作区。</p>
      </div>
    </section>

    <section v-if="pendingProposals.length" class="card review-workspace">
      <div class="history-title"><div><h3>待审核分类</h3><p class="hint">低置信度建议和需要澄清的交易会保留在这里，不会自动落账。</p></div><button class="secondary" type="button" :disabled="reviewBusy" @click="loadPendingProposals">刷新</button></div>
      <div class="proposal-list">
        <button v-for="proposal in pendingProposals" :key="proposal.id" class="proposal-item" :class="{ active: activeProposal?.id === proposal.id }" type="button" @click="openProposal(proposal.id)">
          <span><b>{{ proposal.id }}</b><small>{{ proposal.created_by }} · {{ proposal.created_at }}</small></span>
          <span class="proposal-status">{{ proposal.suggestion_count }} 条建议</span>
        </button>
      </div>
      <div v-if="activeProposal" class="proposal-detail">
        <div class="subheading"><h4>分类审核详情</h4><span>账本版本 {{ activeProposal.current_seq }}</span></div>
        <p v-if="activeProposal.stale" class="hint">账本在草案生成后有其他变化。确认前，服务器会检查本草案涉及的交易和目标分类是否仍安全；若相关交易已变化，将拒绝提交。</p>
        <div class="proposal-suggestions">
          <article v-for="item in activeProposal.suggestions.filter(x => x.status === 'pending')" :key="item.tx_id" class="proposal-suggestion">
            <div class="proposal-tx"><b>{{ item.counterparty || '未知商户' }}</b><span>¥{{ Number(item.amount || 0).toFixed(2) }}</span><small>{{ item.item }} · 当前：{{ item.current_path }}</small></div>
            <div v-if="item.needs_clarification" class="resolve-controls">
              <p class="question-text">{{ item.question || '请选择这笔交易的分类。' }}</p>
              <select v-model="resolutionTargets[item.tx_id]">
                <option value="">选择目标分类</option>
                <option v-for="category in activeProposal.available_categories" :key="category.node_id" :value="category.node_id">{{ category.path }}</option>
              </select>
              <input v-model="resolutionAnswers[item.tx_id]" type="text" placeholder="补充说明（可选）" />
              <label class="remember-choice"><input v-model="resolutionRemember[item.tx_id]" type="checkbox" />将本次选择保存为长期规则</label>
              <button class="secondary" type="button" :disabled="reviewBusy" @click="resolveProposalItem(item)">保存澄清</button>
            </div>
            <div v-else class="proposal-ready">
              <span><b>建议：</b>{{ item.target_path }} · {{ Math.round(item.confidence * 100) }}%</span>
              <small>{{ item.reason }}</small>
            </div>
          </article>
        </div>
        <p v-if="reviewError" class="error" role="alert">{{ reviewError }}</p>
        <p class="hint">确认按钮会一次性确认该草案中所有尚未处理的建议；如有待澄清项，请先逐笔完成澄清。</p>
        <div class="actions-row">
          <button class="primary" type="button" :disabled="reviewBusy || activeProposal.suggestions.some(x => x.status === 'pending' && x.needs_clarification)" @click="confirmActiveProposal">确认全部可处理建议</button>
          <button class="secondary" type="button" :disabled="reviewBusy" @click="rejectActiveProposal">拒绝整份草案</button>
        </div>
      </div>
      <p v-if="reviewError && !activeProposal" class="error">{{ reviewError }}</p>
    </section>

    <section v-if="result" class="card" aria-live="polite">
      <h3>导入完成</h3>
      <div class="result-grid">
        <div><strong>{{ result.inserted }}</strong><span>新增交易</span></div>
        <div><strong>{{ result.skipped }}</strong><span>跳过重复</span></div>
        <div><strong>{{ result.failed }}</strong><span>解析失败</span></div>
      </div>
      <p v-if="result.inserted === 0 && result.skipped > 0 && result.failed === 0" class="import-info">
        该账单已导入过，本次没有新增交易，{{ result.skipped }} 笔重复记录均已自动跳过，数据库中的原始记录未被修改。
      </p>
      <p v-else-if="result.skipped > 0 && result.failed === 0" class="import-info">
        已自动跳过 {{ result.skipped }} 笔重复交易，不会修改数据库中的原始记录。
      </p>
      <p v-if="result.failed > 0" class="warnings">
        有 {{ result.failed }} 笔交易处理失败，请核查账单内容及错误信息。
      </p>
      <p v-if="result.first_error" class="error">首个错误：{{ result.first_error }}</p>
      <p class="muted">批次号：{{ result.batch_id }} · {{ result.date_from }} — {{ result.date_to }}</p>
    </section>

    <section class="card">
      <div class="history-title">
        <h3>导入历史</h3>
        <button class="secondary" type="button" :disabled="historyBusy" @click="loadHistory">
          {{ historyBusy ? '加载中…' : '刷新' }}
        </button>
      </div>
      <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
      <p v-else-if="!historyBusy && !history.length" class="hint">暂无导入记录。</p>
      <div v-if="history.length" class="history-wrap">
        <table class="history-table">
          <thead><tr><th>时间</th><th>平台</th><th>文件</th><th>新增</th><th>重复</th><th>失败</th></tr></thead>
          <tbody>
            <tr v-for="item in history" :key="item.batch_id">
              <td>{{ item.created_at }}</td>
              <td>{{ item.platform_name || item.platform_id }}</td>
              <td class="filename" :title="item.filename || ''">{{ item.filename || '—' }}</td>
              <td>{{ item.inserted }}</td>
              <td>{{ item.skipped }}</td>
              <td>{{ item.failed }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.import-view { max-width: 1060px; margin: 0 auto; }
.ai-config-banner { display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin:12px 0; padding:10px 12px; border-radius:9px; background:#edf8f1; color:#246847; font-size:13px; }
.ai-config-banner a { margin-left:auto; color:#286cae; text-decoration:none; font-weight:650; }.ai-config-banner.needs-config { background:#f4f6f9; color:#687487; }.ai-dot { width:8px; height:8px; border-radius:50%; background:#20a366; }
.ai-analysis-panel { margin:20px 0; padding:18px; border:1px solid #d9e5f1; border-radius:13px; background:#fbfdff; }
.ai-panel-heading,.subheading { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }.ai-panel-heading h3 { margin:3px 0 4px; }.ai-kicker { font-size:10px; letter-spacing:.12em; color:#5680aa; font-weight:700; }.ai-count { white-space:nowrap; border-radius:20px; background:#edf3fa; padding:5px 9px; color:#48647f; font-size:12px; }
.ai-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin:15px 0; }.ai-stats div { padding:11px; border-radius:9px; background:#f0f4f9; }.ai-stats strong,.ai-stats span { display:block; }.ai-stats strong { font-size:22px; }.ai-stats span { color:var(--color-muted); font-size:11px; margin-top:3px; }
.taxonomy-block { margin:18px 0; }.subheading { align-items:center; margin:15px 0 9px; }.subheading h4 { font-size:14px; }.subheading span { color:var(--color-muted); font-size:11px; text-align:right; }.taxonomy-list { display:grid; grid-template-columns:1fr 1fr; gap:7px; }.taxonomy-item { display:flex; align-items:center; gap:7px; min-width:0; padding:9px; border:1px solid var(--color-border); border-radius:8px; background:white; }.taxonomy-name { font-weight:650; }.taxonomy-description { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--color-muted); font-size:11px; }.new-tag,.existing-tag { flex:none; margin-left:auto; padding:3px 6px; border-radius:12px; font-size:10px; }.new-tag { color:#98610d; background:#fff5dc; }.existing-tag { color:#32714e; background:#edf7f0; }.existing-mark { color:#238454; font-weight:700; }
.suggestion-table-wrap { max-height:460px; overflow:auto; border:1px solid var(--color-border); border-radius:9px; }.suggestion-table { min-width:850px; }.suggestion-table td { vertical-align:top; white-space:normal; max-width:220px; }.suggestion-table td:first-child { width:40px; }.suggestion-table td small { display:block; color:var(--color-muted); font-size:10px; margin-top:4px; line-height:1.45; }.suggestion-table tr.excluded { opacity:.58; }.status-tag { display:inline-block; padding:3px 7px; border-radius:12px; font-size:10px; white-space:nowrap; }.status-tag.auto { color:#176b43; background:#e7f6ed; }.status-tag.review { color:#8d5a0a; background:#fff4d9; }.status-tag.excluded-tag { color:#6c7582; background:#eef0f3; }.remember-choice { display:flex; gap:4px; align-items:center; white-space:nowrap; font-size:11px; }
.calibration-chat { margin-top:20px; border-top:1px solid var(--color-border); padding-top:8px; }.chat-history { display:flex; flex-direction:column; gap:8px; max-height:260px; overflow:auto; padding:9px; background:#f4f7fb; border-radius:9px; }.chat-message { max-width:90%; padding:9px 11px; border-radius:9px; background:white; align-self:flex-start; }.chat-message.user { align-self:flex-end; background:#e8f1fb; }.chat-message b { display:block; font-size:11px; color:#52718f; margin-bottom:4px; }.chat-message p { line-height:1.55; white-space:pre-wrap; }.chat-compose { display:flex; gap:8px; margin-top:9px; align-items:stretch; }.chat-compose textarea { flex:1; min-width:0; padding:9px; border:1px solid var(--color-border); border-radius:8px; resize:vertical; }.chat-compose button { flex:none; align-self:flex-end; }.pending-proposals { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:12px; }.pending-proposals code { font-size:10px; background:#f0f3f7; padding:4px 6px; border-radius:5px; }
.review-workspace { margin-top:16px; }.proposal-list { display:flex; flex-direction:column; gap:7px; }.proposal-item { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px; border:1px solid var(--color-border); border-radius:8px; background:white; text-align:left; color:var(--color-text); }.proposal-item.active { border-color:#6c98c2; background:#f4f8fc; }.proposal-item span:first-child,.proposal-item small { display:block; }.proposal-item small { color:var(--color-muted); margin-top:3px; font-size:11px; }.proposal-status { color:#526d88; font-size:12px; white-space:nowrap; }.proposal-detail { margin-top:16px; border-top:1px solid var(--color-border); padding-top:8px; }.proposal-suggestions { display:flex; flex-direction:column; gap:9px; }.proposal-suggestion { border:1px solid var(--color-border); border-radius:9px; padding:11px; display:grid; grid-template-columns:minmax(180px,1fr) minmax(250px,1.2fr); gap:12px; }.proposal-tx b,.proposal-tx span { display:inline-block; margin-right:10px; }.proposal-tx small,.proposal-ready small { display:block; color:var(--color-muted); font-size:11px; margin-top:5px; }.resolve-controls { display:flex; flex-direction:column; gap:7px; }.resolve-controls select,.resolve-controls input { min-height:36px; padding:7px 9px; border:1px solid var(--color-border); border-radius:7px; background:white; }.question-text { font-size:12px; color:#815e22; }.proposal-ready { display:flex; flex-direction:column; gap:4px; }.actions-row { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }

h2 { font-size: var(--font-xl); margin-bottom: var(--space-2); }
h3 { font-size: var(--font-lg); margin-bottom: var(--space-3); }
.hint, .muted { color: var(--color-muted); }
.hint { margin: var(--space-2) 0 var(--space-3); line-height: 1.6; }
.input-label { font-weight: 600; display: block; margin: var(--space-3) 0 var(--space-2); }
.file-input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.drop-zone { min-height: 150px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: var(--space-4); border: 2px dashed #bdcce0; border-radius: 18px; background: #f7faff; color: #53657c; text-align: center; cursor: pointer; transition: .18s ease; }
.drop-zone:hover, .drop-zone.active { border-color: var(--chart-1); background: #edf5ff; transform: translateY(-1px); }
.drop-zone.disabled { opacity: .55; cursor: not-allowed; transform: none; }
.drop-zone small { color: var(--color-muted); }
.drop-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 13px; background: #deecfc; color: var(--chart-1); font-size: 25px; font-weight: 700; }
input[type='password'] { width: min(100%, 380px); padding: var(--space-2); border: 1px solid var(--color-border); border-radius: var(--radius); }
.password-field { margin: var(--space-3) 0; }
.selected-file { color: var(--color-text); margin-top: var(--space-2); overflow-wrap: anywhere; }
button { padding: var(--space-2) var(--space-3); border-radius: var(--radius); font-weight: 600; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }
.primary { border: none; background: var(--chart-1); color: white; margin-top: var(--space-3); }
.secondary { border: 1px solid var(--color-border); color: var(--color-text); background: white; }.legacy-preview { margin-left: 8px; margin-top: var(--space-3); }
.error { color: var(--color-expense); margin-top: var(--space-3); overflow-wrap: anywhere; }
.summary { display: grid; grid-template-columns: 105px 1fr; gap: var(--space-2); }
.summary dt { color: var(--color-muted); }
.summary dd { overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-3) 0; }
.chip { padding: 4px 10px; border-radius: 20px; background: #eff4fa; }
.import-info { color: #20734a; margin: var(--space-3) 0; line-height: 1.6; }
.warnings { color: #a15d11; margin: var(--space-3) 0; line-height: 1.6; }
.warnings p { margin-top: 4px; overflow-wrap: anywhere; }
.sample-details { margin: var(--space-3) 0; }
.sample-details summary { cursor: pointer; color: var(--chart-1); }
.sample-table-wrap, .history-wrap { max-width: 100%; overflow: auto; margin-top: var(--space-2); }
table { border-collapse: collapse; width: 100%; font-size: var(--font-xs); }
th, td { border-bottom: 1px solid var(--color-border); padding: var(--space-2); text-align: left; white-space: nowrap; }
th { color: var(--color-muted); font-weight: 600; }
.filename { max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
.low-confidence { display: flex; align-items: flex-start; gap: 8px; margin: var(--space-3) 0; color: var(--color-expense); }
.result-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-2); }
.result-grid > div { background: var(--color-bg); border-radius: var(--radius); padding: var(--space-3); }
.result-grid strong { display: block; font-size: var(--font-xl); }
.result-grid span { display: block; color: var(--color-muted); margin-top: var(--space-1); }
.history-title { display: flex; align-items: center; justify-content: space-between; }
@media (max-width: 600px) { .result-grid { grid-template-columns: 1fr; } .summary { grid-template-columns: 94px 1fr; } .ai-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }.taxonomy-list { grid-template-columns:1fr; }.chat-compose { flex-direction:column; }.chat-compose button { align-self:stretch; }.proposal-suggestion { grid-template-columns:1fr; } }
</style>
