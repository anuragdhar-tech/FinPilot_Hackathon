// Shapes mirror the backend API contract (backend/models.py).
// Pydantic models are the source of truth; align these once they exist.

export interface Health {
  status: string
}

export interface UploadResult {
  batch_id: string
  count: number
}

export interface RunCreated {
  run_id: string
}

export interface AgentEvent {
  node: string
  input_ref: string | null
  output_ref: string | null
  ts: string
}

export interface RunStatus {
  run_id: string
  status: 'pending' | 'running' | 'done' | 'failed'
  events: AgentEvent[]
}
