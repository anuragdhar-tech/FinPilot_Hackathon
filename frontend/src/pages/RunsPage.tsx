import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { api, type RunStatus } from '../api'
import { useAsync } from '../hooks/useAsync'
import { dateTime, errorMessage } from '../lib/format'
import { forgetRuns, loadRuns } from '../lib/runsStore'
import { StatusChip } from '../components/StatusChip'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'

type Row = { run_id: string; run?: RunStatus; error?: string }

export function RunsPage() {
  const load = useCallback(async (): Promise<Row[]> => {
    const remembered = loadRuns()
    const settled = await Promise.allSettled(remembered.map((r) => api.getRun(r.run_id)))
    return settled.map((s, i) =>
      s.status === 'fulfilled'
        ? { run_id: remembered[i].run_id, run: s.value }
        : { run_id: remembered[i].run_id, error: errorMessage(s.reason) },
    )
  }, [])
  const { data, error, loading, reload } = useAsync(load)

  return (
    <div className="max-w-5xl">
      <PageTitle
        title="Agent Runs"
        subtitle="Open a run to see every step the agents took. Lists runs started from this browser (the API has no list-runs endpoint yet); open any run by URL: /runs/<run id>."
      />
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!data && loading && <Loading label="Loading runs…" />}
      {data && data.length === 0 && (
        <Empty title="No runs yet">
          Start one from <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload</Link>.
        </Empty>
      )}
      {data && data.length > 0 && (
        <div className={loading ? 'opacity-60' : ''}>
          <div className="mb-3 flex justify-end gap-2">
            <button onClick={reload} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50">
              Refresh
            </button>
            <button
              onClick={() => {
                forgetRuns()
                reload()
              }}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              Clear list
            </button>
          </div>
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-medium">Started</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Kind</th>
                  <th className="px-4 py-2 font-medium">Events</th>
                  <th className="px-4 py-2 font-medium">Run</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.map(({ run_id, run, error: rowError }) => (
                  <tr key={run_id} className="hover:bg-slate-50">
                    <td className="px-4 py-2">{run ? dateTime(run.started_at) : '—'}</td>
                    <td className="px-4 py-2">{run ? <StatusChip status={run.status} /> : <span className="text-rose-700">{rowError}</span>}</td>
                    <td className="px-4 py-2">{run?.kind ?? '—'}</td>
                    <td className="px-4 py-2 tabular-nums">{run?.events.length ?? '—'}</td>
                    <td className="px-4 py-2">
                      <Link to={`/runs/${run_id}`} className="font-mono text-xs text-emerald-700 hover:underline">
                        {run_id.slice(0, 8)}… open
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
