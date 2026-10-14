// Aggregations for the dashboard, computed from GET /api/transactions. Pure functions, no I/O.
//
// Definitions (shown under the charts):
//  - Income: inflows categorized "income".
//  - Spend: outflows in every other category except "transfers" (moving money between your own
//    accounts is not spending), net of refunds. Uncategorized outflows count as spend.
//  - Balance: opening balance plus every transaction (transfers included) up to that day.
import type { Transaction } from '../api'
import { CHART_CATEGORIES } from './categories'

const SKIP_SPEND = new Set(['income', 'transfers'])

/** Spend contribution of one transaction in cents (positive = money spent). */
export function spendOf(t: Transaction): number {
  if (t.category != null && SKIP_SPEND.has(t.category)) return 0
  if (t.category == null && t.amount_cents > 0) return 0 // an uncategorized inflow could be anything
  return -t.amount_cents
}

export const incomeOf = (t: Transaction) => (t.category === 'income' && t.amount_cents > 0 ? t.amount_cents : 0)

export const OTHER_KEY = 'other_group'
export const STACK_KEYS = [...CHART_CATEGORIES, OTHER_KEY] as const

export interface MonthRow {
  month: string
  [category: string]: number | string
}

const monthOf = (t: Transaction) => t.date.slice(0, 7)
const sortedMonths = (txns: Transaction[]) => [...new Set(txns.map(monthOf))].sort()

/** Per month: spend for each charted category, everything else (and uncategorized) folded into OTHER_KEY. */
export function spendByCategory(txns: Transaction[]): MonthRow[] {
  const rows = new Map<string, MonthRow>(
    sortedMonths(txns).map((m) => [m, { month: m, ...Object.fromEntries(STACK_KEYS.map((k) => [k, 0])) }]),
  )
  for (const t of txns) {
    const s = spendOf(t)
    if (!s) continue
    const key = (CHART_CATEGORIES as readonly string[]).includes(t.category ?? '') ? t.category! : OTHER_KEY
    const row = rows.get(monthOf(t))!
    row[key] = (row[key] as number) + s
  }
  return [...rows.values()]
}

/** Full table behind the stacked chart: every spend category (and uncategorized) by month, in cents. */
export function spendTable(txns: Transaction[]) {
  const months = sortedMonths(txns)
  const byCat = new Map<string, Map<string, number>>()
  for (const t of txns) {
    const s = spendOf(t)
    if (!s) continue
    const cat = t.category ?? 'uncategorized'
    if (!byCat.has(cat)) byCat.set(cat, new Map())
    const m = byCat.get(cat)!
    m.set(monthOf(t), (m.get(monthOf(t)) ?? 0) + s)
  }
  const rows = [...byCat.entries()]
    .map(([category, m]) => ({ category, values: months.map((mo) => m.get(mo) ?? 0), total: [...m.values()].reduce((a, b) => a + b, 0) }))
    .sort((a, b) => b.total - a.total)
  return { months, rows }
}

export function incomeVsSpend(txns: Transaction[]) {
  const rows = new Map(sortedMonths(txns).map((m) => [m, { month: m, income: 0, spend: 0 }]))
  for (const t of txns) {
    const r = rows.get(monthOf(t))!
    r.income += incomeOf(t)
    r.spend += spendOf(t)
  }
  return [...rows.values()]
}

const PREFIX = /^(CHECKCARD \d{4}\s+|POS PURCHASE\s+|PURCHASE AUTHORIZED ON \d\d\/\d\d\s+)/
const PHONE = /\b\d{3}-\d{3}-\d{4}\b/g

/** Cleaned bank descriptor used to group a merchant: card prefixes, phone numbers and any token containing a digit are dropped. */
export function cleanDescriptor(description: string): string {
  const d = description.toUpperCase().trim().replace(PREFIX, '').replace(PHONE, ' ').replaceAll('*', ' * ')
  const key = d
    .split(/\s+/)
    .filter((tok) => tok && !/\d/.test(tok))
    .join(' ')
    .replaceAll(' * ', '*')
    .replaceAll(' *', '*')
    .replaceAll('* ', '*')
    .replace(/^[ *#-]+|[ *#-]+$/g, '')
  return key || description.toUpperCase().trim()
}

export function topMerchants(txns: Transaction[], limit = 8) {
  const groups = new Map<string, { merchant: string; spend: number; count: number }>()
  for (const t of txns) {
    const s = spendOf(t)
    if (s <= 0) continue
    const merchant = cleanDescriptor(t.description)
    const g = groups.get(merchant) ?? { merchant, spend: 0, count: 0 }
    g.spend += s
    g.count += 1
    groups.set(merchant, g)
  }
  return [...groups.values()].sort((a, b) => b.spend - a.spend).slice(0, limit)
}

/** End-of-day balance for every day that has transactions. */
export function balanceSeries(txns: Transaction[], openingCents: number) {
  const perDay = new Map<string, number>()
  for (const t of txns) perDay.set(t.date, (perDay.get(t.date) ?? 0) + t.amount_cents)
  let balance = openingCents
  return [...perDay.keys()].sort().map((date) => ({ date, balance: (balance += perDay.get(date)!) }))
}
