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
const queryId = computed(() => {
  const id = asText(route.query.id)
  const value = Number(id)
  return /^\d+$/.test(id) && Number.isSafeInteger(value) && value > 0 ? value : 0
})
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

const ledgerLabel = computed(() => asText(route.query.ledger_subcat) || asText(route.query.ledger_category))
const title = computed(() => {
  if (queryId.value > 0) return '交易详情'
  return ledgerLabel.value || asText(route.query.subcat) || asText(route.query.category) || asText(route.query.pay_method) || (selectedDate.value ? `${selectedDate.value} 的流水` : '交易明细')
})
const filterChips = computed(() => [
  selectedDate.value,
  ledgerLabel.value ? `账本 · ${ledgerLabel.value}` : '',
  asText(route.query.category), asText(route.query.subcat), asText(route.query.pay_method),
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
      ledger_category: asText(route.query.ledger_category) || undefined,
      ledger_subcat: asText(route.query.ledger_subcat) || undefined,
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
  if (queryId.value <= 0) {
    if (window.history.state?.back) router.back()
    else void router.push({ name: 'bill', query: { ...reportParams() } })
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
  <div class="workspace-page">
    <header class="workspace-heading">
      <div class="workspace-title">
        <p class="workspace-kicker">账单报告 / 流水</p>
        <h1>{{ title }}</h1>
      </div>
      <button class="workspace-button" type="button" @click="goBack">← {{ queryId > 0 ? '返回明细' : '返回上一页' }}</button>
    </header>

    <div class="transactions-layout" :class="{ 'transactions-layout--detail': queryId > 0 }">
      <section v-if="queryId <= 0" class="workspace-panel calendar-card" aria-label="按日历筛选交易">
        <div class="calendar-head">
          <div><p class="workspace-kicker">按日筛选</p><h2>{{ calendarTitle }}</h2></div>
          <div class="calendar-actions">
            <button class="workspace-button month-step" type="button" aria-label="上个月" @click="changeMonth(-1)">‹</button>
            <button class="workspace-button month-step" type="button" aria-label="下个月" @click="changeMonth(1)">›</button>
          </div>
        </div>
        <div class="weekdays" aria-hidden="true"><span v-for="name in ['一', '二', '三', '四', '五', '六', '日']" :key="name">{{ name }}</span></div>
        <div class="calendar-grid" :aria-busy="calendarLoading">
          <template v-for="(cell, index) in calendarCells" :key="cell?.date || `blank-${index}`">
            <span v-if="!cell" aria-hidden="true" />
            <button
              v-else
              type="button"
              class="calendar-day"
              :class="{ marked: cell.count > 0 }"
              :aria-pressed="selectedDate === cell.date"
              :aria-label="`${cell.date}${cell.count ? `，${cell.count}笔交易，¥${money(cell.amount)}` : '，无交易'}`"
              @click="chooseDate(cell.date)"
            >
              <b>{{ cell.day }}</b>
              <small v-if="cell.count">{{ cell.count }}笔</small>
              <small v-if="cell.count" class="calendar-amount">¥{{ money(cell.amount) }}</small>
            </button>
          </template>
        </div>
        <p v-if="calendarError" class="workspace-note workspace-note--error calendar-caption" role="alert">{{ calendarError }}</p>
        <p v-else class="calendar-caption">有交易的日期已标记，点击即可筛选当天流水。</p>
      </section>

      <section v-if="queryId <= 0" class="workspace-panel transaction-card" aria-labelledby="transaction-results" :aria-busy="loading">
        <div class="results-head">
          <div class="results-title">
            <h2 id="transaction-results">流水列表</h2>
            <span v-if="!loading && !error" class="workspace-badge">{{ result.total }} 笔</span>
          </div>
          <button v-if="selectedDate" class="workspace-button" type="button" @click="clearDate">清除日期</button>
        </div>
        <div v-if="filterChips.length" class="filter-chips" aria-label="当前筛选">
          <span v-for="(chip, index) in filterChips" :key="index" class="workspace-badge workspace-badge--ready">{{ chip }}</span>
        </div>

        <p v-if="loading" class="state" role="status">正在加载流水…</p>
        <div v-else-if="error" class="workspace-note workspace-note--error state" role="alert">
          <p>{{ error }}</p><button class="workspace-button" type="button" @click="load">重试</button>
        </div>
        <div v-else-if="result.items.length">
          <button v-for="item in result.items" :key="item.id" class="transaction-row" type="button" @click="openItem(item.id)">
            <span class="avatar" aria-hidden="true">{{ categoryIcon(item.category) }}</span>
            <span class="row-copy">
              <b>{{ item.counterparty || item.item || item.category }}</b>
              <small>{{ item.tx_time }} · {{ item.source }} · {{ item.pay_method || '方式未知' }}</small>
              <span class="row-category">{{ item.category }}<template v-if="item.subcat"> / {{ item.subcat }}</template></span>
            </span>
            <strong class="row-amount" :class="{ income: item.direction === 'income' }">{{ item.direction === 'income' ? '+' : '-' }}¥{{ money(item.amount) }}</strong>
          </button>
        </div>
        <p v-else class="state">没有符合当前条件的交易</p>
      </section>

      <p v-else-if="loading" class="workspace-panel state" role="status">正在加载交易…</p>
      <div v-else-if="error && !detail" class="workspace-note workspace-note--error state" role="alert">
        <p>{{ error }}</p><button class="workspace-button" type="button" @click="load">重试</button>
      </div>
      <template v-else-if="detail">
        <section class="workspace-panel detail-amount" aria-label="交易金额">
          <span class="workspace-badge workspace-badge--ready">{{ detail.raw.direction === 'income' ? '收入' : '支出' }}</span>
          <h2>{{ detail.raw.direction === 'income' ? '+' : '-' }}¥{{ money(detail.raw.amount) }}</h2>
          <p>{{ detail.raw.counterparty || detail.raw.item || detail.raw.category }}</p>
        </section>
        <section class="workspace-panel detail-card">
          <h2>流水信息</h2>
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
          <p class="workspace-note trace-note">原始记录保持只读；这里展示的是原始流水与修正信息合并后的结果。</p>
          <div class="detail-actions">
            <p v-if="error" class="workspace-note workspace-note--error" role="alert">{{ error }}</p>
            <button class="workspace-button workspace-button--danger" type="button" :disabled="deleting" @click="removeCurrentTransaction">
              {{ deleting ? '正在删除…' : '删除这条交易' }}
            </button>
            <p>删除后不再显示或参与统计，原始凭证仍保留用于防重复导入。</p>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.transactions-layout {
  display: grid;
  grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr);
  align-items: start;
  gap: 16px;
}
.transactions-layout--detail { grid-template-columns: minmax(0, .65fr) minmax(0, 1.35fr); }
.calendar-card, .transaction-card, .detail-card, .detail-amount { padding: clamp(14px, 2.3vw, 24px); }
h2 { font-size: 18px; }
.calendar-head, .results-head, .results-title, .calendar-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.calendar-head .workspace-kicker { margin-bottom: 4px; }
.month-step { min-width: 38px; padding: 4px; font-size: 22px; line-height: 1; }
.weekdays, .calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 5px; }
.weekdays { margin: 16px 0 7px; color: var(--workspace-muted); font-size: 12px; text-align: center; }
.calendar-grid[aria-busy="true"] { opacity: .65; }
.calendar-day {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
  min-height: 45px;
  padding: 3px 1px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: var(--workspace-soft);
  color: var(--workspace-ink);
  font: inherit;
  font-variant-numeric: tabular-nums;
  text-align: center;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.calendar-day small { font-size: 10px; }
.calendar-day.marked { background: var(--workspace-tint); color: var(--workspace-accent); }
.calendar-day:hover { border-color: var(--workspace-accent); }
.calendar-day[aria-pressed="true"] { background: var(--workspace-accent); color: #fff; }
.calendar-caption { margin-top: 12px; color: var(--workspace-muted); font-size: 12px; line-height: 1.5; }
.calendar-caption.workspace-note--error { color: var(--workspace-danger); }
.results-head { margin-bottom: 10px; }
.filter-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.filter-chips .workspace-badge { border-radius: 9px; }
.state { padding: 22px 14px; color: var(--workspace-muted); text-align: center; }
.state.workspace-note--error { color: var(--workspace-danger); }
.state button { margin-top: 8px; }
.transaction-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(0, auto);
  align-items: start;
  gap: 10px;
  width: 100%;
  padding: 13px 5px;
  border: 0;
  border-top: 1px solid var(--workspace-line);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.transaction-row:hover { background: var(--workspace-soft); }
.avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 9px; background: var(--workspace-tint); }
.row-copy, .row-amount, .detail-amount, dd { overflow-wrap: anywhere; }
.row-copy b, .row-copy small, .row-category { display: block; }
.row-copy small, .row-category { margin-top: 3px; font-size: 12px; }
.row-copy small { color: var(--workspace-muted); }
.row-category, .row-amount.income { color: var(--workspace-accent); }
.row-amount, .detail-amount h2 { font-variant-numeric: tabular-nums; }
.row-amount { text-align: right; }
.detail-amount { background: var(--workspace-tint); }
.detail-amount h2 { margin: 10px 0; color: var(--workspace-accent); font-size: clamp(30px, 3.8vw, 44px); }
.detail-card dl { margin-top: 8px; }
.detail-card dl > div { display: grid; grid-template-columns: 7em minmax(0, 1fr); gap: 8px; padding: 11px 0; border-bottom: 1px solid var(--workspace-line); }
.detail-card dt, .detail-actions > p:last-child { color: var(--workspace-muted); }
.trace-note, .detail-actions { margin-top: 15px; font-size: 12px; }
.detail-actions { display: grid; justify-items: start; gap: 8px; }

@media (max-width: 740px) {
  .transactions-layout { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 460px) {
  .calendar-amount { display: none; }
  .transaction-row { grid-template-columns: auto minmax(0, 1fr); gap: 4px 9px; }
  .avatar { grid-row: span 2; }
  .row-amount { text-align: left; }
  .detail-card dl > div { grid-template-columns: 5.5em minmax(0, 1fr); }
}
</style>
