import type { Action, Claim, ClaimKind } from '../api'
import { monthLabel, money } from './format'

export const ACTION_LABEL: Record<Action, string> = {
  cancel: 'Cancel',
  review_price_increase: 'Review price increase',
  reduce_spending: 'Reduce spending',
}

/** What the annual-impact figure means for each action (reduce_spending is NOT a saving: see the Advisor's wording rule). */
export const IMPACT_HINT: Record<Action, string> = {
  cancel: 'per year if cancelled',
  review_price_increase: 'more per year since the increase',
  reduce_spending: 'per year above your recent average',
}

export const KIND_LABEL: Record<ClaimKind, string> = {
  recurring_cost: 'Recurring cost',
  prev_recurring_cost: 'Price before the change',
  price_change: 'Price change',
  days_since_parent_spend: 'Days since other spend here',
  category_spend: 'Category spend',
  excess_spend: 'Spend above recent average',
  percent_change: 'Change vs recent average',
  forecast_balance: 'Forecast balance (p50)',
  forecast_balance_p10: 'Forecast balance (p10)',
  projected_balance_delta: 'Balance gain if you act (p50)',
}

export function claimValue(unit: Claim['unit'], value: number | null | undefined): string {
  if (value == null) return '—'
  if (unit === 'cents') return money(value)
  if (unit === 'days') return `${value.toLocaleString()} days`
  return `${value}%`
}

/** "per month" / "per year" / "in Mar 2027" qualifiers carried by a claim. */
export function claimQualifier(c: Claim): string | null {
  const parts = [c.period ? `per ${c.period}` : null, c.month ? `in ${monthLabel(c.month)}` : null].filter(Boolean)
  return parts.length ? parts.join(', ') : null
}

export const RULE_LABEL: Record<string, string> = {
  schema: 'Well-formed draft',
  trigger: 'Cites a pattern from this run',
  target: 'Target matches the pattern',
  action: 'Evidence supports the action',
  required_claims: 'Has the required claims',
  numbers: 'Every number in the text has a claim',
}

/** The Verifier's failed-check names (eval by_check) in plain words. */
export function checkLabel(name: string): string {
  if (RULE_LABEL[name]) return RULE_LABEL[name]
  if (name.startsWith('claim:')) return `Claim recomputed wrongly: ${name.slice(6).replace(/_/g, ' ')}`
  return name
}
