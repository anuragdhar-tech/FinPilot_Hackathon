const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const usdWhole = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
const usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1,
})

/** Integer cents -> "$1,234.56" (negative: "-$12.30"). */
export const money = (cents: number) => usd.format(cents / 100)
export const moneyWhole = (cents: number) => usdWhole.format(cents / 100)
export const moneyCompact = (cents: number) => usdCompact.format(cents / 100)

export const pct = (x: number | null | undefined, digits = 1) =>
  x == null ? 'n/a' : `${(x * 100).toFixed(digits)}%`

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "2026-03" -> "Mar 2026"; short: "Mar '26". */
export function monthLabel(ym: string, short = false) {
  const [y, m] = ym.split('-')
  const name = MONTHS[Number(m) - 1] ?? ym
  return short ? `${name} '${y.slice(2)}` : `${name} ${y}`
}

/** SQLite timestamps are UTC without a zone ("2026-10-15 09:30:00"); show them in local time. */
export function dateTime(ts: string | null | undefined) {
  if (!ts) return '—'
  const d = new Date(ts.includes('T') ? ts : `${ts.replace(' ', 'T')}Z`)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

export function duration(ms: number | null | undefined) {
  if (ms == null) return null
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

export const errorMessage = (e: unknown) => (e instanceof Error ? e.message : String(e))
