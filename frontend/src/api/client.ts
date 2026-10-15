import { mockClient } from './mock'
import { realClient } from './real'
import type {
  EvalSummary,
  Forecast,
  Health,
  RunCreated,
  RunStatus,
  TransactionList,
  UploadResult,
  User,
  UserDeleted,
} from './types'

export interface UploadInput {
  file: File
  /** Append to this user. Omit to create a new profile. */
  userId?: number
  name?: string
  openingBalanceCents?: number
}

// Add one method per backend endpoint as its screen is built.
export interface ApiClient {
  health(): Promise<Health>
  getUsers(): Promise<User[]>
  deleteUser(userId: number): Promise<UserDeleted>
  uploadTransactions(input: UploadInput): Promise<UploadResult>
  createRun(batchId: string): Promise<RunCreated>
  getRun(runId: string): Promise<RunStatus>
  getTransactions(userId?: number): Promise<TransactionList>
  /** forecastVersion: take the forecast metrics from that committed method version (e.g. 'fc-v1'). */
  getEvalLatest(forecastVersion?: string): Promise<EvalSummary>
  getForecast(userId?: number, horizonMonths?: number): Promise<Forecast>
}

// Real backend by default; set VITE_USE_MOCKS=true for the mocked API.
export const useMocks = import.meta.env.VITE_USE_MOCKS === 'true'

export const api: ApiClient = useMocks ? mockClient : realClient
