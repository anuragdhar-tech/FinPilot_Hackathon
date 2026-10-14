import type { ApiClient } from './client'

const delay = <T,>(value: T, ms = 150) =>
  new Promise<T>((resolve) => setTimeout(() => resolve(value), ms))

export const mockClient: ApiClient = {
  health: () => delay({ status: 'ok (mock)' }),
  uploadTransactions: () => delay({ batch_id: 'mock-batch', count: 0 }),
  createRun: () => delay({ run_id: 'mock-run' }),
  getRun: (id) => delay({ run_id: id, status: 'pending' as const, events: [] }),
}
