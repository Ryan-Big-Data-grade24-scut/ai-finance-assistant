export function isoWeek(date: Date) {
  const utc = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  utc.setUTCDate(utc.getUTCDate() + 4 - (utc.getUTCDay() || 7))
  const year = utc.getUTCFullYear()
  return { year, week: Math.ceil(((utc.getTime() - Date.UTC(year, 0, 1)) / 86400000 + 1) / 7) }
}

export function currentPeriodValue(period: string, date = new Date()) {
  if (period === 'week') {
    const { year, week } = isoWeek(date)
    return `${year}-W${String(week).padStart(2, '0')}`
  }
  const year = String(date.getFullYear())
  return period === 'year' ? year : `${year}-${String(date.getMonth() + 1).padStart(2, '0')}`
}
