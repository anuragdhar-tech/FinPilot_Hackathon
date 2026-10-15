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

export interface EvalSummary {
  run_at: string
  git_sha: string
  dataset: string
  seed: number | null
  models: Record<string, string>
  categorization: CategorizationMetrics | null
}
