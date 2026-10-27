<script setup lang="ts">
/**
 * 导入流程：选择文件 → 预览（不写入）→ 确认导入 → 历史记录。
 * 只使用 backend/main.py 已有的三个接口，不修改数据库或其他模块。
 */
import { computed, onMounted, ref } from 'vue'
import { commitImport, getImportHistory, previewImport } from '@/api/imports'
import type { ImportBatch, ImportCommitResp, ImportPreviewResp } from '@/types/import'

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024
const file = ref<File | null>(null)
const alipayPassword = ref('')
const preview = ref<ImportPreviewResp | null>(null)
const result = ref<ImportCommitResp | null>(null)
const history = ref<ImportBatch[]>([])
const busy = ref<'preview' | 'commit' | null>(null)
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
  file.value && preview.value && !busy.value && !result.value &&
  lastPreviewFile === file.value && lastPreviewPassword === alipayPassword.value &&
  (!lowConfidence.value || confirmedLowConfidence.value),
))

function resetForChange() {
  previewController?.abort()
  previewController = null
  busy.value = null
  preview.value = null
  result.value = null
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
  // 允许用户连续选择同一个文件，并重新触发 change。
  input.value = ''
}

function openFilePicker() {
  if (busy.value !== 'commit') fileInput.value?.click()
}

function onDrop(event: DragEvent) {
  dragActive.value = false
  if (busy.value === 'commit') return
  selectFile(event.dataTransfer?.files?.[0] ?? null)
}

function onPasswordInput(event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value !== alipayPassword.value) {
    resetForChange()
    alipayPassword.value = value
  }
}

async function loadHistory() {
  historyBusy.value = true
  historyError.value = ''
  try {
    const response = await getImportHistory()
    history.value = response.batches
  } catch (err) {
    historyError.value = err instanceof Error ? err.message : '无法加载导入历史'
  } finally {
    historyBusy.value = false
  }
}

async function startPreview() {
  const selected = file.value
  if (!selected || busy.value) return
  const password = alipayPassword.value
  const controller = new AbortController()
  previewController = controller
  preview.value = null
  result.value = null
  error.value = ''
  busy.value = 'preview'
  confirmedLowConfidence.value = false
  try {
    const response = await previewImport(selected, password, controller.signal)
    if (previewController !== controller || file.value !== selected || alipayPassword.value !== password) return
    preview.value = response
    lastPreviewFile = selected
    lastPreviewPassword = password
  } catch (err) {
    if (controller.signal.aborted) return
    error.value = err instanceof Error ? err.message : '账单预览失败'
  } finally {
    if (previewController === controller) {
      previewController = null
      busy.value = null
    }
  }
}

async function commit() {
  if (!readyToCommit.value || !file.value) return
  busy.value = 'commit'
  error.value = ''
  try {
    const response = await commitImport(file.value, alipayPassword.value)
    result.value = response
    // 成功后不再保留密码；重新导入须重新预览。
    alipayPassword.value = ''
    lastPreviewFile = null
    lastPreviewPassword = ''
    await loadHistory()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认导入失败'
  } finally {
    busy.value = null
  }
}

onMounted(loadHistory)
</script>

<template>
  <div class="import-view">
    <section class="card">
      <h2>导入账单</h2>
      <p class="hint">支持微信支付 XLSX、支付宝 CSV / ZIP。先预览，确认后才会写入数据库。</p>

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

      <button class="primary" type="button" :disabled="!file || !!busy" @click="startPreview">
        {{ busy === 'preview' ? '正在解析…' : '预览账单' }}
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
      <label v-if="lowConfidence" class="low-confidence">
        <input v-model="confirmedLowConfidence" type="checkbox" />
        平台识别置信度较低（{{ preview.confidence.toFixed(2) }}），我已确认识别的平台正确。
      </label>
      <button class="primary" type="button" :disabled="!readyToCommit" @click="commit">
        {{ busy === 'commit' ? '正在写入数据库…' : '确认导入数据库' }}
      </button>
      <p class="hint">确认后同一文件会重新上传；重复交易会跳过，不会修改原始交易记录。</p>
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
.secondary { border: 1px solid var(--color-border); color: var(--color-text); background: white; }
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
@media (max-width: 600px) { .result-grid { grid-template-columns: 1fr; } .summary { grid-template-columns: 94px 1fr; } }
</style>
