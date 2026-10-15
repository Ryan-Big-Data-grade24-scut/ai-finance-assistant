<script setup lang="ts">
/**
 * ImportView —— 导入页骨架
 *
 * 接口：
 *   POST /api/import/preview  → ImportPreviewResp
 *   POST /api/import/commit   → ImportCommitResp
 *   GET  /api/import/history  → ImportHistoryResp
 *
 * 流程：选文件 → preview（不入库）→ 用户确认 → commit（入库）
 *
 * ⚠️ commit 返回的 skipped 必须显示给用户：
 *    重复笔数明显异常时，用户需要人工核查（兜底 dedupe_key 有误判风险）。
 */
import { ref } from 'vue'

const file = ref<File | null>(null)
const preview = ref<any>(null)
const result = ref<any>(null)
const busy = ref(false)

const onFile = (event: Event) => {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

const commit = () => {
  // 导入功能由另一条开发线负责；此副本只保证页面可以独立构建。
}

// 接真实接口时打开：
// const onFile = async (e: Event) => {
//   file.value = (e.target as HTMLInputElement).files?.[0] ?? null
//   const fd = new FormData()
//   fd.append('file', file.value)
//   const res = await fetch('/api/import/preview', { method: 'POST', body: fd })
//   preview.value = await res.json()
// }
</script>

<template>
  <div class="import-view">
    <section class="card" data-component="file-picker" data-api="/api/import/preview">
      <h3>选择账单文件</h3>
      <p class="hint">支持：微信账单 .xlsx、支付宝 .csv / .zip、第三方平台账单</p>
      <input type="file" accept=".xlsx,.csv,.zip" @change="onFile" />
    </section>

    <section v-if="preview" class="card" data-api="/api/import/preview">
      <h3>预览（尚未入库）</h3>
      <dl>
        <dt>识别平台</dt>
        <dd>{{ preview.platform_name }}（置信度 {{ preview.confidence }}）</dd>
        <dt>解析行数</dt>
        <dd>{{ preview.total }}</dd>
        <dt>日期范围</dt>
        <dd>{{ preview.date_from }} ~ {{ preview.date_to }}</dd>
        <dt>支出合计</dt>
        <dd>¥{{ preview.amount_sum?.toFixed(2) }}</dd>
      </dl>
      <!-- confidence 低于 0.6 时应弹窗让用户手动确认平台 -->
      <div v-if="preview.warnings?.length" class="warnings">
        <p v-for="w in preview.warnings" :key="w">⚠️ {{ w }}</p>
      </div>
      <button :disabled="busy" @click="commit">确认导入</button>
    </section>

    <section v-if="result" class="card" data-api="/api/import/commit">
      <h3>导入结果</h3>
      <p>新增 <b>{{ result.inserted }}</b> 笔</p>
      <!-- ⚠️ skipped 必须显示，不要静默隐藏 -->
      <p>跳过重复 <b>{{ result.skipped }}</b> 笔</p>
      <p v-if="result.failed">解析失败 <b>{{ result.failed }}</b> 笔</p>
      <p v-if="result.first_error" class="error">{{ result.first_error }}</p>
    </section>

    <section class="card" data-component="history" data-api="/api/import/history">
      <h3>导入历史</h3>
    </section>
  </div>
</template>

<style scoped>
h3 { font-size: var(--font-md); margin-bottom: var(--space-3); }
.hint { color: var(--color-muted); margin-bottom: var(--space-2); }
dl { display: grid; grid-template-columns: 100px 1fr; gap: var(--space-2); }
dt { color: var(--color-muted); }
.warnings { color: var(--color-expense); margin: var(--space-2) 0; }
.error { color: var(--color-expense); }
button { padding: var(--space-2) var(--space-4); border: 0; border-radius: var(--radius); background: var(--chart-1); color: #fff; cursor: pointer; }
</style>
