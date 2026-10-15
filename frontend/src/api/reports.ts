import type {
  CalendarResp,
  CategoryResp,
  IncomeRankResp,
  MethodResp,
  PaginatedResp,
  ReportParams,
  SummaryResp,
  TrendResp,
  TxDetailResp,
} from '@/types/report'

function queryString(params: Record<string, string | number | boolean | undefined>) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return query.toString()
}

async function getJson<T>(path: string, params: Record<string, string | number | boolean | undefined>): Promise<T> {
  const response = await fetch(`${path}?${queryString(params)}`)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export function getReportBundle(params: ReportParams) {
  const common = { ...params }
  return Promise.all([
    getJson<SummaryResp>('/api/report/summary', common),
    getJson<CalendarResp>('/api/report/calendar', common),
    getJson<TrendResp>('/api/report/trend', { ...common, n: 6 }),
    params.type === 'expense'
      ? getJson<CategoryResp>('/api/report/by-category', common)
      : Promise.resolve<CategoryResp>({ total: 0, majors: [] }),
    params.type === 'expense'
      ? getJson<MethodResp>('/api/report/by-method', common)
      : Promise.resolve<MethodResp>({ groups: [] }),
    params.type === 'income'
      ? getJson<IncomeRankResp>('/api/report/income-rank', { period: params.period, value: params.value })
      : Promise.resolve<IncomeRankResp>({ items: [] }),
    getJson<PaginatedResp>('/api/report/transactions', { ...common, page: 1, size: 8 }),
  ])
}

export function getTransactions(
  params: ReportParams,
  filters: {
    category?: string
    subcat?: string
    pay_method?: string
    date_from?: string
    date_to?: string
    page?: number
    size?: number
  },
) {
  return getJson<PaginatedResp>('/api/report/transactions', {
    ...params,
    ...filters,
    page: filters.page ?? 1,
    size: filters.size ?? 50,
  })
}

export async function getTxDetail(id: number): Promise<TxDetailResp> {
  const response = await fetch(`/api/tx/${id}`)
  if (!response.ok) throw new Error(`交易详情加载失败（${response.status}）`)
  return response.json() as Promise<TxDetailResp>
}
