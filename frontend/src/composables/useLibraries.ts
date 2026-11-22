import { computed, ref } from 'vue'
import {
  createLibrary as createLibraryApi,
  getLibraries,
  selectLibrary as selectLibraryApi,
  type LedgerLibrary,
} from '@/api/libraries'

const libraries = ref<LedgerLibrary[]>([])
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const revision = ref(0)
let inflight: Promise<void> | null = null

const active = computed(() => libraries.value.find(item => item.active) ?? null)

async function fetchLibraries(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const data = await getLibraries()
    libraries.value = data.libraries
    loaded.value = true
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取账本列表'
    throw reason
  } finally {
    loading.value = false
  }
}

export function useLibraries() {
  async function refresh(force = false): Promise<void> {
    if (inflight) return inflight
    if (loaded.value && !force) return
    inflight = fetchLibraries().finally(() => { inflight = null })
    return inflight
  }

  async function choose(id: string): Promise<LedgerLibrary> {
    const selected = await selectLibraryApi(id)
    revision.value += 1
    await refresh(true)
    return selected
  }

  async function create(name: string): Promise<LedgerLibrary> {
    const created = await createLibraryApi(name)
    revision.value += 1
    await refresh(true)
    return created
  }

  return { libraries, active, loading, error, revision, refresh, choose, create }
}
