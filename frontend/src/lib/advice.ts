import type { Action, Claim, ClaimKind, Recommendation } from '../api'
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

const addMonths = (ym: string, n: number) => {
  const [y, m] = ym.split('-').map(Number)
  const k = y * 12 + (m - 1) + n
  return `${Math.floor(k / 12)}-${String((k % 12) + 1).padStart(2, '0')}`
}

/** The card's headline figure. reduce_spending: the verifier-checked p50 balance gain 6 months after as_of
 *  (its annual figure is spend above the recent average, not a saving); otherwise the annual impact. */
export function headline(rec: Recommendation, asOf: string | null | undefined): { cents: number; label: string; hint: string | null } {
  if (rec.action === 'reduce_spending' && asOf) {
    const m6 = addMonths(asOf, 6)
    const gain = rec.claims.find((c) => c.kind === 'projected_balance_delta' && c.month === m6)
    if (gain) return { cents: gain.value, label: 'projected balance gain in 6 months if the rise stops', hint: null }
  }
  return { cents: rec.impact_annual_cents, label: 'annual impact', hint: IMPACT_HINT[rec.action] }
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
  goal_target: 'Goal target',
  goal_saved: 'Saved toward the goal so far',
  goal_gap: 'Goal gap at the target month (p50)',
  goal_gap_after: 'Goal gap left if you act (p50)',
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
