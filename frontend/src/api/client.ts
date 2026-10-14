import { mockClient } from './mock'
import { realClient } from './real'
import type { Health, RunCreated, RunStatus, UploadResult } from './types'

// Add one method per PLAN.md endpoint as its screen is built.
export interface ApiClient {
  health(): Promise<Health>
  uploadTransactions(file: File): Promise<UploadResult>
  createRun(batchId: string): Promise<RunCreated>
  getRun(runId: string): Promise<RunStatus>
}

export const useMocks = import.meta.env.VITE_USE_MOCKS !== 'false'

export const api: ApiClient = useMocks ? mockClient : realClient
