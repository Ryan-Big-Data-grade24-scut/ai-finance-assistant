<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'
import { deleteTransaction, getCalendar, getTransactions, getTxDetail } from '@/api/reports'
import type { DayPoint, PaginatedResp, Period, ReportParams, TxDetailResp, TxType } from '@/types/report'

interface CalendarCell {
  date: string
  day: number
  amount: number
  count: number
}

const route = useRoute()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const result = ref<PaginatedResp>({ total: 0, page: 1, size: 50, items: [] })
const detail = ref<TxDetailResp | null>(null)
const deleting = ref(false)
const calendarLoading = ref(false)
const calendarError = ref('')
const calendarDays = ref<DayPoint[]>([])
let calendarRequest = 0

const money = (value: number) => new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
const asText = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback
const queryId = computed(() => Number(route.query.id || 0))
const selectedDate = computed(() => asText(route.query.date))

function reportParams(): ReportParams {
  return {
    type: (route.query.type === 'income' ? 'income' : 'expense') as TxType,
    period: (['week', 'year'].includes(asText(route.query.period)) ? route.query.period : 'month') as Period,
    value: asText(route.query.value, '2026-10'),
  }
}

function monthFromRoute() {
  const date = selectedDate.value
  if (/^\d{4}-\d{2}-\d{2}$/.test(date)) return date.slice(0, 7)
  const value = asText(route.query.value)
  if (/^\d{4}-\d{2}$/.test(value)) return value
  if (/^\d{4}$/.test(value)) return `${value}-01`
  return new Date().toISOString().slice(0, 7)
}

const calendarMonth = ref(monthFromRoute())
const calendarTitle = computed(() => {
  const [year, month] = calendarMonth.value.split('-')
  return `${year}年${Number(month)}月`
})
const calendarCells = computed<Array<CalendarCell | null>>(() => {
  const [year, month] = calendarMonth.value.split('-').map(Number)
  const firstWeekday = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate()
  const map = new Map(calendarDays.value.map(day => [day.date, day]))
  const cells: Array<CalendarCell | null> = Array.from({ length: firstWeekday }, () => null)
  for (let day = 1; day <= daysInMonth; day += 1) {
    const date = `${calendarMonth.value}-${String(day).padStart(2, '0')}`
    const point = map.get(date)
    cells.push({ date, day, amount: point?.amount ?? 0, count: point?.count ?? 0 })
  }
  return cells
})

const title = computed(() => {
  if (detail.value) return '交易详情'
  return asText(route.query.subcat) || asText(route.query.category) || asText(route.query.pay_method) || (selectedDate.value ? `${selectedDate.value} 的流水` : '交易明细')
})
const filterChips = computed(() => [
  selectedDate.value, asText(route.query.category), asText(route.query.subcat), asText(route.query.pay_method),
].filter(Boolean))

