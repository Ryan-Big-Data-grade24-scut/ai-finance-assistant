/** 与 backend/schemas.py 的 Import*Resp 及 /api/import/history 保持一致。 */
export interface ImportPreviewResp {
  batch_id: string
  mapping_token?: string | null
  platform_id: string
  platform_name: string
  confidence: number
  total: number
  by_status: Record<string, number>
  by_direction: Record<string, number>
  date_from: string
  date_to: string
  amount_sum: number
  sample: Array<Record<string, unknown>>
  warnings: string[]
}

export interface ImportCommitResp {
  batch_id: string
  inserted: number
  skipped: number
  failed: number
  first_error: string | null
  date_from: string
  date_to: string
}

export interface ImportBatch {
  batch_id: string
  platform_id: string
  platform_name: string | null
  filename: string | null
  total: number
  inserted: number
  skipped: number
  failed: number
  date_from: string | null
  date_to: string | null
  created_at: string
}

export interface ImportHistoryResp {
  batches: ImportBatch[]
}
