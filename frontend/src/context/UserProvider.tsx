import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, type User } from '../api'
import { UserContext } from './user-context'

const STORAGE_KEY = 'finpilot.userId'

function readStored(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw == null ? null : Number(raw)
  } catch {
    return null
  }
}

function store(id: number) {
  try {
    localStorage.setItem(STORAGE_KEY, String(id))
  } catch {
    // storage unavailable (private window); selection just won't survive a reload
  }
}

/** Remembered choice if it still exists, else the backend's active user, else the first. */
function pick(users: User[], wanted: number | null): number | null {
  if (wanted != null && users.some((u) => u.id === wanted)) return wanted
  return (users.find((u) => u.is_active) ?? users[0])?.id ?? null
}

export function UserProvider({ children }: { children: ReactNode }) {
  const [users, setUsers] = useState<User[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(readStored)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  const apply = useCallback((list: User[], selectId?: number) => {
    setUsers(list)
    setSelectedId((current) => {
      const next = pick(list, selectId ?? current)
      if (next != null) store(next)
      return next
    })
    setError(null)
    setLoading(false)
  }, [])

  const fail = useCallback((e: unknown) => {
    setError(e instanceof Error ? e : new Error(String(e)))
    setLoading(false)
  }, [])

  const refresh = useCallback(
    async (selectId?: number) => {
      try {
        apply(await api.getUsers(), selectId)
      } catch (e) {
        fail(e)
      }
    },
    [apply, fail],
  )

  useEffect(() => {
    let cancelled = false
    api.getUsers().then(
      (list) => !cancelled && apply(list),
      (e) => !cancelled && fail(e),
    )
    return () => {
      cancelled = true
    }
  }, [apply, fail])

  const select = useCallback((id: number) => {
    store(id)
    setSelectedId(id)
  }, [])

  const value = useMemo(
    () => ({ users, loading, error, selected: users.find((u) => u.id === selectedId) ?? null, select, refresh }),
    [users, loading, error, selectedId, select, refresh],
  )
  return <UserContext.Provider value={value}>{children}</UserContext.Provider>
}
