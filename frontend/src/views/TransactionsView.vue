<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getTransactions, getTxDetail } from '@/api/reports'
import type { PaginatedResp, Period, ReportParams, TxDetailResp, TxType } from '@/types/report'

const route = useRoute()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const result = ref<PaginatedResp>({ total: 0, page: 1, size: 50, items: [] })
const detail = ref<TxDetailResp | null>(null)

const money = (value: number) => new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
const asText = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback
const queryId = computed(() => Number(route.query.id || 0))
const title = computed(() => {
  if (detail.value) return '交易详情'
  return asText(route.query.subcat) || asText(route.query.category) || asText(route.query.pay_method) || (asText(route.query.date) ? `${route.query.date} 的流水` : '交易明细')
})
const filterChips = computed(() => [
  asText(route.query.date), asText(route.query.category), asText(route.query.subcat), asText(route.query.pay_method),
].filter(Boolean))

function reportParams(): ReportParams {
  return {
    type: (route.query.type === 'income' ? 'income' : 'expense') as TxType,
    period: (['week', 'year'].includes(asText(route.query.period)) ? route.query.period : 'month') as Period,
    value: asText(route.query.value, '2026-10'),
  }
}

async function load() {
  loading.value = true
  error.value = ''
  detail.value = null
  try {
    if (queryId.value > 0) {
      detail.value = await getTxDetail(queryId.value)
      return
    }
    const date = asText(route.query.date)
    result.value = await getTransactions(reportParams(), {
      category: asText(route.query.category) || undefined,
      subcat: asText(route.query.subcat) || undefined,
      pay_method: asText(route.query.pay_method) || undefined,
      date_from: date || undefined,
      date_to: date || undefined,
      size: 100,
    })
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '交易加载失败'
  } finally {
    loading.value = false
  }
}

function openItem(id: number) {
  router.push({ name: 'transactions', query: { ...route.query, id } })
}

function categoryIcon(name: string) {
  const map: Record<string, string> = { 餐饮美食: '🍜', 交通出行: '🚕', 日用百货: '🛍️', 学习教育: '📚', 娱乐休闲: '🎬', 医疗健康: '🩺', 生活缴费: '💡', 数码电器: '💻', 住房: '🏠', 工资: '💼', 奖学金: '🎓', 转账红包: '🧧', 退款: '↩️' }
  return map[name] || '🧾'
}

watch(() => route.fullPath, load, { immediate: true })
</script>

<template>
  <div class="transactions-page">
    <header class="transactions-header">
      <button class="back" @click="router.back()">←</button>
      <div><span>Transactions</span><h1>{{ title }}</h1></div>
      <span v-if="!detail" class="count">{{ result.total }} 笔</span>
    </header>

    <div v-if="filterChips.length && !detail" class="filter-chips">
      <span v-for="chip in filterChips" :key="chip">{{ chip }}</span>
    </div>

    <div v-if="loading" class="state">正在加载流水…</div>
    <div v-else-if="error" class="state error">{{ error }}<button @click="load">重试</button></div>

    <template v-else-if="detail">
      <section class="detail-amount">
        <span>{{ detail.raw.direction === 'income' ? '收入' : '支出' }}</span>
        <h2>{{ detail.raw.direction === 'income' ? '+' : '-' }}¥{{ money(detail.raw.amount) }}</h2>
        <p>{{ detail.raw.counterparty || detail.raw.item || detail.raw.category }}</p>
      </section>
      <section class="detail-card">
        <dl>
          <div><dt>交易时间</dt><dd>{{ detail.raw.tx_time }}</dd></div>
          <div><dt>来源平台</dt><dd>{{ detail.raw.source }}</dd></div>
          <div><dt>支付方式</dt><dd>{{ detail.raw.pay_method || '—' }}</dd></div>
          <div><dt>原始分类</dt><dd>{{ detail.raw.category || '—' }}</dd></div>
          <div><dt>修正小类</dt><dd>{{ detail.raw.subcat || '未修正' }}</dd></div>
          <div><dt>资金来源</dt><dd>{{ detail.raw.funding }}</dd></div>
          <div><dt>统计状态</dt><dd>{{ detail.raw.is_exempt ? '已豁免' : '计入统计' }}</dd></div>
          <div><dt>备注</dt><dd>{{ detail.raw.note || '—' }}</dd></div>
        </dl>
        <p class="trace-note">原始记录保持只读；页面展示的是原始层与修正层合并后的结果。</p>
      </section>
    </template>

    <section v-else-if="result.items.length" class="transaction-card">
      <button v-for="item in result.items" :key="item.id" class="transaction-row" @click="openItem(item.id)">
        <span class="avatar">{{ categoryIcon(item.category) }}</span>
        <span class="main"><b>{{ item.counterparty || item.item || item.category }}</b><small>{{ item.tx_time }} · {{ item.source }} · {{ item.pay_method || '方式未知' }}</small><em>{{ item.category }}<template v-if="item.subcat"> / {{ item.subcat }}</template></em></span>
        <strong :class="{ income: item.direction === 'income' }">{{ item.direction === 'income' ? '+' : '-' }}¥{{ money(item.amount) }}</strong>
        <i>›</i>
      </button>
    </section>
    <div v-else class="state">没有符合当前条件的交易</div>
  </div>
