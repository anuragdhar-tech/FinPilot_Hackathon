import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../api'
import { useRunPolling } from '../hooks/useRunPolling'
import { dateTime } from '../lib/format'
import { RunTimeline } from '../components/RunTimeline'
import { StatusChip } from '../components/StatusChip'
import { ErrorBox, Loading } from '../components/State'

export function RunDetailPage() {
  const { runId = '' } = useParams()
  const { run, error } = useRunPolling(runId)

  return (
    <div className="max-w-4xl">
      <Link to="/runs" className="text-sm text-emerald-700 hover:underline">
        ← All runs
      </Link>
      <h1 className="mt-2 text-2xl font-semibold">Run</h1>
      <p className="break-all font-mono text-xs text-slate-500">{runId}</p>

      {error && !run && (
        <div className="mt-6">
          <ErrorBox
            error={error instanceof ApiError && error.status === 404 ? 'Run not found. The backend database may have been reset.' : error}
          />
        </div>
      )}
      {!run && !error && <Loading label="Loading run…" />}
      {run && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-slate-200 bg-white p-4 text-sm">
            <StatusChip status={run.status} />
            <span className="text-slate-500">kind <b className="font-medium text-slate-800">{run.kind}</b></span>
            <span className="text-slate-500">as of <b className="font-medium text-slate-800">{run.as_of_month}</b></span>
            <span className="text-slate-500">started <b className="font-medium text-slate-800">{dateTime(run.started_at)}</b></span>
            <span className="text-slate-500">finished <b className="font-medium text-slate-800">{dateTime(run.finished_at)}</b></span>
          </div>
          {run.error && (
            <div className="mt-4">
              <ErrorBox error={run.error} />
            </div>
          )}
          {error && <p className="mt-3 text-sm text-amber-700">Connection problem while polling: {error.message} (retrying)</p>}
          <h2 className="mb-3 mt-8 text-sm font-semibold uppercase tracking-wide text-slate-500">Agent events</h2>
          <RunTimeline events={run.events} waiting={run.status === 'pending' || run.status === 'running'} />
        </>
      )}
    </div>
  )
}
