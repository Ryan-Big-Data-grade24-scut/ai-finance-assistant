<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getReportBundle, getTransactions } from '@/api/reports'
import type { CalendarResp, CategoryResp, IncomeRankResp, MethodResp, PaginatedResp, Period, ReportParams, SummaryResp, TrendResp, TxItem, TxType } from '@/types/report'

const props = defineProps<{ type: string; period: string; value: string; tab: string }>()
const router = useRouter()
const loading = ref(true)
const error = ref('')
const activePane = ref<'overview' | 'category'>(props.tab === 'category' ? 'category' : 'overview')
const summary = ref<SummaryResp>({ period: '', total: 0, prev: 0, delta: 0, rate: null, count: 0 })
const calendar = ref<CalendarResp>({ days: [] })
const trend = ref<TrendResp>({ bars: [], avg: 0, n: 0 })
const categories = ref<CategoryResp>({ total: 0, majors: [] })
const methods = ref<MethodResp>({ groups: [] })
const incomeRank = ref<IncomeRankResp>({ items: [] })
const recent = ref<PaginatedResp>({ total: 0, page: 1, size: 8, items: [] })
const detailOpen = ref(false)
const detailLoading = ref(false)
const detailTitle = ref('交易明细')
const detailItems = ref<TxItem[]>([])
const expandedCategories = ref(new Set<string>())
const trendView = ref<'bar' | 'line'>('bar')
const categoryView = ref<'donut' | 'bar'>('donut')
const calendarView = ref<'calendar' | 'heat'>('calendar')
const selectedCategory = ref('')
const pickerOpen = ref(false)
const trendEl = ref<HTMLDivElement | null>(null)
const categoryEl = ref<HTMLDivElement | null>(null)
let trendChart: echarts.ECharts | null = null
let categoryChart: echarts.ECharts | null = null

const currentType = computed<TxType>(() => props.type === 'income' ? 'income' : 'expense')
const currentPeriod = computed<Period>(() => ['week', 'year'].includes(props.period) ? props.period as Period : 'month')
const reportParams = computed<ReportParams>(() => ({ type: currentType.value, period: currentPeriod.value, value: props.value }))
const isIncome = computed(() => currentType.value === 'income')
const topCategory = computed(() => categories.value.majors[0])
const selectedMajor = computed(() => categories.value.majors.find(item => item.major === selectedCategory.value) || topCategory.value)
const maxMethod = computed(() => Math.max(...methods.value.groups.map(item => item.amount), 1))
const maxDayAmount = computed(() => Math.max(...calendar.value.days.map(item => item.amount), 1))
const periodTabs: { key: Period; label: string }[] = [
  { key: 'week', label: '周' }, { key: 'month', label: '月' }, { key: 'year', label: '年' },
]
const periodOptions = computed(() => {
  const year = Number(props.value.slice(0, 4)) || new Date().getFullYear()
  if (currentPeriod.value === 'year') return Array.from({ length: 5 }, (_, index) => ({ value: String(year + 1 - index), label: `${year + 1 - index}年` }))
  if (currentPeriod.value === 'month') return Array.from({ length: 12 }, (_, index) => {
    const month = 12 - index
    return { value: `${year}-${String(month).padStart(2, '0')}`, label: `${year}年${month}月` }
  })
  return Array.from({ length: 53 }, (_, index) => {
    const week = 53 - index
    return { value: `${year}-W${String(week).padStart(2, '0')}`, label: `${year}年第${week}周` }
  })
})

const money = (value: number) => new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
const compactMoney = (value: number) => value >= 10000 ? `${(value / 10000).toFixed(1)}万` : money(value)
const periodLabel = computed(() => {
  if (currentPeriod.value === 'month') {
    const [year, month] = props.value.split('-')
    return `${year}年${Number(month)}月`
  }
  if (currentPeriod.value === 'year') return `${props.value}年`
  const [year, week] = props.value.split('-W')
  return `${year}年第${Number(week)}周`
})
const comparisonText = computed(() => {
  if (summary.value.prev === 0) return '上期暂无可比较数据'
  return `较上期${summary.value.delta >= 0 ? '增加' : '减少'} ¥${money(Math.abs(summary.value.delta))}`
})
const deterministicInsight = computed(() => {
  if (!summary.value.count) return '当前周期暂无交易，换一个时间范围看看。'
  if (isIncome.value) {
    const first = incomeRank.value.items[0]
    return first ? `本期共记录 ${summary.value.count} 笔收入，最大一笔来自${first.counterparty || first.source}。` : `本期共记录 ${summary.value.count} 笔收入。`
  }
  return topCategory.value ? `${topCategory.value.major}占本期支出的 ${topCategory.value.pct.toFixed(1)}%，是金额最高的分类。` : `本期共记录 ${summary.value.count} 笔支出。`
})

