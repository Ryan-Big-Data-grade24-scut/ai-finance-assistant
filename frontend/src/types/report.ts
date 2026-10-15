export type TxType = 'expense' | 'income'
export type Period = 'week' | 'month' | 'year'

export interface ReportParams {
  type: TxType
  period: Period
  value: string
}

export interface SummaryResp {
  period: string
  total: number
  prev: number
  delta: number
  rate: number | null
  count: number
}

export interface DayPoint {
  date: string
  amount: number
  count: number
}

export interface CalendarResp {
  days: DayPoint[]
}

export interface TrendBar {
  label: string
  amount: number
  count: number
}

export interface TrendResp {
  bars: TrendBar[]
  avg: number
  n: number
}

export interface MinorGroup {
  minor: string
  amount: number
  pct: number
  count: number
  tx_ids: number[]
}

export interface MajorGroup {
  major: string
  amount: number
  pct: number
  count: number
  minors: MinorGroup[]
}

export interface CategoryResp {
  total: number
  majors: MajorGroup[]
}

export interface MethodGroup {
  pay_method: string
  amount: number
  count: number
}

export interface MethodResp {
  groups: MethodGroup[]
}

export interface RankItem {
  tx_id: number
  amount: number
  date: string
  tx_time: string
  counterparty: string
  tx_type: string
  source: string
}

export interface IncomeRankResp {
  items: RankItem[]
}

export interface TxItem {
  id: number
  source: string
  tx_time: string
  date: string
  direction: 'expense' | 'income' | 'neutral'
  amount: number
  pay_method: string
  counterparty: string
  item: string
  category: string
  subcat: string
  is_exempt: boolean
  funding: string
  note: string
  is_overridden: boolean
}

export interface PaginatedResp {
  total: number
  page: number
  size: number
  items: TxItem[]
}

export interface OverrideResp {
  tx_id: number
  manual_category: string | null
  manual_subcat: string | null
  is_exempt: boolean
  exempt_reason: string | null
  funding: string
  note: string | null
  updated_by: string
  updated_at: string
}

export interface TxDetailResp {
  raw: TxItem
  override: OverrideResp | null
}
