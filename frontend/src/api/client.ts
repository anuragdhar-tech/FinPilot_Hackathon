import { mockClient } from './mock'
import { realClient } from './real'
import type {
  EvalSummary,
  Forecast,
  AuditRecord,
  Health,
  RecommendationList,
  RunCreated,
  RunStatus,
  SimulateRequest,
  SimulateResponse,
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
  /** Accepted recommendations of the latest finished main run, ranked. */
  getRecommendations(userId?: number): Promise<RecommendationList>
  /** Every draft of the recommendation's lineage with its verifier checks. */
  getAudit(recId: number): Promise<AuditRecord>
  /** Synchronous what-if (one parse call and up to two explanation calls). 422 = text is not a valid scenario. */
  simulate(body: SimulateRequest, userId?: number): Promise<SimulateResponse>
}

// Real backend by default; set VITE_USE_MOCKS=true for the mocked API.
export const useMocks = import.meta.env.VITE_USE_MOCKS === 'true'

export const api: ApiClient = useMocks ? mockClient : realClient
