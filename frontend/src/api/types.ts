// Shapes mirror the backend API contract (backend/models.py).
// Pydantic models are the source of truth; keep these aligned with them.

export const CATEGORIES = [
  'income', 'housing', 'utilities', 'groceries', 'dining', 'food_delivery', 'transport',
  'shopping', 'entertainment', 'health', 'fitness', 'travel', 'transfers', 'fees', 'other',
] as const
export type Category = (typeof CATEGORIES)[number]

export interface Health {
  status: string
}

// ---- users / upload ----

export interface User {
  id: number
  name: string
  opening_balance_cents: number
  opening_date: string
  as_of_month: string
  txn_count: number
  is_active: boolean
}

export interface UserDeleted {
  user_id: number
  deleted_rows: Record<string, number>
}

export interface UploadResult {
  batch_id: string
  user_id: number
  count: number
  duplicates_skipped: number
}

// ---- runs ----

export interface RunCreated {
  run_id: string
}

export type EventStatus = 'ok' | 'rejected' | 'error'
export type RunState = 'pending' | 'running' | 'done' | 'failed'

export interface AgentEvent {
  seq: number
  node: string
  attempt: number
  status: EventStatus
  summary: string
  input_ref: string | null
  output_ref: string | null
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  duration_ms: number | null
  ts: string
}

export interface RunStatus {
  run_id: string
  kind: 'main' | 'whatif' | 'goal_check'
  status: RunState
  as_of_month: string
  started_at: string
  finished_at: string | null
  error: string | null
  events: AgentEvent[]
}

// ---- transactions ----

export interface Transaction {
  id: number
  external_id: string
  date: string
  description: string
  amount_cents: number
  // category fields are all null when the row is uncategorized
  category: Category | null
  confidence: number | null
  rationale: string | null
  model: string | null
  prompt_version: string | null
  from_cache: boolean | null
}

export interface TransactionList {
  items: Transaction[]
  total: number
}

// ---- forecast ----

export interface ForecastPoint {
  month: string
  balance_p10: number
  balance_p50: number
  balance_p90: number
  net_flow_cents: number
  income_cents: number
  spend_cents: number // positive = money out
  // projected spend per category (positive cents). `one_off` is the allowance for one-off spending.
  by_category: Record<string, number>
}

export interface Forecast {
  id: number
  as_of_month: string
  horizon_months: number
  method: string
  start_balance_cents: number
  points: ForecastPoint[] // empty when insufficient_data
  insufficient_data: boolean // under 3 complete months of history: no projection and no advice
  reason: string | null
}

// ---- what-if ----

export interface ScenarioItem {
  kind: 'one_off' | 'recurring_change'
  label: string
  amount_cents: number // signed: negative = money out
  start_month: string
  months: number | null // recurring_change only; null = open-ended
}

export interface Scenario {
  items: ScenarioItem[]
}

export type SimulateRequest = { text: string; horizon_months?: number } | { scenario: Scenario; horizon_months?: number }

export type Verdict = 'affordable' | 'tight' | 'not_affordable'

export interface LowPoint {
  month: string
  cents: number
}

export interface NumberCheck {
  passed: boolean // the LLM explanation's numbers all matched code-computed facts
  issues: string[] // every failed attempt's unmatched numbers, also when the template was used
}

export interface SimulateResponse {
  scenario: Scenario // what the parser understood
  baseline: ForecastPoint[]
  projected: ForecastPoint[]
  end_delta_cents: number
  verdict: Verdict
  lowest_p50: LowPoint
  lowest_p10: LowPoint
  baseline_forecast_id: number
  scenario_forecast_id: number
  run_id: string
  explanation: string
  explanation_source: 'llm' | 'template'
  number_check: NumberCheck
}

// ---- recommendations / audit ----

export type ClaimKind =
  | 'recurring_cost' | 'prev_recurring_cost' | 'price_change' | 'days_since_parent_spend'
  | 'category_spend' | 'excess_spend' | 'percent_change'
  | 'forecast_balance' | 'forecast_balance_p10' | 'projected_balance_delta'
  | 'goal_target' | 'goal_saved' | 'goal_gap' | 'goal_gap_after'
export type Action = 'cancel' | 'review_price_increase' | 'reduce_spending'
export type TriggerType = 'pattern' | 'forecast' | 'goal' | 'scenario'

export interface Claim {
  kind: ClaimKind
  value: number // cents, days or integer percent (see unit)
  unit: 'cents' | 'days' | 'percent'
  source_ref: string
  month: string | null
  period: 'month' | 'year' | null // year = 12 x the monthly figure
}

