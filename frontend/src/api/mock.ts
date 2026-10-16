import type { ApiClient } from './client'
import { ApiError } from './errors'
import type {
  AgentEvent, AuditRecord, Category, Claim, EvalSummary, Forecast, RecommendationList, RunStatus, SimulateResponse,
  Transaction, User,
} from './types'

// Mock mode: everything below is invented sample data for working without the backend.
// Nothing here is a real result.

const delay = <T,>(value: T, ms = 150) =>
  new Promise<T>((resolve) => setTimeout(() => resolve(value), ms))

const CATALOG: [description: string, category: Category, cents: number][] = [
  ['ELM ST PROPERTY MGMT WEB PMTS', 'housing', -215000],
  ['ACME ANALYTICS INC PAYROLL PPD ID: 9025122', 'income', 218540],
  ['TRADER JOES #450', 'groceries', -5479],
  ['SAFEWAY STORE 1211', 'groceries', -8412],
  ['CHECKCARD 1002 UBER *TRIP JW25MAZ4AA', 'transport', -1522],
  ['SQ *BLUE BTL COF 6979', 'dining', -675],
  ['DOORDASH*SWEETGREEN', 'food_delivery', -2340],
  ['NETFLIX.COM', 'entertainment', -1549],
  ['PGANDE WEB ONLINE 10186876', 'utilities', -5562],
  ['TARGET 61751816 OAKLAND CA', 'shopping', -4754],
  ['ONLINE TRANSFER TO SAV XXXXXX', 'transfers', -30000],
]

