export function formatNumber(value: number | undefined | null) {
  return new Intl.NumberFormat('en-US').format(value ?? 0)
}

export function formatCurrencyCents(value: number | undefined | null) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Unknown'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value / 100)
}

export function formatDate(value: string | undefined | null, fallback = 'Not yet observed') {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return fallback
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' }).format(date)
}

export function formatDateTime(value: string | undefined | null, fallback = 'Not yet observed') {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return fallback
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date)
}

export function toDateTimeLocal(value: string | undefined | null) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

export function fromDateTimeLocal(value: string) {
  return value ? new Date(value).toISOString() : ''
}

export function titleCase(value: string) {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

export function truncate(value: string | undefined | null, length = 92) {
  if (!value) return '—'
  return value.length > length ? `${value.slice(0, length - 1)}…` : value
}

export function percent(spent: number, limit: number) {
  if (!limit) return 0
  return Math.min(100, Math.round((spent / limit) * 100))
}
