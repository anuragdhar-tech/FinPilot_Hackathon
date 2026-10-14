import type { ApiClient } from './client'
import type { Health, RunCreated, RunStatus, UploadResult } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) throw new Error(`${init?.method ?? 'GET'} ${path} -> ${res.status}`)
  return res.json() as Promise<T>
}

const post = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const realClient: ApiClient = {
  health: () => request<Health>('/api/health'),
  uploadTransactions: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request<UploadResult>('/api/transactions/upload', { method: 'POST', body: form })
  },
  createRun: (batchId) => request<RunCreated>('/api/runs', post({ batch_id: batchId })),
  getRun: (id) => request<RunStatus>(`/api/runs/${id}`),
}
