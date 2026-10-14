import { createContext, useContext } from 'react'
import type { User } from '../api'

export interface UserContextValue {
  users: User[]
  loading: boolean
  error: Error | null
  /** The profile every read is scoped to (sent as ?user_id=). Null until one exists. */
  selected: User | null
  select: (id: number) => void
  /** Reload the profile list, optionally selecting a profile (e.g. the one just uploaded). */
  refresh: (selectId?: number) => Promise<void>
}

export const UserContext = createContext<UserContextValue | null>(null)

export function useUsers(): UserContextValue {
  const ctx = useContext(UserContext)
  if (!ctx) throw new Error('useUsers must be used inside <UserProvider>')
  return ctx
}
