<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { agentLedgerRevision } from '@/utils/agentPageState'
import { useLibraries } from '@/composables/useLibraries'
import type { Period } from '@/types/report'

const props = defineProps<{ period: Period; value: string; label: string }>()
const { revision } = useLibraries()
interface Brief {
  as_of: string; summary: string; flags: string[]; advice: string[]
  income_cents: number | null; obligatory_cents: number | null; flexible_cents: number | null
  expense_cents: number | null; free_cents: number | null; surplus_cents: number | null
}
const brief = ref<Brief | null>(null)
const error = ref(false)
const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const amount = (cents: number | null) => cents === null ? '待估算' : currency.format(cents / 100)
const estimates = computed(() => {
  const b = brief.value
  return b ? [
    ['预计收入', b.income_cents], ['预计支出', b.expense_cents],
    ['其中 · 义务性支出', b.obligatory_cents], ['其中 · 弹性支出', b.flexible_cents],
    ['预留义务支出后 · 活钱', b.free_cents], ['扣除弹性支出后 · 结余', b.surplus_cents],
  ] as const : []
})
let pending: AbortController | undefined
let timer: number
async function readBrief() {
  if (document.hidden) return
  pending?.abort()
  const controller = new AbortController()
  pending = controller
  try {
    const query = new URLSearchParams({ period: props.period, value: props.value })
    const response = await fetch(`/api/report/brief?${query}`, { cache: 'no-store', signal: controller.signal })
    if (!response.ok) throw Error()
    const data = await response.json()
    if (pending === controller) { brief.value = data.brief; error.value = false }
  } catch {
    if (!controller.signal.aborted) error.value = true
  }
}
watch([() => props.period, () => props.value, revision], () => {
  pending?.abort()
  brief.value = null
  error.value = false
  void readBrief()
}, { immediate: true })
watch(agentLedgerRevision, readBrief)
onMounted(() => {
  document.addEventListener('visibilitychange', readBrief)
  timer = window.setInterval(readBrief, 30_000)
})
onBeforeUnmount(() => {
  pending?.abort()
  pending = undefined
  window.clearInterval(timer)
  document.removeEventListener('visibilitychange', readBrief)
})
</script>

<template>
  <section class="brief-letter" aria-labelledby="brief-heading">
    <header><span>ATLAS</span><i aria-hidden="true">✦</i></header>
    <h2 id="brief-heading">{{ { week: '本周', month: '本月', year: '本年' }[period] }}财务简报</h2>
    <p class="dateline">{{ label }}<template v-if="brief"> · {{ brief.as_of }} 撰写</template></p>
    <p v-if="error" role="status">简报读取失败。<button type="button" @click="readBrief">重试</button></p>
    <template v-if="brief">
      <p class="body-copy">{{ brief.summary }}</p>
      <h3>本期剩余收支</h3>
      <dl><div v-for="[name, cents] in estimates" :key="name"><dt>{{ name }}</dt><dd>{{ amount(cents) }}</dd></div></dl>
      <h3>值得核对的支出</h3>
      <ul v-if="brief.flags.length"><li v-for="(item, i) in brief.flags" :key="i">{{ item }}</li></ul>
      <p v-else>暂无待核对项</p>
      <template v-if="brief.advice.length">
        <h3>建议</h3>
        <p v-for="(item, i) in brief.advice" :key="i">{{ item }}</p>
      </template>
    </template>
    <div v-else-if="!error" class="blank-lines" aria-label="简报正文尚未写入"></div>
  </section>
</template>

<style scoped>
.brief-letter {
  position: relative; min-width: 0; padding: 21px 20px 24px; color: #253e34;
  border: 1px solid #cfc4a8; border-top: 4px solid #829680; border-radius: 3px 8px 5px 3px;
  background: linear-gradient(105deg, #f4eddb, #fffbef 20%, #fbf5e6);
  box-shadow: 1px 3px 0 -1px #e5dcc6, 1px 4px 0 -1px #cfc4a8, var(--surface-shadow);
  overflow-wrap: anywhere;
}
header { display: flex; align-items: center; justify-content: space-between; color: #786438; font: 600 12px Georgia, serif; letter-spacing: .18em; }
header i { font-style: normal; }
h2 { margin: 13px 0 5px; font: 700 24px/1.35 var(--book-serif); }
.dateline { border-bottom: 3px double #cbbd99; padding-bottom: 12px; color: #686456; font-size: 12px; }
p, li, dl { font-size: 13px; line-height: 1.75; }
h3 { margin: 14px 0 4px; font-size: 14px; color: #415949; }
.body-copy { margin-top: 12px; white-space: pre-line; }
dl div { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 5px; padding: 3px 0; border-bottom: 1px solid #ddd3b9; }
dd { margin-left: auto; font-weight: 650; font-variant-numeric: tabular-nums; }
ul { padding-left: 16px; }
li + li { margin-top: 5px; }
.blank-lines { height: 126px; background: repeating-linear-gradient(transparent 0 24px, #d5cbb577 24px 25px); }
button { border: 0; background: transparent; color: #285746; text-decoration: underline; cursor: pointer; }
@media(max-width: 800px) { .blank-lines { height: 50px; } }
</style>
