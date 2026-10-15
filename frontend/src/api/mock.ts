import type { ApiClient } from './client'
import { ApiError } from './errors'
import type { AgentEvent, Category, EvalSummary, RunStatus, Transaction, User } from './types'

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
  getEvalLatest: () => delay(mockEval),
}
