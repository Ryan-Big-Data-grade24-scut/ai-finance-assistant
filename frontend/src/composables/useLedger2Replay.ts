import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { getLedger2Replay, type Ledger2Replay } from '@/api/ledger2'
import { describeReplay } from '@/utils/ledger2ReplayScene'

export type ReplayPhase = 'source' | 'target' | 'settle' | 'result'

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
  const direction = computed(() => step.value ? describeReplay(step.value) : null)
  const active = computed(() => fetching.value || replay.value !== null)

  function pause() {
    window.clearTimeout(timer)
    timer = undefined
    playing.value = false
  }

  function advance() {
    const duration = phase.value === 'source' ? (direction.value?.preview ? 780 : 680)
      : phase.value === 'target' ? 690 : phase.value === 'settle' ? 850 : 1150
    timer = window.setTimeout(() => {
      if (!replay.value) return
      if (phase.value === 'source') phase.value = direction.value?.preview ? 'target' : 'result'
      else if (phase.value === 'target') phase.value = direction.value?.settle ? 'settle' : 'result'
      else if (phase.value === 'settle') phase.value = 'result'
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
