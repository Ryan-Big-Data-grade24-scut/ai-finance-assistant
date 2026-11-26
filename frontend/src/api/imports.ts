import type { ImportCommitResp, ImportHistoryResp, ImportPreviewResp } from '../types/import'

/** 只向相对 /api 路径发请求，交由 Vite 代理到 FastAPI。 */
async function parseResponse<T>(response: Response): Promise<T> {
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.map(item => item?.msg || String(item)).join('；')
    }
    throw new Error(message)
  }
  return payload as T
}

function makeFormData(file: File, alipayPassword: string): FormData {
  const body = new FormData()
  body.append('file', file, file.name)
  // 后端可选参数名是 alipay_password，不是 password。
  // 密码只随本次 multipart 上传发送，不存 localStorage，也不写到 URL。
  if (file.name.toLowerCase().endsWith('.zip') && alipayPassword) {
    body.append('alipay_password', alipayPassword)
  }
  return body
}

export async function previewImport(file: File, alipayPassword = '', signal?: AbortSignal) {
  const response = await fetch('/api/import/preview', {
    method: 'POST',
    body: makeFormData(file, alipayPassword),
    signal,
  })
  return parseResponse<ImportPreviewResp>(response)
}

export async function commitImport(file: File, alipayPassword = '', expectedLibraryId?: string, mappingToken?: string | null) {
  const body = makeFormData(file, alipayPassword)
  if (expectedLibraryId) body.append('expected_library_id', expectedLibraryId)
  if (mappingToken) body.append('mapping_token', mappingToken)
  const response = await fetch('/api/import/commit', {
    method: 'POST',
    body,
  })
  return parseResponse<ImportCommitResp>(response)
}

export async function getImportHistory() {
  const response = await fetch('/api/import/history')
  return parseResponse<ImportHistoryResp>(response)
}