</template>

<style scoped>
.transactions-page{width:min(100%,900px);margin:0 auto;padding-bottom:50px}.transactions-header{display:flex;align-items:center;gap:16px;padding:18px 0 22px}.back{width:42px;height:42px;border:0;border-radius:14px;background:#fff;color:#41516a;font-size:22px;cursor:pointer;box-shadow:0 7px 20px rgba(52,72,102,.08)}.transactions-header div{flex:1}.transactions-header span{color:#8a95a6;font-size:11px;letter-spacing:1px}.transactions-header h1{margin-top:4px;font-size:26px}.transactions-header .count{padding:8px 13px;border-radius:99px;background:#eaf2fd;color:#4675ae;font-size:13px;letter-spacing:0}.filter-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}.filter-chips span{padding:7px 12px;border-radius:99px;background:#fff;color:#607086;border:1px solid #e1e8f0}.transaction-card,.detail-card{padding:8px 24px;background:#fff;border:1px solid #ebf0f5;border-radius:24px;box-shadow:0 12px 32px rgba(48,68,98,.06)}.transaction-row{width:100%;display:flex;align-items:center;gap:13px;padding:16px 0;border:0;border-bottom:1px solid #eef2f6;background:transparent;color:inherit;text-align:left;cursor:pointer}.transaction-row:last-child{border-bottom:0}.avatar{flex:none;width:44px;height:44px;display:grid;place-items:center;border-radius:14px;background:#edf4fc;font-size:20px}.main{min-width:0;flex:1}.main b,.main small,.main em{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.main small{margin-top:5px;color:#8a95a5;font-size:12px}.main em{margin-top:5px;color:#4f79a8;font-size:11px;font-style:normal}.transaction-row>strong{flex:none}.transaction-row>strong.income{color:#268269}.transaction-row>i{color:#a1aab7;font-size:20px;font-style:normal}.state{padding:70px 20px;text-align:center;color:#8b96a6}.state.error{color:#a8464c}.state button{margin-left:12px;border:0;border-radius:8px;padding:7px 12px;background:#a8464c;color:#fff}.detail-amount{padding:34px;text-align:center;border-radius:26px;background:linear-gradient(135deg,#ffecd6,#e0efff)}.detail-amount span{color:#758197}.detail-amount h2{margin:10px 0;font-size:40px}.detail-amount p{color:#657185}.detail-card{margin-top:16px;padding:22px 28px}.detail-card dl div{display:flex;justify-content:space-between;gap:24px;padding:13px 0;border-bottom:1px solid #eef2f6}.detail-card dl div:last-child{border:0}.detail-card dt{color:#8b96a6}.detail-card dd{text-align:right}.trace-note{margin-top:18px;padding:13px;border-radius:12px;background:#f2f6fb;color:#718096;font-size:12px;line-height:1.6}
@media(max-width:600px){.transactions-header{padding-top:4px}.transactions-header h1{font-size:21px}.transaction-card{padding:4px 16px;border-radius:20px}.transaction-row{gap:10px}.avatar{width:39px;height:39px}.transaction-row>strong{font-size:13px}.transaction-row>i{display:none}.detail-amount{padding:28px 18px}.detail-amount h2{font-size:34px}.detail-card{padding:18px}.detail-card dl div{display:block}.detail-card dd{margin-top:5px;text-align:left}}
</style>
