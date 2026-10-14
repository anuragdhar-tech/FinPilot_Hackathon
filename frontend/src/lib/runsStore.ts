// The API has no "list runs" endpoint yet, so runs started from this browser are remembered locally
// (ids only; their status and events are always fetched live from GET /api/runs/{id}).

const KEY = 'finpilot.runs'
const MAX = 30

export interface RememberedRun {
  run_id: string
  batch_id: string
  user_id: number
  started_at: string // ISO, from this browser's clock
}

export function loadRuns(): RememberedRun[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function rememberRun(run: RememberedRun) {
  try {
    localStorage.setItem(KEY, JSON.stringify([run, ...loadRuns()].slice(0, MAX)))
  } catch {
    // storage unavailable; the run is still reachable by URL
  }
}

export function forgetRuns() {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // nothing to do
  }
}
