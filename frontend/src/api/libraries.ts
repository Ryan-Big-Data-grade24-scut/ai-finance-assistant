export interface LibraryStats {
  transactions: number
  initialized: number
  uninitialized: number
  batches: number
  date_from: string | null
  date_to: string | null
  last_import: string | null
  readable: boolean
}

export interface LedgerLibrary {
  id: string
  name: string
  active: boolean
  saved_active: boolean
  created_at: string
  stats: LibraryStats
}

export interface LibrariesResp {
  libraries: LedgerLibrary[]
  active: LedgerLibrary | null
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export async function getLibraries(): Promise<LibrariesResp> {
  return parse<LibrariesResp>(await fetch('/api/libraries', { cache: 'no-store' }))
}

export async function getActiveLibrary(): Promise<LedgerLibrary> {
  return parse<LedgerLibrary>(await fetch('/api/libraries/active', { cache: 'no-store' }))
}

export async function selectLibrary(id: string): Promise<LedgerLibrary> {
  return parse<LedgerLibrary>(await fetch(`/api/libraries/${encodeURIComponent(id)}/select`, {
    method: 'POST',
  }))
}

export async function createLibrary(name: string): Promise<LedgerLibrary> {
  return parse<LedgerLibrary>(await fetch('/api/libraries', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }))
}
