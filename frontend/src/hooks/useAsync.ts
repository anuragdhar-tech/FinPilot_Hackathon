import { useEffect, useState } from 'react'

interface Result<T> {
  load: () => Promise<T>
  tick: number
  data: T | null
  error: Error | null
}

/**
 * Run an async loader. Pass a loader memoised with useCallback: a new loader (or reload())
 * triggers a fetch. The previous data stays available while the new request is in flight so the
 * page does not flash, and a stale response can never overwrite a newer one.
 */
export function useAsync<T>(load: () => Promise<T>) {
  const [tick, setTick] = useState(0)
  const [result, setResult] = useState<Result<T> | null>(null)

  useEffect(() => {
    let cancelled = false
    load().then(
      (data) => {
        if (!cancelled) setResult({ load, tick, data, error: null })
      },
      (e) => {
        if (!cancelled) setResult({ load, tick, data: null, error: e instanceof Error ? e : new Error(String(e)) })
      },
    )
    return () => {
      cancelled = true
    }
  }, [load, tick])

  const settled = result?.load === load && result.tick === tick
  return {
    data: result?.data ?? null,
    error: settled ? result.error : null,
    loading: !settled,
    reload: () => setTick((t) => t + 1),
  }
}
