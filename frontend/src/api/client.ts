import { mockClient } from './mock'
import { realClient } from './real'
import type {
  EvalSummary,
  Forecast,
  AuditRecord,
  Goal,
  GoalAdvance,
  GoalCreate,
  GoalDeleted,
  GoalProgress,
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
  /** The profile's goal, if any (at most one). */
  getGoals(userId?: number): Promise<Goal[]>
  /** Create the goal and check it at its start month (an earlier start month rewinds the clock). 409: one exists. */
  createGoal(body: GoalCreate, userId?: number): Promise<GoalAdvance>
  getGoalProgress(goalId: number): Promise<GoalProgress>
  /** Replay the next month. The status is in the response; advice arrives via the returned run. */
  advanceGoal(goalId: number): Promise<GoalAdvance>
  /** Jump the clock to the latest data month (one check there unless the goal is final). */
  resetGoal(goalId: number): Promise<GoalAdvance>
  /** Remove the goal and its checks; the clock goes back to the latest data month. */
  deleteGoal(goalId: number): Promise<GoalDeleted>
}

// Real backend by default; set VITE_USE_MOCKS=true for the mocked API.
export const useMocks = import.meta.env.VITE_USE_MOCKS === 'true'

export const api: ApiClient = useMocks ? mockClient : realClient
