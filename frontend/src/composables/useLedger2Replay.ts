import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { getLedger2Replay, type Ledger2Replay } from '@/api/ledger2'

export type ReplayPhase = 'source' | 'target' | 'result'

export function useLedger2Replay() {
  const replay = shallowRef<Ledger2Replay | null>(null)
  const fetching = ref(false)
  const position = ref(0)
  const phase = ref<ReplayPhase>('source')
  const playing = ref(false)
  const complete = ref(false)
  let timer: number | undefined
  let request = 0

  const step = computed(() => replay.value?.steps[position.value] ?? null)
  const staged = computed(() => {
    const current = step.value
    if (!current) return false
    const before = new Map(current.before.map(item => [item.ref, item.parent]))
    const changed = new Set(current.changed)
    return current.after.some(item => changed.has(item.ref)
      && before.has(item.ref) && before.get(item.ref) !== item.parent)
  })
  const active = computed(() => fetching.value || replay.value !== null)

  function pause() {
    window.clearTimeout(timer)
    timer = undefined
    playing.value = false
  }

  function advance() {
    const duration = phase.value === 'source' ? (staged.value ? 950 : 650)
      : phase.value === 'target' ? 900 : 1150
    timer = window.setTimeout(() => {
      if (!replay.value) return
      if (phase.value === 'source') phase.value = staged.value ? 'target' : 'result'
      else if (phase.value === 'target') phase.value = 'result'
      else if (position.value + 1 < replay.value.steps.length) {
        position.value += 1
        phase.value = 'source'
      } else {
        complete.value = true
        pause()
        return
      }
      advance()
    }, duration)
  }

  function resume() {
    if (!replay.value || playing.value || complete.value) return
    playing.value = true
    advance()
  }

  function exit() {
    request += 1
    pause()
    replay.value = null
    fetching.value = false
    position.value = 0
    phase.value = 'source'
    complete.value = false
  }

  async function begin(ids: number[]) {
    const current = ++request
    fetching.value = true
    try {
      const next = await getLedger2Replay(ids)
      if (current !== request) return false
      position.value = 0
      phase.value = 'source'
      complete.value = false
      replay.value = next
      resume()
      return true
    } catch (reason) {
      if (current === request) throw reason
      return false
    } finally {
      if (current === request) fetching.value = false
    }
  }

  onBeforeUnmount(exit)
  return { replay, fetching, position, phase, playing, step, active, complete, begin, pause, resume, exit }
}