async function load() {
  loading.value = true
  error.value = ''
  detail.value = null
  try {
    if (queryId.value > 0) {
      detail.value = await getTxDetail(queryId.value)
      return
    }
    const date = selectedDate.value
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

async function loadCalendar() {
  const request = ++calendarRequest
  calendarLoading.value = true
  calendarError.value = ''
  try {
    const response = await getCalendar({
      type: reportParams().type,
      period: 'month',
      value: calendarMonth.value,
    })
    if (request === calendarRequest) calendarDays.value = response.days
  } catch (reason) {
    if (request === calendarRequest) {
      calendarDays.value = []
      calendarError.value = reason instanceof Error ? reason.message : '日历加载失败'
    }
  } finally {
    if (request === calendarRequest) calendarLoading.value = false
  }
}

function syncCalendarFromRoute() {
  const target = monthFromRoute()
  if (calendarMonth.value !== target) calendarMonth.value = target
  void loadCalendar()
}

function changeMonth(offset: number) {
  const [year, month] = calendarMonth.value.split('-').map(Number)
  const target = new Date(Date.UTC(year, month - 1 + offset, 1))
  calendarMonth.value = `${target.getUTCFullYear()}-${String(target.getUTCMonth() + 1).padStart(2, '0')}`
  void loadCalendar()
}

function chooseDate(date: string) {
  const query: LocationQueryRaw = { ...route.query, date }
  delete query.id
  void router.push({ name: 'transactions', query })
}

function clearDate() {
  const query = { ...route.query }
  delete query.date
  delete query.id
  void router.push({ name: 'transactions', query })
}

function openItem(id: number) {
  void router.push({ name: 'transactions', query: { ...route.query, id } })
}

function goBack() {
  if (!detail.value) {
    router.back()
    return
  }
  const query = { ...route.query }
  delete query.id
  void router.push({ name: 'transactions', query })
}

async function removeCurrentTransaction() {
  const txId = detail.value?.raw.id
  if (!txId || deleting.value) return
  const confirmed = window.confirm('确定删除这条交易吗？它会从交易明细和所有统计中移除；原始凭证仍会安全保留，用于去重和审计。')
  if (!confirmed) return

  deleting.value = true
  error.value = ''
  try {
    await deleteTransaction(txId)
    const query = { ...route.query }
    delete query.id
    await router.replace({ name: 'transactions', query })
    await loadCalendar()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '删除交易失败'
  } finally {
    deleting.value = false
  }
}

function categoryIcon(name: string) {
  const map: Record<string, string> = { 餐饮美食: '🍜', 交通出行: '🚕', 日用百货: '🛍️', 学习教育: '📚', 娱乐休闲: '🎬', 医疗健康: '🩺', 生活缴费: '💡', 数码电器: '💻', 住房: '🏠', 工资: '💼', 奖学金: '🎓', 转账红包: '🧧', 退款: '↩️' }
  return map[name] || '🧾'
}

watch(() => route.fullPath, load, { immediate: true })
watch(() => [route.query.type, route.query.value, route.query.date], syncCalendarFromRoute, { immediate: true })
</script>

<template>
  <div class="transactions-page">
    <header class="transactions-header">
      <button class="back" type="button" aria-label="返回" @click="goBack">←</button>
      <div><span>Transactions</span><h1>{{ title }}</h1></div>
      <span v-if="!detail" class="count">{{ result.total }} 笔</span>
    </header>

    <section v-if="!detail" class="calendar-card" aria-label="按日历筛选交易">
      <div class="calendar-head">
        <div><span>日期检索</span><h2>{{ calendarTitle }}</h2></div>
        <div class="calendar-actions">
          <button type="button" aria-label="上个月" @click="changeMonth(-1)">‹</button>
          <button v-if="selectedDate" class="clear-date" type="button" @click="clearDate">清除日期</button>
          <button type="button" aria-label="下个月" @click="changeMonth(1)">›</button>
        </div>
      </div>
      <div class="weekdays"><span v-for="name in ['一', '二', '三', '四', '五', '六', '日']" :key="name">{{ name }}</span></div>
      <div class="calendar-grid" :class="{ loading: calendarLoading }">
        <template v-for="(cell, index) in calendarCells" :key="cell?.date || `blank-${index}`">
          <span v-if="!cell" class="calendar-blank" />
          <button
            v-else
            type="button"
            class="calendar-day"
            :class="{ marked: cell.count > 0, selected: selectedDate === cell.date }"
            :aria-label="`${cell.date}${cell.count ? `，${cell.count}笔交易` : '，无交易'}`"
            @click="chooseDate(cell.date)"
          >
            <b>{{ cell.day }}</b>
            <i v-if="cell.count">{{ cell.count }}笔</i>
            <em v-if="cell.count">¥{{ money(cell.amount) }}</em>
          </button>
        </template>
      </div>
      <p v-if="calendarError" class="calendar-error">{{ calendarError }}</p>
      <p v-else class="calendar-help"><span class="legend-dot" /> 有账单的日期已标记，点击日期即可筛选当天明细</p>
    </section>

    <div v-if="filterChips.length && !detail" class="filter-chips">
      <span v-for="chip in filterChips" :key="chip">{{ chip }}</span>
    </div>

    <div v-if="loading" class="state">正在加载流水…</div>
    <div v-else-if="error && !detail" class="state error">{{ error }}<button type="button" @click="load">重试</button></div>

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
        <p v-if="error" class="detail-error" role="alert">{{ error }}</p>
        <button class="delete-button" type="button" :disabled="deleting" @click="removeCurrentTransaction">
          {{ deleting ? '正在删除…' : '删除这条交易' }}
        </button>
        <p class="delete-note">删除后不再显示或参与统计，原始凭证仍保留用于防重复导入。</p>
      </section>
    </template>

    <section v-else-if="result.items.length" class="transaction-card">
      <button v-for="item in result.items" :key="item.id" class="transaction-row" type="button" @click="openItem(item.id)">
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
.transactions-page{width:min(100%,900px);margin:0 auto;padding-bottom:50px}.transactions-header{display:flex;align-items:center;gap:16px;padding:18px 0 22px}.back{width:42px;height:42px;border:0;border-radius:14px;background:#fff;color:#41516a;font-size:22px;cursor:pointer;box-shadow:0 7px 20px rgba(52,72,102,.08)}.transactions-header div{flex:1}.transactions-header span{color:#8a95a6;font-size:11px;letter-spacing:1px}.transactions-header h1{margin-top:4px;font-size:26px}.transactions-header .count{padding:8px 13px;border-radius:99px;background:#eaf2fd;color:#4675ae;font-size:13px;letter-spacing:0}.filter-chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.filter-chips span{padding:7px 12px;border-radius:99px;background:#fff;color:#607086;border:1px solid #e1e8f0}.transaction-card,.detail-card,.calendar-card{padding:8px 24px;background:#fff;border:1px solid #ebf0f5;border-radius:24px;box-shadow:0 12px 32px rgba(48,68,98,.06)}.transaction-row{width:100%;display:flex;align-items:center;gap:13px;padding:16px 0;border:0;border-bottom:1px solid #eef2f6;background:transparent;color:inherit;text-align:left;cursor:pointer}.transaction-row:last-child{border-bottom:0}.avatar{flex:none;width:44px;height:44px;display:grid;place-items:center;border-radius:14px;background:#edf4fc;font-size:20px}.main{min-width:0;flex:1}.main b,.main small,.main em{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.main small{margin-top:5px;color:#8a95a5;font-size:12px}.main em{margin-top:5px;color:#4f79a8;font-size:11px;font-style:normal}.transaction-row>strong{flex:none}.transaction-row>strong.income{color:#268269}.transaction-row>i{color:#a1aab7;font-size:20px;font-style:normal}.state{padding:70px 20px;text-align:center;color:#8b96a6}.state.error{color:#a8464c}.state button{margin-left:12px;border:0;border-radius:8px;padding:7px 12px;background:#a8464c;color:#fff}.detail-amount{padding:34px;text-align:center;border-radius:26px;background:linear-gradient(135deg,#ffecd6,#e0efff)}.detail-amount span{color:#758197}.detail-amount h2{margin:10px 0;font-size:40px}.detail-amount p{color:#657185}.detail-card{margin-top:16px;padding:22px 28px}.detail-card dl div{display:flex;justify-content:space-between;gap:24px;padding:13px 0;border-bottom:1px solid #eef2f6}.detail-card dl div:last-child{border:0}.detail-card dt{color:#8b96a6}.detail-card dd{text-align:right}.trace-note{margin-top:18px;padding:13px;border-radius:12px;background:#f2f6fb;color:#718096;font-size:12px;line-height:1.6}
.calendar-card{padding:22px 24px}.calendar-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.calendar-head>div:first-child span{color:#8995a6;font-size:11px;letter-spacing:1px}.calendar-head h2{margin-top:4px;font-size:21px}.calendar-actions{display:flex;align-items:center;gap:7px}.calendar-actions button{min-width:36px;height:36px;padding:0 10px;border:1px solid #dde6f0;border-radius:11px;background:#f5f8fc;color:#53667e;font-size:20px;cursor:pointer}.calendar-actions .clear-date{font-size:12px;color:#4577ae}.weekdays,.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px}.weekdays{margin:18px 0 8px}.weekdays span{text-align:center;color:#929cab;font-size:12px}.calendar-grid{transition:opacity .15s}.calendar-grid.loading{opacity:.55}.calendar-blank{min-height:70px}.calendar-day{position:relative;min-height:70px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:7px 3px;border:1px solid transparent;border-radius:13px;background:#f7f9fc;color:#4f5f73;cursor:pointer}.calendar-day:hover{border-color:#a9c5e6;background:#f0f6fd}.calendar-day b{font-size:14px}.calendar-day i,.calendar-day em{font-size:10px;font-style:normal;color:#7c899a}.calendar-day.marked{background:#eaf3fd}.calendar-day.marked::after{content:'';position:absolute;top:7px;right:7px;width:6px;height:6px;border-radius:50%;background:#4d8fd4}.calendar-day.marked i{color:#3674b5;font-weight:700}.calendar-day.selected{border-color:#2f7dc9;background:#d9ebff;box-shadow:inset 0 0 0 1px #2f7dc9}.calendar-help,.calendar-error{margin-top:13px;color:#7d8999;font-size:12px}.calendar-error{color:#a8464c}.legend-dot{display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:#4d8fd4}.delete-button{width:100%;margin-top:18px;padding:12px;border:1px solid #d76b72;border-radius:12px;background:#fff5f5;color:#ad3f47;font-weight:700;cursor:pointer}.delete-button:hover{background:#ffe9ea}.delete-button:disabled{opacity:.55;cursor:not-allowed}.delete-note{margin-top:8px;color:#929baa;font-size:11px;text-align:center}.detail-error{margin-top:12px;color:#a8464c;font-size:13px}
@media(max-width:600px){.transactions-header{padding-top:4px}.transactions-header h1{font-size:21px}.transaction-card{padding:4px 16px;border-radius:20px}.transaction-row{gap:10px}.avatar{width:39px;height:39px}.transaction-row>strong{font-size:13px}.transaction-row>i{display:none}.detail-amount{padding:28px 18px}.detail-amount h2{font-size:34px}.detail-card{padding:18px}.detail-card dl div{display:block}.detail-card dd{margin-top:5px;text-align:left}.calendar-card{padding:18px 12px}.calendar-head{padding:0 5px}.calendar-actions .clear-date{display:none}.weekdays,.calendar-grid{gap:4px}.calendar-blank,.calendar-day{min-height:55px}.calendar-day{border-radius:10px}.calendar-day em{display:none}.calendar-day.marked::after{top:5px;right:5px;width:5px;height:5px}}
</style>
