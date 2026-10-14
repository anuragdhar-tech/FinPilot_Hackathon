import { useEffect, useState } from 'react'
import { api, type RunStatus } from '../api'

/** Poll GET /api/runs/{id} until the run is done or failed. Network blips are retried, not fatal. */
export function useRunPolling(runId: string | null, intervalMs = 1000) {
  const [run, setRun] = useState<RunStatus | null>(null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!runId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    const tick = () =>
      api.getRun(runId).then(
        (r) => {
          if (cancelled) return
          setRun(r)
          setError(null)
          if (r.status === 'pending' || r.status === 'running') timer = setTimeout(tick, intervalMs)
        },
        (e) => {
          if (cancelled) return
          setError(e instanceof Error ? e : new Error(String(e)))
          timer = setTimeout(tick, intervalMs * 3)
        },
      )

    timer = setTimeout(tick, 0)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [runId, intervalMs])

  // never show a previous run's data for a new id
  return { run: run?.run_id === runId ? run : null, error }
}
