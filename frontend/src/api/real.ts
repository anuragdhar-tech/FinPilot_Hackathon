import type { ApiClient } from './client'
import { ApiError } from './errors'
import type {
  EvalSummary,
  Forecast,
  AuditRecord,
  Goal,
  GoalAdvance,
  GoalDeleted,
  GoalProgress,
  Health,
  RecommendationList,
  RunCreated,
  RunStatus,
  SimulateResponse,
  TransactionList,
  UploadResult,
  User,
  UserDeleted,
} from './types'

/** FastAPI errors carry `detail` as a string, a list of validation errors, or {errors: [...]}. */
function describe(detail: unknown): string[] {
  if (typeof detail === 'string') return [detail]
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (typeof d === 'string') return d
      if (d && typeof d === 'object' && 'msg' in d) {
        const loc = 'loc' in d && Array.isArray(d.loc) ? `${d.loc.join('.')}: ` : ''
        return `${loc}${String(d.msg)}`
      }
      return JSON.stringify(d)
    })
  }
  if (detail && typeof detail === 'object' && 'errors' in detail && Array.isArray(detail.errors)) {
    return detail.errors.map(String)
  }
  return []
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch {
    throw new ApiError(0, 'Cannot reach the backend. Is uvicorn running on port 8000?')
  }
  if (!res.ok) {
    let details: string[] = []
    try {
      details = describe((await res.json()).detail)
    } catch {
      // body was not JSON (e.g. the dev proxy's own error page)
    }
    const message =
      details[0] ??
      (res.status >= 500
        ? `Request failed (${res.status}). Is the backend running on port 8000?`
        : `${init?.method ?? 'GET'} ${path} -> ${res.status}`)
    throw new ApiError(res.status, message, details)
  }
  return res.json() as Promise<T>
}

const post = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const realClient: ApiClient = {
  health: () => request<Health>('/api/health'),
  getUsers: async () => (await request<{ items: User[] }>('/api/users')).items,
  deleteUser: (userId) => request<UserDeleted>(`/api/users/${userId}`, { method: 'DELETE' }),
  uploadTransactions: ({ file, userId, name, openingBalanceCents }) => {
    const form = new FormData()
    form.append('file', file)
    if (userId != null) form.append('user_id', String(userId))
    if (name) form.append('name', name)
    if (openingBalanceCents != null) form.append('opening_balance_cents', String(openingBalanceCents))
    return request<UploadResult>('/api/transactions/upload', { method: 'POST', body: form })
  },
  createRun: (batchId) => request<RunCreated>('/api/runs', post({ batch_id: batchId })),
  getRun: (id) => request<RunStatus>(`/api/runs/${id}`),
  getTransactions: (userId) =>
    request<TransactionList>(`/api/transactions${userId != null ? `?user_id=${userId}` : ''}`),
  getEvalLatest: (forecastVersion) =>
    request<EvalSummary>(`/api/eval/latest${forecastVersion ? `?forecast_version=${forecastVersion}` : ''}`),
  getForecast: (userId, horizonMonths = 6) =>
    request<Forecast>(`/api/forecast?horizon_months=${horizonMonths}${userId != null ? `&user_id=${userId}` : ''}`),
  getRecommendations: (userId) =>
    request<RecommendationList>(`/api/recommendations${userId != null ? `?user_id=${userId}` : ''}`),
  getAudit: (recId) => request<AuditRecord>(`/api/recommendations/${recId}/audit`),
  simulate: (body, userId) =>
    request<SimulateResponse>(`/api/simulate${userId != null ? `?user_id=${userId}` : ''}`, post(body)),
  getGoals: async (userId) =>
    (await request<{ items: Goal[] }>(`/api/goals${userId != null ? `?user_id=${userId}` : ''}`)).items,
  createGoal: (body, userId) =>
    request<GoalAdvance>(`/api/goals${userId != null ? `?user_id=${userId}` : ''}`, post(body)),
  getGoalProgress: (goalId) => request<GoalProgress>(`/api/goals/${goalId}/progress`),
  advanceGoal: (goalId) => request<GoalAdvance>(`/api/goals/${goalId}/advance`, post({})),
  resetGoal: (goalId) => request<GoalAdvance>(`/api/goals/${goalId}/reset`, post({})),
  deleteGoal: (goalId) => request<GoalDeleted>(`/api/goals/${goalId}`, { method: 'DELETE' }),
}