function makeTransactions(seed: number, perMonth = 1): Transaction[] {
  let s = seed
  const rand = () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296
  const out: Transaction[] = []
  const seen = new Set<string>()
  for (let m = 0; m < 12; m++) {
    const year = 2025 + Math.floor((9 + m) / 12)
    const month = ((9 + m) % 12) + 1
    for (let rep = 0; rep < perMonth; rep++) {
      CATALOG.forEach(([description, category, cents], i) => {
        const day = 1 + ((i * 2 + rep * 7 + m) % 27)
        const jitter = category === 'housing' || category === 'income' ? 1 : 0.7 + rand() * 0.6
        const id = out.length + 1
        const uncategorized = id % 53 === 0
        const lowConf = id % 29 === 0
        out.push({
          id,
          external_id: `mock${String(id).padStart(5, '0')}`,
          date: `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
          description,
          amount_cents: Math.round(cents * jitter),
          category: uncategorized ? null : category,
          confidence: uncategorized ? null : lowConf ? 0.5 : 0.9,
          rationale: uncategorized ? null : `${description.split(' ')[0]} is a ${category.replace('_', ' ')} merchant.`,
          model: uncategorized ? null : 'mock-model',
          prompt_version: uncategorized ? null : 'cat-mock',
          from_cache: uncategorized ? null : seen.has(description),
        })
        seen.add(description)
      })
    }
  }
  return out.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.id - a.id))
}

let users: User[] = [
  { id: 1, name: 'maya_renter (mock)', opening_balance_cents: 420000, opening_date: '2025-10-01', as_of_month: '2026-09', txn_count: 0, is_active: false },
  { id: 2, name: 'sam_freelancer (mock)', opening_balance_cents: 1800000, opening_date: '2025-10-01', as_of_month: '2026-09', txn_count: 0, is_active: true },
]
const txns: Record<number, Transaction[]> = { 1: makeTransactions(1), 2: makeTransactions(2, 2) }
const syncCounts = () => users.forEach((u) => (u.txn_count = txns[u.id]?.length ?? 0))
syncCounts()

const batches = new Map<string, number>() // batch_id -> user_id
const polls = new Map<string, number>() // run_id -> times polled
let counter = 0

function mockEvents(upTo: number, startedAt: string): AgentEvent[] {
  const base = { input_ref: null, output_ref: null, prompt_tokens: null, completion_tokens: null, ts: startedAt }
  const all: AgentEvent[] = [
    { ...base, seq: 1, node: 'ingest', attempt: 1, status: 'ok', summary: 'loaded 672 transactions (mock)', model: null, duration_ms: 12 },
    { ...base, seq: 2, node: 'categorize.batch', attempt: 2, status: 'error', summary: 'representatives: 25 items, 2 attempt(s), 2 retried, 0 failed | 2 missing/invalid', model: 'mock-model', prompt_tokens: 1068, completion_tokens: 366, duration_ms: 5122 },
    { ...base, seq: 3, node: 'categorize.batch', attempt: 1, status: 'ok', summary: 'confirmations: 8 items, 1 attempt(s), 0 retried, 0 failed', model: 'mock-model', prompt_tokens: 540, completion_tokens: 210, duration_ms: 2310 },
    { ...base, seq: 4, node: 'categorize', attempt: 1, status: 'ok', summary: 'mock: 672/672 categorized', model: 'mock-model', prompt_tokens: 1608, completion_tokens: 576, duration_ms: 7600 },
  ]
  return all.slice(0, upTo)
}

const mockEval: EvalSummary = {
  run_at: '2026-01-01T00:00:00+00:00',
  git_sha: 'mock',
  dataset: 'mock data (not real results)',
  seed: null,
  models: { categorizer: 'mock-model' },
  categorization: {
    accuracy: 0.9,
    lenient_accuracy: 0.92,
    macro_f1: 0.88,
    cache_hit_rate: 0.7,
    n: 100,
    uncategorized: 0,
    top_confusions: [{ true: 'dining', predicted: 'food_delivery', count: 4 }],
    per_category: {
      dining: { precision: 0.95, recall: 0.8, f1: 0.87, support: 20 },
      food_delivery: { precision: 0.8, recall: 0.95, f1: 0.87, support: 10 },
    },
    per_persona: null,
  },
  verifier: {
    first_draft_fail_rate: 0.333, drafts: 24, rejected: 6, lineages: 18, accepted: 18, dropped: 0,
    by_check: { numbers: 6 },
  },
  grounded_number_rate: 0.94,
  forecast: {
    mape: 0.11, naive_mape: 0.13, naive_mean_mape: 0.115, no_trend_mape: 0.112, mean_error_cents: -40000,
    baseline_mean_error_cents: { no_trend: -41000, naive_last: -7600, naive_mean: -28500 },
    method_version: 'fc-v2', coverage_80: 0.54, origins: 24, horizon: 1,
  },
}

const mockForecast: Forecast = {
  id: 1, as_of_month: '2026-09', horizon_months: 6, method: 'mock', start_balance_cents: 900000,
  points: ['2026-10', '2026-11', '2026-12', '2027-01', '2027-02', '2027-03'].map((month, k) => ({
    month, balance_p50: 900000 + 30000 * (k + 1), balance_p10: 900000 + 30000 * (k + 1) - 60000 * Math.sqrt(k + 1),
    balance_p90: 900000 + 30000 * (k + 1) + 60000 * Math.sqrt(k + 1), net_flow_cents: 30000,
    income_cents: 470000, spend_cents: 440000,
    by_category: { housing: 215000, groceries: 90000, dining: 60000, utilities: 40000, one_off: 35000 },
  })),
}

const claim = (kind: Claim['kind'], value: number, unit: Claim['unit'], ref: string, extra: Partial<Claim> = {}): Claim =>
  ({ kind, value, unit, source_ref: ref, month: null, period: null, ...extra })

const mockRecs: RecommendationList = {
  run_id: 'mock-run-main', as_of_month: '2026-09',
  items: [
    {
      id: 11, lineage_id: 'mock-lin-1', action: 'cancel', target: 'UBER*ONE MEMBERSHIP',
      title: 'Cancel Uber One ($9.99/month)',
      body: 'No other Uber spend for 288 days. Cancelling would leave your balance $59.94 higher by 2027-03.',
      claims: [], trigger_type: 'pattern', trigger_id: '3', goal_id: null, rank: 1, impact_annual_cents: 11988,
      attempts: 2, created_at: '2026-10-15 09:30:00',
    },
    {
      id: 14, lineage_id: 'mock-lin-2', action: 'review_price_increase', target: 'SPOTIFY USA',
      title: 'Spotify went up $1.00/month', body: 'The price rose by $1.00 per month. Check whether the plan is still worth it.',
      claims: [], trigger_type: 'pattern', trigger_id: '2', goal_id: null, rank: 2, impact_annual_cents: 1200,
      attempts: 1, created_at: '2026-10-15 09:30:05',
    },
  ],
}

const mockAudit = (recId: number): AuditRecord => {
  const ref = 'pattern:3'
  const good = [claim('recurring_cost', 999, 'cents', ref, { period: 'month' }),
    claim('days_since_parent_spend', 288, 'days', ref),
    claim('projected_balance_delta', 5994, 'cents', 'forecast:1', { month: '2027-03' })]
  const bad = [claim('recurring_cost', 1299, 'cents', ref, { period: 'month' }), good[2]]
  return {
    recommendation_id: recId, lineage_id: 'mock-lin-1', run_id: 'mock-run-main', trigger_type: 'pattern', trigger_id: '3',
    trigger: { title: 'UBER*ONE MEMBERSHIP: $9.99/month, no other UBER spend for 288 days', evidence_txn_ids: (txns[2] ?? []).slice(0, 4).map((t) => t.id) },
    inputs: { pattern_ids: [3], forecast_id: 1, as_of_month: '2026-09' }, model: 'mock-model', prompt_version: 'mock',
    attempts: [
      {
        attempt: 1, rec_id: recId - 1, status: 'rejected', action: 'cancel', target: 'UBER*ONE MEMBERSHIP',
        title: 'Cancel Uber One ($12.99/month)', body: 'Cancelling would leave your balance $59.94 higher by 2027-03, about $150 a year.',
        claims: bad, verdict: 'reject',
        reasons: ['claim recurring_cost (pattern:3): claimed 1299, recomputed 999', "numbers: '$150' in the text matches no claim"],
        checks: [
          { claim: bad[0], recomputed: 999, tolerance: 1, ok: false, note: 'claimed 1299, recomputed 999' },
          { claim: bad[1], recomputed: 5994, tolerance: 0, ok: true, note: null },
        ],
        rule_checks: [
          { rule: 'schema', ok: true, note: '' }, { rule: 'trigger', ok: true, note: '' }, { rule: 'target', ok: true, note: '' },
          { rule: 'action', ok: true, note: '' }, { rule: 'required_claims', ok: true, note: '' },
          { rule: 'numbers', ok: false, note: "'$150' in the text matches no claim" },
        ],
        ts: '2026-10-15 09:29:40',
      },
      {
        attempt: 2, rec_id: recId, status: 'accepted', action: 'cancel', target: 'UBER*ONE MEMBERSHIP',
        title: 'Cancel Uber One ($9.99/month)', body: 'No other Uber spend for 288 days. Cancelling would leave your balance $59.94 higher by 2027-03.',
        claims: good, verdict: 'pass', reasons: [],
        checks: [
          { claim: good[0], recomputed: 999, tolerance: 1, ok: true, note: null },
          { claim: good[1], recomputed: 288, tolerance: 0, ok: true, note: null },
          { claim: good[2], recomputed: 5994, tolerance: 0, ok: true, note: null },
        ],
        rule_checks: ['schema', 'trigger', 'target', 'action', 'required_claims', 'numbers'].map((rule) => ({ rule: rule as 'schema', ok: true, note: '' })),
        ts: '2026-10-15 09:29:52',
      },
    ],
  }
}

const mockSimulate = (text: string): SimulateResponse => {
  const base = mockForecast.points
  const proj = base.map((p, k) => (k >= 1
    ? { ...p, balance_p10: p.balance_p10 - 200000, balance_p50: p.balance_p50 - 200000, balance_p90: p.balance_p90 - 200000 }
    : p))
  return {
    scenario: { items: [{ kind: 'one_off', label: text.slice(0, 40), amount_cents: -200000, start_month: '2026-11', months: null }] },
    baseline: base, projected: proj, end_delta_cents: -200000, verdict: 'affordable',
    lowest_p50: { month: '2026-11', cents: proj[1].balance_p50 }, lowest_p10: { month: '2026-11', cents: proj[1].balance_p10 },
    baseline_forecast_id: 1, scenario_forecast_id: 2, run_id: 'mock-run-whatif',
    explanation: 'Mock explanation: the purchase keeps even the low-end balance above $0.00.', explanation_source: 'llm',
    number_check: { passed: true, issues: [] },
  }
}

export const mockClient: ApiClient = {
  health: () => delay({ status: 'ok (mock)' }),
  getUsers: () => delay(users.map((u) => ({ ...u }))),
  deleteUser: async (userId) => {
    const user = users.find((u) => u.id === userId)
    if (!user) throw new ApiError(404, 'no such user')
    users = users.filter((u) => u.id !== userId)
    delete txns[userId]
    return delay({ user_id: userId, deleted_rows: { transactions: user.txn_count, users: 1 } })
  },
  uploadTransactions: async ({ file, userId, name, openingBalanceCents }) => {
    const rows = Math.max(0, (await file.text()).trim().split('\n').length - 1)
    let id = userId
    if (id == null) {
      id = Math.max(0, ...users.map((u) => u.id)) + 1
      users = [...users.map((u) => ({ ...u, is_active: false })), {
        id, name: name || file.name.replace(/\.csv$/i, ''), opening_balance_cents: openingBalanceCents ?? 0,
        opening_date: '2025-10-01', as_of_month: '2026-09', txn_count: 0, is_active: true,
      }]
    }
    const added = makeTransactions(id * 7).slice(0, Math.min(rows, 60))
    txns[id] = [...(txns[id] ?? []), ...added]
    syncCounts()
    const batch_id = `mock-batch-${++counter}`
    batches.set(batch_id, id)
    return delay({ batch_id, user_id: id, count: added.length, duplicates_skipped: rows - added.length })
  },
  createRun: async (batchId) => {
    if (!batches.has(batchId)) throw new ApiError(404, `batch ${batchId} not found`)
    const run_id = `mock-run-${++counter}`
    polls.set(run_id, 0)
    return delay({ run_id })
  },
  getRun: async (runId) => {
    const n = polls.get(runId)
    if (n == null) throw new ApiError(404, 'run not found')
    polls.set(runId, n + 1)
    const started = new Date().toISOString()
    const status: RunStatus['status'] = n === 0 ? 'pending' : n < 3 ? 'running' : 'done'
    return delay({
      run_id: runId, kind: 'main', status, as_of_month: '2026-09', started_at: started,
      finished_at: status === 'done' ? started : null, error: null,
      events: mockEvents(n === 0 ? 0 : Math.min(n + 1, 4), started),
    })
  },
  getTransactions: async (userId) => {
    const id = userId ?? users.find((u) => u.is_active)?.id
    if (id == null || !txns[id]) throw new ApiError(404, 'no data uploaded yet')
    return delay({ items: txns[id], total: txns[id].length })
  },
  getEvalLatest: (forecastVersion) =>
    delay(forecastVersion === 'fc-v1' && mockEval.forecast
      ? { ...mockEval, forecast: { ...mockEval.forecast, method_version: 'fc-v1', mape: 0.123, mean_error_cents: -64000 } }
      : mockEval),
  getRecommendations: () => delay(mockRecs),
  getAudit: async (recId) => {
    if (!mockRecs.items.some((r) => r.id === recId)) throw new ApiError(404, 'recommendation not found')
    return delay(mockAudit(recId))
  },
  simulate: async (body) => {
    if (!('text' in body)) return delay(mockSimulate('scenario'), 600)
    if (!/\$\s?\d/.test(body.text)) {
      throw new ApiError(422, 'not a what-if scenario: no purchase or amount found', ['not a what-if scenario: no purchase or amount found'])
    }
    return delay(mockSimulate(body.text), 600)
  },
  getForecast: (_userId, horizonMonths = 6) =>
    delay({ ...mockForecast, horizon_months: horizonMonths, points: mockForecast.points.slice(0, horizonMonths) }),
}
