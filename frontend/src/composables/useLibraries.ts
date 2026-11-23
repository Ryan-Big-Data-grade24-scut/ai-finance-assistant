import { computed, ref } from 'vue'
import {
  createLibrary as createLibraryApi,
  deleteLibrary as deleteLibraryApi,
  getLibraries,
  selectLibrary as selectLibraryApi,
  type LedgerLibrary,
} from '@/api/libraries'

const libraries = ref<LedgerLibrary[]>([])
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const revision = ref(0)
const mutating = ref(false)
let inflight: Promise<void> | null = null
let snapshotVersion = 0

const active = computed(() => libraries.value.find(item => item.active) ?? null)

async function fetchLibraries(): Promise<void> {
  const version = snapshotVersion
  loading.value = true
  error.value = ''
  try {
    const data = await getLibraries()
    if (version !== snapshotVersion) return
    libraries.value = data.libraries
    loaded.value = true
  } catch (reason) {
    if (version !== snapshotVersion) return
    error.value = reason instanceof Error ? reason.message : '无法读取账本列表'
    throw reason
  } finally {
    loading.value = false
  }
}

async function mutate<T>(action: () => Promise<T>): Promise<T> {
  if (mutating.value) throw new Error('正在更新账本，请稍后再试。')
  mutating.value = true
  try {
    return await action()
  } finally {
    mutating.value = false
  }
}

export function useLibraries() {
  async function refresh(force = false): Promise<void> {
    if (inflight) return inflight
    if (loaded.value && !force) return
    inflight = fetchLibraries().finally(() => { inflight = null })
    return inflight
  }

  function choose(id: string): Promise<LedgerLibrary> {
    return mutate(async () => {
      const selected = await selectLibraryApi(id)
      revision.value += 1
      await refresh(true)
      return selected
    })
  }

  function create(name: string): Promise<LedgerLibrary> {
    return mutate(async () => {
      const created = await createLibraryApi(name)
      revision.value += 1
      await refresh(true)
      return created
    })
  }

  function remove(id: string): Promise<void> {
    return mutate(async () => {
      const previous = active.value?.id
      const data = await deleteLibraryApi(id)
      // 早于删除发出的列表请求不能把已删账本写回共享状态。
      snapshotVersion += 1
      libraries.value = data.libraries
      loaded.value = true
      error.value = ''
      if (data.active?.id !== previous) revision.value += 1
    })
  }

  return { libraries, active, loading, error, revision, mutating, refresh, choose, create, remove }
}