function isoWeek(date: Date) {
  const utc = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  const day = utc.getUTCDay() || 7
  utc.setUTCDate(utc.getUTCDate() + 4 - day)
  const yearStart = new Date(Date.UTC(utc.getUTCFullYear(), 0, 1))
  return { year: utc.getUTCFullYear(), week: Math.ceil((((utc.getTime() - yearStart.getTime()) / 86400000) + 1) / 7) }
}
function weekStart(value: string) {
  const [yearText, weekText] = value.split('-W')
  const fourth = new Date(Number(yearText), 0, 4)
  const monday = new Date(fourth)
  monday.setDate(fourth.getDate() - ((fourth.getDay() + 6) % 7) + (Number(weekText) - 1) * 7)
  return monday
}
function isoDate(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

const dayMap = computed(() => new Map(calendar.value.days.map(day => [day.date, day])))
const calendarCells = computed(() => {
  if (currentPeriod.value === 'year') return []
  const dates: { key: string; date: number; amount: number; count: number; blank?: boolean }[] = []
  if (currentPeriod.value === 'week') {
    const start = weekStart(props.value)
    for (let i = 0; i < 7; i += 1) {
      const date = new Date(start)
      date.setDate(start.getDate() + i)
      const key = isoDate(date)
      const data = dayMap.value.get(key)
      dates.push({ key, date: date.getDate(), amount: data?.amount ?? 0, count: data?.count ?? 0 })
    }
    return dates
  }
  const [year, month] = props.value.split('-').map(Number)
  const leading = (new Date(year, month - 1, 1).getDay() + 6) % 7
  const totalDays = new Date(year, month, 0).getDate()
  for (let i = 0; i < leading; i += 1) dates.push({ key: `blank-${i}`, date: 0, amount: 0, count: 0, blank: true })
  for (let day = 1; day <= totalDays; day += 1) {
    const key = `${props.value}-${String(day).padStart(2, '0')}`
    const data = dayMap.value.get(key)
    dates.push({ key, date: day, amount: data?.amount ?? 0, count: data?.count ?? 0 })
  }
  return dates
})
const yearlyMonths = computed(() => {
  const months = Array.from({ length: 12 }, (_, index) => ({ month: index + 1, amount: 0, count: 0 }))
  calendar.value.days.forEach(day => {
    const index = Number(day.date.slice(5, 7)) - 1
    if (months[index]) { months[index].amount += day.amount; months[index].count += day.count }
  })
  return months
})
function isToday(key: string) { return key === isoDate(new Date()) }
function heatStyle(amount: number) {
  if (!amount || calendarView.value !== 'heat') return undefined
  const intensity = Math.min(0.88, 0.16 + amount / maxDayAmount.value * 0.72)
  return { background: `rgba(54, 121, 237, ${intensity})`, color: intensity > 0.5 ? '#fff' : '#254f82' }
}
function categoryIcon(name: string) {
  const map: Record<string, string> = { 餐饮美食: '🍜', 交通出行: '🚕', 日用百货: '🛍️', 学习教育: '📚', 娱乐休闲: '🎬', 医疗健康: '🩺', 生活缴费: '💡', 数码电器: '💻', 住房: '🏠', 工资: '💼', 奖学金: '🎓', 转账红包: '🧧', 退款: '↩️' }
  return map[name] || '🧾'
}
function methodBadge(method: string) {
  if (method.includes('微信')) return { text: '微', className: 'wechat' }
  if (method.includes('支付宝') || method.includes('花呗')) return { text: '支', className: 'alipay' }
  if (method.includes('亲情')) return { text: '亲', className: 'family' }
  return { text: '卡', className: 'bank' }
}

function replaceQuery(next: Partial<ReportParams>) {
  router.replace({ name: 'bill', query: { ...router.currentRoute.value.query, type: next.type ?? currentType.value, period: next.period ?? currentPeriod.value, value: next.value ?? props.value } })
}
function setType(type: TxType) { activePane.value = 'overview'; replaceQuery({ type }) }
function setPane(pane: 'overview' | 'category') {
  activePane.value = pane
  router.replace({ query: { ...router.currentRoute.value.query, tab: pane === 'overview' ? 'summary' : 'category' } })
}
function openPeriodPicker(period: Period) {
  if (period !== currentPeriod.value) setPeriod(period)
  pickerOpen.value = true
}
function choosePeriod(value: string) { pickerOpen.value = false; replaceQuery({ value }) }
function setPeriod(period: Period) {
  const year = props.value.slice(0, 4) || String(new Date().getFullYear())
  let value = year
  if (period === 'month') value = `${year}-${String(new Date().getMonth() + 1).padStart(2, '0')}`
  if (period === 'week') { const current = isoWeek(new Date()); value = `${current.year}-W${String(current.week).padStart(2, '0')}` }
  replaceQuery({ period, value })
}
function shiftPeriod(delta: number) {
  if (currentPeriod.value === 'year') { replaceQuery({ value: String(Number(props.value) + delta) }); return }
  if (currentPeriod.value === 'month') {
    const [year, month] = props.value.split('-').map(Number)
    const date = new Date(year, month - 1 + delta, 1)
    replaceQuery({ value: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}` }); return
  }
  const start = weekStart(props.value); start.setDate(start.getDate() + delta * 7)
  const shifted = isoWeek(start); replaceQuery({ value: `${shifted.year}-W${String(shifted.week).padStart(2, '0')}` })
}
function toggleCategory(name: string) {
  const next = new Set(expandedCategories.value); next.has(name) ? next.delete(name) : next.add(name); expandedCategories.value = next
}
function selectAndToggleCategory(name: string) { selectedCategory.value = name; toggleCategory(name); nextTick(renderCharts) }
function selectCategory(name: string) {
  selectedCategory.value = name
  const next = new Set(expandedCategories.value); next.add(name); expandedCategories.value = next
  nextTick(renderCharts)
}
function goTransactions(filters: Record<string, string | number | undefined> = {}) {
  router.push({ name: 'transactions', query: { ...reportParams.value, ...filters } })
}
async function openDetails(title: string, filters: { category?: string; pay_method?: string } = {}) {
  detailOpen.value = true; detailLoading.value = true; detailTitle.value = title; detailItems.value = []
  try { detailItems.value = (await getTransactions(reportParams.value, { ...filters, size: 100 })).items }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '明细加载失败' }
  finally { detailLoading.value = false }
}
function closeDetails() { detailOpen.value = false }

function renderCharts() {
  if (trendEl.value) {
    if (!trendChart || trendChart.getDom() !== trendEl.value) {
      trendChart?.dispose()
      trendChart = echarts.init(trendEl.value, undefined, { renderer: 'svg' })
    }
    const amounts = trend.value.bars.map(item => item.amount)
    const averageLine = trend.value.avg ? { symbol: 'none', silent: true, data: [{ yAxis: trend.value.avg }], lineStyle: { color: '#d6b85b', type: 'dashed' }, label: { formatter: `均值 ¥${compactMoney(trend.value.avg)}`, color: '#9a7b1f', position: 'insideEndTop' } } : undefined
    trendChart.setOption({
      animationDuration: 650,
      grid: { left: 8, right: 8, top: 34, bottom: 28, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (value: number) => `¥${money(value)}` },
      xAxis: { type: 'category', data: trend.value.bars.map(item => currentPeriod.value === 'month' ? `${Number(item.label.slice(5))}月` : item.label), axisTick: { show: false }, axisLine: { lineStyle: { color: '#dbe3ec' } }, axisLabel: { color: '#748091', fontSize: 12 } },
      yAxis: { type: 'value', show: false },
      series: trendView.value === 'bar'
        ? [{ type: 'bar', data: amounts.map((value, index) => ({ value, itemStyle: { color: index === amounts.length - 1 ? '#3679ed' : '#b9d7ff', borderRadius: [9, 9, 2, 2] } })), barMaxWidth: 42, label: { show: true, position: 'top', color: '#647083', formatter: ({ value }: { value: number }) => compactMoney(value) }, markLine: averageLine }]
        : [{ type: 'line', data: amounts, smooth: true, symbol: 'circle', symbolSize: 9, lineStyle: { width: 4, color: '#3679ed' }, itemStyle: { color: '#3679ed', borderColor: '#fff', borderWidth: 3 }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(54,121,237,.35)' }, { offset: 1, color: 'rgba(54,121,237,.03)' }] } }, label: { show: true, position: 'top', color: '#647083', formatter: ({ value }: { value: number }) => compactMoney(value) }, markLine: averageLine }],
    }, true)
  } else if (trendChart) {
    trendChart.dispose()
    trendChart = null
  }
  if (categoryEl.value && categories.value.majors.length) {
    if (!categoryChart || categoryChart.getDom() !== categoryEl.value) {
      categoryChart?.dispose()
      categoryChart = echarts.init(categoryEl.value, undefined, { renderer: 'svg' })
    }
    const colors = ['#3679ed', '#65a8ff', '#72d1c1', '#f6bd60', '#ee7b7b', '#9d8df1', '#8fabc7']
    const option = categoryView.value === 'donut' ? {
      animationDuration: 650, color: colors, tooltip: { trigger: 'item', formatter: '{b}<br/>¥{c} · {d}%' },
      xAxis: undefined, yAxis: undefined, grid: undefined,
      series: [{ type: 'pie', radius: ['54%', '76%'], center: ['50%', '50%'], selectedMode: 'single', avoidLabelOverlap: true, itemStyle: { borderColor: '#fff', borderWidth: 3, borderRadius: 6 }, label: { show: false }, emphasis: { scaleSize: 8 }, data: categories.value.majors.map(item => ({ name: item.major, value: item.amount, selected: item.major === selectedMajor.value?.major })) }],
      graphic: [{ type: 'group', left: 'center', top: 'middle', children: [
        { type: 'text', style: { text: selectedMajor.value?.major || '', textAlign: 'center', fill: '#667085', fontSize: 14 }, left: 'center', top: -20 },
        { type: 'text', style: { text: `${selectedMajor.value?.pct.toFixed(1) || 0}%`, textAlign: 'center', fill: '#172033', fontSize: 24, fontWeight: 700 }, left: 'center', top: 3 },
      ] }],
    } : {
      animationDuration: 650, color: colors, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (value: number) => `¥${money(value)}` }, graphic: [],
      grid: { left: 16, right: 45, top: 8, bottom: 8, containLabel: true },
      xAxis: { type: 'value', show: false },
      yAxis: { type: 'category', inverse: true, data: categories.value.majors.map(item => item.major), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#59667a' } },
      series: [{ type: 'bar', data: categories.value.majors.map((item, index) => ({ value: item.amount, name: item.major, itemStyle: { color: item.major === selectedMajor.value?.major ? '#3679ed' : colors[index % colors.length], borderRadius: [0, 8, 8, 0] } })), barMaxWidth: 24, label: { show: true, position: 'right', formatter: ({ value }: { value: number }) => `¥${compactMoney(value)}`, color: '#657185' } }],
    }
    categoryChart.setOption(option, true)
    categoryChart.off('click')
    categoryChart.on('click', event => selectCategory(String(event.name)))
  } else if (categoryChart) {
    categoryChart.dispose()
    categoryChart = null
  }
}
async function loadReport() {
  loading.value = true; error.value = ''
  let loaded = false
  try {
    const [summaryData, calendarData, trendData, categoryData, methodData, rankData, recentData] = await getReportBundle(reportParams.value)
    summary.value = summaryData; calendar.value = calendarData; trend.value = trendData; categories.value = categoryData; methods.value = methodData; incomeRank.value = rankData; recent.value = recentData
    expandedCategories.value = new Set(categoryData.majors.slice(0, 1).map(item => item.major))
    selectedCategory.value = categoryData.majors[0]?.major || ''
    loaded = true
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '报告加载失败' }
  finally { loading.value = false }
  if (loaded) { await nextTick(); renderCharts() }
}
function onResize() { trendChart?.resize(); categoryChart?.resize() }
watch(() => [props.type, props.period, props.value], loadReport, { immediate: true })
watch(activePane, async () => { await nextTick(); renderCharts() })
watch([trendView, categoryView], async () => { await nextTick(); renderCharts() })
window.addEventListener('resize', onResize)
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); trendChart?.dispose(); categoryChart?.dispose() })
</script>

<template>
  <div class="report-page">
    <header class="report-controls">
      <div class="period-tabs" aria-label="报告周期">
        <button v-for="item in periodTabs" :key="item.key" :class="{ active: currentPeriod === item.key }" @click="openPeriodPicker(item.key)">{{ item.label }}<small>▼</small></button>
      </div>
      <div v-if="pickerOpen" class="period-picker">
        <div><b>选择{{ currentPeriod === 'week' ? '周' : currentPeriod === 'month' ? '月份' : '年份' }}</b><button @click="pickerOpen = false">×</button></div>
        <button v-for="option in periodOptions" :key="option.value" :class="{ active: option.value === props.value }" @click="choosePeriod(option.value)">{{ option.label }}</button>
      </div>
      <div class="filter-row">
        <div class="date-switcher"><button class="round-button" aria-label="上一周期" @click="shiftPeriod(-1)">‹</button><strong>{{ periodLabel }}</strong><button class="round-button" aria-label="下一周期" @click="shiftPeriod(1)">›</button></div>
        <div class="type-toggle" aria-label="收支类型"><button :class="{ active: !isIncome }" @click="setType('expense')">支出</button><button :class="{ active: isIncome }" @click="setType('income')">收入</button></div>
      </div>
      <div class="view-tabs">
        <button :class="{ active: activePane === 'overview' }" @click="setPane('overview')">{{ currentPeriod === 'month' ? '月度小结' : currentPeriod === 'week' ? '周度小结' : '年度小结' }}</button>
        <button :class="{ active: activePane === 'category' }" @click="setPane('category')">{{ isIncome ? '收入排行' : '支出分类' }}</button>
      </div>
    </header>

    <div v-if="error" class="status-card error-card"><b>报告暂时无法加载</b><span>{{ error }}</span><button @click="loadReport">重新加载</button></div>
    <div v-else-if="loading" class="loading-grid" aria-label="正在加载"><div v-for="n in 4" :key="n" class="skeleton" /></div>
    <template v-else>
      <section class="hero-card" :class="{ income: isIncome }">
        <div><p>{{ periodLabel }} · {{ isIncome ? '收入' : '支出' }}</p><h1><span>¥</span>{{ money(summary.total) }}</h1><div class="hero-meta"><span>共 {{ summary.count }} 笔</span><span>{{ comparisonText }}</span></div></div>
        <div class="hero-orbit" aria-hidden="true"><div class="orbit-ring" /><div class="orbit-core">{{ isIncome ? '收' : '账' }}</div></div>
      </section>

      <template v-if="activePane === 'overview'">
        <section class="report-card insight-card">
          <div class="section-heading compact"><div><span class="eyebrow">确定性统计</span><h2>{{ currentPeriod === 'month' ? '月度小结' : currentPeriod === 'week' ? '周度小结' : '年度小结' }}</h2></div><span class="data-badge">由后端计算</span></div>
          <p class="insight-copy"><i />{{ deterministicInsight }}</p>
          <div class="metric-grid">
            <div><span>本期笔数</span><strong>{{ summary.count }}</strong></div>
            <div><span>单笔均额</span><strong>¥{{ money(summary.count ? summary.total / summary.count : 0) }}</strong></div>
            <div><span>环比变化</span><strong :class="summary.delta > 0 ? 'warm' : 'cool'">{{ summary.rate === null ? '—' : `${summary.rate > 0 ? '+' : ''}${summary.rate}%` }}</strong></div>
            <div><span>{{ isIncome ? '最大收入' : '最高分类' }}</span><strong class="truncate">{{ isIncome ? (incomeRank.items[0]?.counterparty || incomeRank.items[0]?.source || '—') : (topCategory?.major || '—') }}</strong></div>
          </div>
        </section>
        <section class="report-card chart-card">
          <div class="section-heading"><div><span class="eyebrow">最近 {{ trend.n }} 期</span><h2>{{ isIncome ? '收入趋势' : '支出趋势' }}</h2></div><div class="heading-actions"><span class="average-pill">均值 ¥{{ compactMoney(trend.avg) }}</span><div class="chart-switch"><button :class="{ active: trendView === 'bar' }" @click="trendView = 'bar'">柱状</button><button :class="{ active: trendView === 'line' }" @click="trendView = 'line'">折线</button></div></div></div>
          <div ref="trendEl" class="trend-chart" role="img" :aria-label="`周期趋势${trendView === 'bar' ? '柱状图' : '折线图'}`" />
        </section>
        <section class="report-card calendar-card">
          <div class="section-heading"><div><span class="eyebrow">按日期追溯</span><h2>{{ currentPeriod === 'year' ? '月度分布' : '每日账单' }}</h2></div><div v-if="currentPeriod !== 'year'" class="chart-switch"><button :class="{ active: calendarView === 'calendar' }" @click="calendarView = 'calendar'">日历</button><button :class="{ active: calendarView === 'heat' }" @click="calendarView = 'heat'">热力</button></div></div>
          <template v-if="currentPeriod !== 'year'">
            <div class="weekday-row"><span v-for="day in ['一', '二', '三', '四', '五', '六', '日']" :key="day">{{ day }}</span></div>
            <div class="calendar-grid" :class="{ heatmap: calendarView === 'heat' }"><button v-for="cell in calendarCells" :key="cell.key" class="calendar-cell" :class="{ blank: cell.blank, active: cell.amount > 0, today: isToday(cell.key) }" :style="heatStyle(cell.amount)" :disabled="cell.blank || !cell.amount" @click="goTransactions({ date: cell.key })"><b v-if="!cell.blank">{{ isToday(cell.key) ? '今' : cell.date }}</b><small v-if="!cell.blank">{{ cell.amount ? money(cell.amount) : '—' }}</small></button></div>
          </template>
          <div v-else class="year-grid"><div v-for="item in yearlyMonths" :key="item.month" :class="{ active: item.amount }"><span>{{ item.month }}月</span><strong>¥{{ compactMoney(item.amount) }}</strong><small>{{ item.count }}笔</small></div></div>
        </section>
        <section class="report-card">
          <div class="section-heading"><div><span class="eyebrow">逐笔可追溯</span><h2>近期明细</h2></div><button class="text-button" @click="goTransactions()">全部 {{ recent.total }} 笔 ›</button></div>
          <div v-if="recent.items.length" class="transaction-list"><button v-for="item in recent.items" :key="item.id" class="transaction-row" @click="goTransactions({ id: item.id })"><span class="category-avatar">{{ categoryIcon(item.category) }}</span><span class="transaction-main"><b>{{ item.counterparty || item.item || item.category }}</b><small>{{ item.date.slice(5) }} · {{ item.source }} · {{ item.pay_method || '方式未知' }}</small></span><strong :class="{ income: item.direction === 'income' }">{{ item.direction === 'income' ? '+' : '-' }}¥{{ money(item.amount) }}</strong></button></div>
          <div v-else class="empty-state">本期还没有可展示的交易</div>
        </section>
      </template>

      <template v-else-if="!isIncome">
        <section class="report-card category-overview">
          <div class="section-heading"><div><span class="eyebrow">按原始分类统计</span><h2>支出分类</h2></div><div class="heading-actions"><span class="data-badge">暂未经过智能重分类</span><div class="chart-switch"><button :class="{ active: categoryView === 'donut' }" @click="categoryView = 'donut'">环形</button><button :class="{ active: categoryView === 'bar' }" @click="categoryView = 'bar'">条形</button></div></div></div>
          <p class="insight-copy"><i />{{ deterministicInsight }}</p>
          <div class="category-layout"><div ref="categoryEl" class="category-chart" role="img" :aria-label="`支出分类${categoryView === 'donut' ? '环形图' : '条形图'}`" /><div class="category-legend"><button v-for="item in categories.majors.slice(0, 5)" :key="item.major" :class="{ active: selectedCategory === item.major }" @click="selectCategory(item.major)"><span>{{ categoryIcon(item.major) }} {{ item.major }}</span><b>{{ item.pct.toFixed(1) }}%</b></button></div></div>
        </section>
        <section class="report-card category-list-card">
          <div class="category-group" v-for="(major, index) in categories.majors" :key="major.major">
            <button class="category-summary" @click="selectAndToggleCategory(major.major)"><span class="rank-number">{{ index + 1 }}</span><span class="category-avatar">{{ categoryIcon(major.major) }}</span><span class="category-name"><b>{{ major.major }}</b><small>{{ major.count }} 笔 · {{ major.pct.toFixed(1) }}%</small></span><strong>¥{{ money(major.amount) }}</strong><span class="chevron" :class="{ open: expandedCategories.has(major.major) }">⌄</span></button>
            <div v-if="expandedCategories.has(major.major)" class="minor-list"><button v-if="!major.minors.length" @click="goTransactions({ category: major.major })"><span>查看该分类全部交易</span><b>{{ major.count }} 笔 ›</b></button><button v-for="minor in major.minors" :key="minor.minor" @click="goTransactions({ category: major.major, subcat: minor.minor })"><span>{{ minor.minor }}</span><b>¥{{ money(minor.amount) }} · {{ minor.count }}笔 ›</b></button></div>
          </div>
          <div v-if="!categories.majors.length" class="empty-state">本期没有支出分类数据</div>
        </section>
        <section class="report-card method-card">
          <div class="section-heading"><div><span class="eyebrow">资金渠道</span><h2>支出方式</h2></div></div>
          <button v-for="method in methods.groups" :key="method.pay_method" class="method-row" @click="goTransactions({ pay_method: method.pay_method })"><span class="method-logo" :class="methodBadge(method.pay_method).className">{{ methodBadge(method.pay_method).text }}</span><span class="method-main"><b>{{ method.pay_method || '支付方式未知' }}</b><span><i :style="{ width: `${method.amount / maxMethod * 100}%` }" /></span></span><strong>¥{{ money(method.amount) }}<small>{{ method.count }}笔</small></strong><span class="row-arrow">›</span></button>
        </section>
      </template>

      <template v-else>
        <section class="report-card income-rank-card">
          <div class="section-heading"><div><span class="eyebrow">按金额排序</span><h2>收入排行</h2></div><span class="data-badge">最多显示 50 笔</span></div>
          <button v-for="(item, index) in incomeRank.items" :key="item.tx_id" class="rank-row" @click="goTransactions({ id: item.tx_id })"><span class="rank-medal" :class="{ top: index < 3 }">{{ index + 1 }}</span><span class="category-avatar">{{ categoryIcon(item.tx_type) }}</span><span class="transaction-main"><b>{{ item.counterparty || item.source }}</b><small>{{ item.date }} · {{ item.tx_type }}</small></span><strong>+¥{{ money(item.amount) }}</strong></button>
          <div v-if="!incomeRank.items.length" class="empty-state">本期没有收入记录</div>
        </section>
      </template>
    </template>

    <Teleport to="body"><div v-if="detailOpen" class="detail-backdrop" @click.self="closeDetails"><aside class="detail-sheet" aria-modal="true" role="dialog"><header><div><span>逐笔可追溯</span><h2>{{ detailTitle }}</h2></div><button aria-label="关闭" @click="closeDetails">×</button></header><div v-if="detailLoading" class="empty-state">正在加载明细…</div><div v-else-if="detailItems.length" class="detail-list"><button v-for="item in detailItems" :key="item.id" @click="router.push(`/tx?id=${item.id}`)"><span class="category-avatar">{{ categoryIcon(item.category) }}</span><span class="transaction-main"><b>{{ item.counterparty || item.item || item.category }}</b><small>{{ item.tx_time }} · {{ item.source }} · {{ item.pay_method }}</small></span><strong>{{ item.direction === 'income' ? '+' : '-' }}¥{{ money(item.amount) }}</strong></button></div><div v-else class="empty-state">没有符合条件的交易</div></aside></div></Teleport>
  </div>
</template>

<style scoped>
.report-page{width:min(100%,1080px);margin:0 auto;padding-bottom:56px}.report-controls{position:sticky;top:0;z-index:20;margin:-24px -24px 20px;padding:18px 24px 0;background:rgba(246,249,253,.92);backdrop-filter:blur(16px);border-bottom:1px solid rgba(215,224,235,.8)}.period-tabs,.view-tabs{display:flex;justify-content:center;gap:54px}.period-tabs button,.view-tabs button{position:relative;border:0;background:transparent;color:#697386;font:inherit;font-weight:650;cursor:pointer;padding:8px 2px 14px}.period-tabs button{font-size:17px}.period-tabs button.active,.view-tabs button.active{color:#172033}.period-tabs button.active:after,.view-tabs button.active:after{content:'';position:absolute;height:3px;width:28px;border-radius:99px;background:#172033;bottom:5px;left:50%;transform:translateX(-50%)}.filter-row{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:16px 0 14px}.date-switcher{display:flex;align-items:center;gap:10px}.date-switcher strong{min-width:138px;text-align:center;font-size:21px;letter-spacing:-.4px}.round-button{width:32px;height:32px;border:1px solid #dfe6ef;border-radius:50%;background:#fff;color:#657185;cursor:pointer;font-size:24px;line-height:24px}.type-toggle{display:flex;padding:3px;background:#e9eef5;border-radius:999px}.type-toggle button{min-width:66px;padding:8px 16px;border:0;border-radius:999px;background:transparent;color:#697386;font:inherit;font-weight:650;cursor:pointer}.type-toggle button.active{background:#6d8eb8;color:#fff;box-shadow:0 4px 12px rgba(71,104,148,.2)}.view-tabs{justify-content:flex-start;gap:36px}.view-tabs button{font-size:15px}
.hero-card{position:relative;overflow:hidden;min-height:190px;padding:30px 34px;border-radius:28px;color:#253043;background:linear-gradient(120deg,#ffead8 0%,#fff3ce 48%,#deecff 100%);box-shadow:0 18px 46px rgba(70,88,116,.11)}.hero-card:before,.hero-card:after{content:'';position:absolute;border-radius:50%;filter:blur(2px);opacity:.7}.hero-card:before{width:180px;height:180px;right:13%;top:-80px;background:rgba(255,186,93,.35)}.hero-card:after{width:110px;height:110px;left:43%;bottom:-72px;background:rgba(255,141,103,.25)}.hero-card.income{background:linear-gradient(120deg,#dff7eb,#e9fbf4 48%,#dcecff)}.hero-card p{color:#687489;font-size:15px;font-weight:650}.hero-card h1{margin:18px 0 13px;font-size:clamp(38px,6vw,58px);line-height:1;letter-spacing:-2px}.hero-card h1 span{font-size:.52em;margin-right:6px;vertical-align:9px}.hero-meta{display:flex;flex-wrap:wrap;gap:10px 22px;color:#687489}.hero-orbit{position:absolute;right:7%;top:22px;width:145px;height:145px;display:grid;place-items:center}.orbit-ring{position:absolute;inset:8px;border:1px solid rgba(55,92,145,.2);border-radius:44% 56% 63% 37%/45% 38% 62% 55%;transform:rotate(18deg);animation:float 8s ease-in-out infinite}.orbit-core{width:76px;height:76px;display:grid;place-items:center;border-radius:24px;background:rgba(255,255,255,.72);backdrop-filter:blur(8px);color:#5079a9;font-size:30px;font-weight:750;box-shadow:0 18px 35px rgba(59,93,134,.18);transform:rotate(-6deg)}@keyframes float{50%{transform:rotate(34deg) scale(1.05)}}
.report-card{margin-top:18px;padding:26px 28px;background:#fff;border:1px solid #edf1f6;border-radius:24px;box-shadow:0 10px 30px rgba(59,73,96,.06)}.section-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:20px}.section-heading.compact{margin-bottom:12px}.section-heading h2{margin-top:4px;font-size:22px;letter-spacing:-.4px}.eyebrow{color:#8b96a7;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase}.data-badge,.average-pill{flex:none;padding:7px 12px;border-radius:99px;background:#f1f6ff;color:#4c76ac;font-size:12px}.average-pill{background:#fff7df;color:#8c7426}.muted{color:#9aa3b1;font-size:12px}.insight-copy{display:flex;gap:10px;align-items:flex-start;line-height:1.8;font-size:16px}.insight-copy i{flex:none;width:7px;height:7px;margin-top:11px;border-radius:50%;background:#3f83ee;box-shadow:0 0 0 5px #e9f2ff}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}.metric-grid>div{min-width:0;padding:16px;border-radius:16px;background:#f5f8fc}.metric-grid span{display:block;color:#7b8798;font-size:12px}.metric-grid strong{display:block;margin-top:9px;font-size:20px}.metric-grid strong.warm{color:#e15f58}.metric-grid strong.cool{color:#29866d}.truncate{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.trend-chart{height:290px;width:100%}
.weekday-row,.calendar-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:9px}.weekday-row{margin-bottom:9px;color:#8b95a5;text-align:center;font-size:12px}.calendar-cell{aspect-ratio:1.2;min-height:58px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;border-radius:14px;background:#f6f8fb;color:#576276}.calendar-cell.blank{visibility:hidden}.calendar-cell.active{background:#eaf3ff;color:#254f82}.calendar-cell.today{color:#fff;background:linear-gradient(145deg,#72aaf2,#4c86dc);box-shadow:0 7px 18px rgba(58,121,209,.25)}.calendar-cell b{font-size:13px}.calendar-cell small{max-width:100%;padding:0 4px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.year-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.year-grid>div{padding:17px;border-radius:16px;background:#f6f8fb}.year-grid>div.active{background:#eaf3ff}.year-grid span,.year-grid small{display:block;color:#7c8797}.year-grid strong{display:block;margin:8px 0}
.transaction-list,.detail-list{display:flex;flex-direction:column}.transaction-row,.detail-list button,.rank-row{width:100%;display:flex;align-items:center;gap:13px;border:0;border-bottom:1px solid #eef2f6;background:transparent;padding:14px 0;text-align:left;cursor:pointer;font:inherit;color:inherit}.transaction-row:last-child,.detail-list button:last-child,.rank-row:last-child{border-bottom:0}.category-avatar{flex:none;width:42px;height:42px;display:grid;place-items:center;border-radius:14px;background:#edf4fc;font-size:19px}.transaction-main{min-width:0;flex:1}.transaction-main b,.transaction-main small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.transaction-main small{margin-top:5px;color:#8a95a5;font-size:12px}.transaction-row>strong,.detail-list button>strong{flex:none;color:#263247}.transaction-row>strong.income{color:#268269}.text-button{border:0;background:transparent;color:#4079bc;cursor:pointer;font:inherit;font-size:13px}.empty-state{padding:42px 12px;color:#929cab;text-align:center}
.category-layout{display:grid;grid-template-columns:minmax(300px,1fr) minmax(240px,.8fr);gap:30px;align-items:center}.category-chart{height:320px}.category-legend{display:flex;flex-direction:column;gap:5px}.category-legend button{display:flex;justify-content:space-between;padding:11px 12px;border:0;border-radius:12px;background:transparent;color:#485468;font:inherit;cursor:pointer}.category-legend button:hover{background:#f4f7fb}.category-group+.category-group{border-top:1px solid #eef2f6}.category-summary{width:100%;display:flex;align-items:center;gap:12px;padding:17px 0;border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;text-align:left}.rank-number{width:24px;color:#9ba4b2;font-weight:700}.category-name{min-width:0;flex:1}.category-name b,.category-name small{display:block}.category-name small{margin-top:4px;color:#8b96a6;font-size:12px}.chevron{margin-left:4px;color:#98a1af;transition:transform .2s}.chevron.open{transform:rotate(180deg)}.minor-list{margin:0 0 14px 78px;padding:6px 14px;border-radius:15px;background:#f3f7fc}.minor-list button{width:100%;display:flex;justify-content:space-between;padding:11px 0;border:0;border-bottom:1px solid #e3eaf2;background:transparent;color:#687487;font:inherit;cursor:pointer}.minor-list button:last-child{border-bottom:0}.minor-list b{font-size:12px}
.method-row{width:100%;display:flex;align-items:center;gap:14px;padding:15px;margin-top:10px;border:0;border-radius:17px;background:#f4f8fc;color:inherit;font:inherit;text-align:left;cursor:pointer}.method-logo{flex:none;width:42px;height:42px;display:grid;place-items:center;border-radius:14px;background:#436a9f;color:#fff;font-weight:800}.method-logo.wechat{background:#21a77a}.method-logo.alipay{background:#287bee}.method-logo.family{background:#ff7a45}.method-main{min-width:0;flex:1}.method-main b{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.method-main>span{display:block;height:4px;margin-top:9px;border-radius:9px;background:#dde6f0;overflow:hidden}.method-main i{display:block;height:100%;border-radius:inherit;background:#7aa9e8}.method-row>strong{flex:none;text-align:right}.method-row>strong small{display:block;margin-top:4px;color:#8994a4;font-weight:500}.row-arrow{color:#a7afbb;font-size:22px}.rank-medal{width:28px;height:28px;display:grid;place-items:center;border-radius:50%;background:#eef2f7;color:#7e8999;font-size:12px;font-weight:800}.rank-medal.top{background:#fff0c8;color:#a67d11}.rank-row>strong{color:#268269}
.status-card{padding:22px;border-radius:18px;background:#fff}.error-card{display:flex;align-items:center;gap:12px;color:#9a3d42;border:1px solid #f1c9cc}.error-card span{flex:1}.error-card button{border:0;border-radius:10px;padding:8px 12px;background:#9a3d42;color:#fff;cursor:pointer}.loading-grid{display:grid;gap:18px}.skeleton{height:180px;border-radius:24px;background:linear-gradient(90deg,#edf1f6,#f8fafc,#edf1f6);background-size:200% 100%;animation:shimmer 1.5s infinite}@keyframes shimmer{to{background-position:-200% 0}}
.detail-backdrop{position:fixed;inset:0;z-index:100;display:flex;justify-content:flex-end;background:rgba(20,30,46,.42);backdrop-filter:blur(3px)}.detail-sheet{width:min(540px,100%);height:100%;overflow:auto;padding:28px;background:#fff;box-shadow:-20px 0 60px rgba(24,39,63,.18);animation:sheet-in .25s ease-out}.detail-sheet header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px}.detail-sheet header span{color:#8b96a7;font-size:12px}.detail-sheet header h2{margin-top:4px}.detail-sheet header button{width:36px;height:36px;border:0;border-radius:50%;background:#f0f3f7;color:#5f6b7b;font-size:24px;cursor:pointer}@keyframes sheet-in{from{transform:translateX(30px);opacity:0}}
@media(max-width:720px){.report-page{width:100%}.report-controls{margin:-16px -16px 14px;padding:10px 16px 0}.period-tabs{gap:36px}.filter-row{gap:8px}.date-switcher{gap:4px}.date-switcher strong{min-width:112px;font-size:18px}.round-button{width:28px;height:28px;font-size:20px}.type-toggle button{min-width:56px;padding:7px 12px}.hero-card{min-height:175px;padding:25px 22px;border-radius:22px}.hero-orbit{right:1%;transform:scale(.8)}.hero-card h1{font-size:39px}.report-card{margin-top:14px;padding:21px 18px;border-radius:20px}.metric-grid{grid-template-columns:repeat(2,1fr)}.metric-grid>div{padding:14px}.metric-grid strong{font-size:17px}.section-heading h2{font-size:19px}.section-heading .muted{display:none}.trend-chart{height:240px}.calendar-grid,.weekday-row{gap:5px}.calendar-cell{min-height:49px;border-radius:11px;aspect-ratio:1}.calendar-cell small{font-size:9px}.year-grid{grid-template-columns:repeat(3,1fr);gap:8px}.year-grid>div{padding:13px 10px}.category-layout{grid-template-columns:1fr;gap:0}.category-chart{height:270px}.category-legend{display:grid;grid-template-columns:1fr 1fr}.minor-list{margin-left:0}.method-row{padding:13px 11px}.method-row>strong{font-size:13px}.category-summary>strong{font-size:14px}.data-badge{max-width:140px;text-align:center}}
@media(max-width:390px){.period-tabs{gap:26px}.view-tabs{gap:24px}.date-switcher .round-button{display:none}.date-switcher strong{text-align:left}.hero-orbit{display:none}.metric-grid{gap:8px}.calendar-cell small{transform:scale(.9)}}
.report-page{max-width:100%;overflow-x:clip}.filter-row,.date-switcher{min-width:0}.type-toggle{flex:none}
@media(max-width:720px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.report-card,.hero-card{max-width:100%;overflow:hidden}}
.period-tabs button small{margin-left:4px;color:#9aa4b3;font-size:8px}.period-picker{position:absolute;z-index:40;top:58px;left:50%;width:min(520px,calc(100% - 32px));max-height:310px;overflow:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:7px;padding:14px;border:1px solid #e0e7f0;border-radius:18px;background:rgba(255,255,255,.98);box-shadow:0 20px 45px rgba(43,61,88,.18);transform:translateX(-50%)}.period-picker>div{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;padding:3px 4px 8px}.period-picker>div button{border:0;background:transparent;color:#7d8999;font-size:20px;cursor:pointer}.period-picker>button{padding:9px 8px;border:0;border-radius:10px;background:#f4f7fb;color:#59677b;cursor:pointer}.period-picker>button.active{background:#397ceb;color:#fff;font-weight:700}.heading-actions{display:flex;align-items:center;gap:9px}.chart-switch{display:flex;padding:3px;border-radius:10px;background:#edf2f8}.chart-switch button{padding:6px 10px;border:0;border-radius:8px;background:transparent;color:#748094;font-size:12px;cursor:pointer}.chart-switch button.active{background:#fff;color:#2f6fbe;font-weight:700;box-shadow:0 3px 9px rgba(55,77,110,.1)}.calendar-cell{border:0;font-family:inherit;cursor:pointer}.calendar-cell:disabled{cursor:default}.calendar-grid.heatmap .calendar-cell{transition:background .2s,color .2s}.category-legend button.active{background:#eaf2fd;color:#285f9f}.category-legend button.active b{color:#2f73c8}
@media(max-width:720px){.period-picker{top:52px;grid-template-columns:repeat(2,1fr)}.heading-actions{align-items:flex-end;flex-direction:column}.average-pill{display:none}.chart-switch button{padding:6px 9px}.category-overview .section-heading{align-items:center}.category-overview .data-badge{display:none}}
</style>
