import { useCallback } from 'react'
import { api, ApiError } from '../api'
import { useUsers } from '../context/user-context'
import { useAsync } from './useAsync'

/** Transactions of the selected profile (sent as ?user_id=). `data` is null when nothing is uploaded yet. */
export function useProfileTransactions() {
  const { selected, loading: usersLoading } = useUsers()
  const userId = selected?.id
  const load = useCallback(async () => {
    if (userId == null) return null
    try {
      return await api.getTransactions(userId)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null // no data uploaded yet
      throw e
    }
  }, [userId])
  return { selected, usersLoading, ...useAsync(load) }
}
