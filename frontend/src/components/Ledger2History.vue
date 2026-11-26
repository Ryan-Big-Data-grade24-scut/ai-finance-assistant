<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Ledger2TimelineEvent } from '@/api/ledger2'

const props = defineProps<{
  steps: Ledger2TimelineEvent[]
  busy: boolean
  currentId: number | null
  label: (step: Ledger2TimelineEvent) => string
}>()
const emit = defineEmits<{ play: [ids: number[]] }>()

type HistoryGroup = { key: string; request: string | null; isRun: boolean; steps: Ledger2TimelineEvent[] }
const groups = computed(() => {
  const grouped = new Map<string, HistoryGroup>()
  for (const step of [...props.steps].reverse()) {
    const key = step.run_id ?? `step-${step.id}`
    let group = grouped.get(key)
    if (!group) {
      group = { key, request: step.request, isRun: !!step.run_id, steps: [] }
      grouped.set(key, group)
    }
    group.steps.push(step)
  }
  return [...grouped.values()]
})

const positions = computed(() => new Map(props.steps.map((step, index) => [step.id, index])))
const span = ref<{ first: number; last: number } | null>(null)
const limits = computed(() => {
  if (!span.value) return null
  const first = positions.value.get(span.value.first)
  const last = positions.value.get(span.value.last)
  return first === undefined || last === undefined ? null : { first, last }
})
watch(positions, () => { if (!limits.value) span.value = null })

function setSpan(first: number, last: number) {
  span.value = { first: props.steps[first].id, last: props.steps[last].id }
}

function chosen(id: number) {
  return !!span.value && id >= span.value.first && id <= span.value.last
}

function selectable(id: number) {
  if (props.busy) return false
  if (!limits.value) return true
  const { first, last } = limits.value
  const index = positions.value.get(id)
  return index === first - 1 || index === first || index === last || index === last + 1
}

function toggle(id: number) {
  if (!selectable(id)) return
  const index = positions.value.get(id)!
  if (!limits.value) { setSpan(index, index); return }
  const { first, last } = limits.value
  if (index < first) setSpan(index, last)
  else if (index > last) setSpan(first, index)
  else if (first === last) span.value = null
  else if (index === first) setSpan(first + 1, last)
  else setSpan(first, last - 1)
}

function toggleRun(group: HistoryGroup) {
  if (props.busy) return
  const first = positions.value.get(group.steps[group.steps.length - 1].id)!
  const last = positions.value.get(group.steps[0].id)!
  if (group.steps.every(step => chosen(step.id))) span.value = null
  else setSpan(first, last)
}

const chosenIds = computed(() => limits.value
  ? props.steps.slice(limits.value.first, limits.value.last + 1).map(step => step.id) : [])
</script>

<template>
  <div v-if="!steps.length" class="muted">还没有操作</div>
  <template v-else>
    <div class="play-actions">
      <button type="button" class="play" data-testid="play-history" :disabled="busy || !chosenIds.length"
              @click="emit('play', chosenIds)">▶ 回放{{ chosenIds.length ? ` · ${chosenIds.length} 步` : '' }}</button>
      <button v-if="chosenIds.length" type="button" :disabled="busy" @click="span = null">清空</button>
    </div>
    <p class="muted history-hint">勾选连续步骤或整轮请求。整轮间的其他操作也会一并回放。</p>
    <ol class="history-list">
      <li v-for="group in groups" :key="group.key" :class="{ 'history-run': group.isRun }">
        <header v-if="group.isRun" class="run-header">
          <label><input type="checkbox" :checked="group.steps.every(step => chosen(step.id))"
                        :disabled="busy" @change="toggleRun(group)">整轮</label>
          <b :title="group.request || 'ATLAS 请求'">{{ group.request || 'ATLAS 请求' }}</b>
        </header>
        <ol class="history-steps">
          <li v-for="step in group.steps" :key="step.id" :class="{ current: currentId === step.id }"
              :aria-current="currentId === step.id ? 'step' : undefined">
            <label>
              <input type="checkbox" :checked="chosen(step.id)" :disabled="!selectable(step.id)"
                     :data-testid="`history-step-${step.id}`" @change="toggle(step.id)">
              <span><b>#{{ step.seq }}</b> {{ label(step) }}</span>
            </label>
          </li>
        </ol>
      </li>
    </ol>
  </template>
</template>

<style scoped>
.play-actions { display: flex; align-items: center; gap: 7px; }
.play-actions button { padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 6px; background: white; }
.play-actions .play { color: #315f92; background: #f0f6ff; }
button:disabled { opacity: .52; cursor: not-allowed; }
.history-hint { margin: 6px 0; font-size: 11px; line-height: 1.45; color: var(--color-muted); }
.history-list, .history-steps { list-style: none; margin: 0; padding: 0; }
.history-list { display: grid; gap: 6px; max-height: 310px; overflow: auto; }
.history-run { min-width: 0; border: 1px solid #dbe7f4; border-radius: 8px; overflow: hidden; }
.run-header { display: flex; gap: 6px; align-items: center; background: #edf5ff; padding: 5px 6px; font-size: 12px; }
.run-header b { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.run-header label { flex: none; color: #315f92; }
.history-steps { display: grid; gap: 2px; }
.history-steps li { padding: 4px 6px; font-size: 13px; }
.history-steps li.current { color: #1b5190; background: #dcecff; }
label { display: flex; gap: 5px; align-items: center; cursor: pointer; }
label:has(input:disabled) { cursor: default; }
</style>