export interface Recommendation {
  id: number
  lineage_id: string
  action: Action
  target: string
  title: string
  body: string
  claims: Claim[]
  trigger_type: TriggerType
  trigger_id: string
  goal_id: number | null
  rank: number // 1 = largest verifier-recomputed annual impact
  impact_annual_cents: number // recomputed by the Verifier, not taken from the LLM
  attempts: number // drafts in this lineage (2+ = revised after a rejection)
  created_at: string
}

export interface RecommendationList {
  items: Recommendation[] // accepted only, ranked
  run_id: string | null // null = no finished run yet
  as_of_month: string | null
}

export interface ClaimCheck {
  claim: Claim
  recomputed: number | null
  tolerance: number
  ok: boolean
  note: string | null
}

export type RuleName = 'schema' | 'trigger' | 'target' | 'action' | 'required_claims' | 'numbers'

export interface RuleCheck {
  rule: RuleName
  ok: boolean
  note: string
}

export type DraftStatus = 'draft' | 'rejected' | 'accepted' | 'dropped'

export interface AuditAttempt {
  attempt: number
  rec_id: number
  status: DraftStatus
  action: string
  target: string
  title: string
  body: string
  claims: Claim[]
  verdict: 'pass' | 'reject'
  reasons: string[]
  checks: ClaimCheck[]
  rule_checks: RuleCheck[]
  ts: string
}

export interface AuditRecord {
  recommendation_id: number
  lineage_id: string
  run_id: string
  trigger_type: TriggerType
  trigger_id: string
  trigger: { title: string; evidence_txn_ids: number[] } | null
  inputs: { pattern_ids: number[]; forecast_id: number | null; as_of_month: string }
  model: string
  prompt_version: string
  attempts: AuditAttempt[] // every draft of the lineage, rejected ones included
}

// ---- eval ----

export interface CategoryScore {
  precision: number
  recall: number
  f1: number
  support: number
}

export interface Confusion {
  true: Category
  predicted: Category
  count: number
}

export interface CategorizationMetrics {
  accuracy: number // strict
  macro_f1: number
  top_confusions: Confusion[]
  lenient_accuracy: number | null
  n: number | null
  uncategorized: number | null
  cache_hit_rate: number | null
  per_category: Record<string, CategoryScore> | null
  per_persona: Record<string, number> | null
}

export interface ForecastMetrics {
  mape: number // next-month total spend, rolling origin
  naive_mape: number // last month's spend
  naive_mean_mape: number | null // 3-month trailing mean
  no_trend_mape: number | null
  mean_error_cents: number | null // forecast minus actual; negative = under-forecast
  baseline_mean_error_cents: { no_trend?: number; naive_last?: number; naive_mean?: number } | null
  method_version: string | null
  coverage_80: number | null
  origins: number | null
  horizon: number | null
}

export interface VerifierMetrics {
  first_draft_fail_rate: number // lineages rejected on attempt 1 / lineages
  drafts: number
  rejected: number
  lineages: number | null
  accepted: number | null
  dropped: number | null // 3 rejected attempts, or withdrawn by the Advisor after a rejection
  by_check: Record<string, number> | null // first-draft failures per check
}

export interface EvalSummary {
  run_at: string
  git_sha: string
  dataset: string
  seed: number | null
  models: Record<string, string>
  categorization: CategorizationMetrics | null
  forecast?: ForecastMetrics | null
  verifier?: VerifierMetrics | null
  grounded_number_rate?: number | null
}

// ---- goals (one per profile; the goal owns the profile's clock while it exists) ----

export type GoalStatus = 'ahead' | 'on_track' | 'behind' | 'achieved' | 'missed'

export interface Goal {
  id: number
  user_id: number
  name: string
  target_cents: number
  start_month: string // savings = balance growth since the end of this month
  target_month: string
  created_at: string
}

export interface GoalCreate {
  name: string
  target_cents: number
  target_month: string
  start_month?: string | null // earlier than the profile's month = rewind the clock for replay
}

/** One goal check (one replayed month); every figure is computed in code from that month's forecast. */
export interface GoalMonth {
  month: string
  status: GoalStatus
  saved_cents: number
  planned_cents: number
  projected_p50_cents: number | null // projected savings at the target month; null once final
  projected_p10_cents: number | null
  gap_cents: number // target - projected p50 savings (final: target - saved); negative = ahead
  projected_completion: string | null
  run_id: string
  run_status: RunState
  decision: 'advise' | 'carry' | 'final'
  reason: string
  advice_from_month: string | null
  recommendations: Recommendation[]
}

export interface GoalProgress {
  goal: Goal
  as_of_month: string
  latest_data_month: string
  next_month: string | null
  advance_blocked: string | null
  current: GoalMonth | null
  months: GoalMonth[] // oldest first
}

export interface GoalAdvance {
  run_id: string | null
  progress: GoalProgress
}

export interface GoalDeleted {
  goal_id: number
  as_of_month: string
  deleted_rows: Record<string, number>
}
