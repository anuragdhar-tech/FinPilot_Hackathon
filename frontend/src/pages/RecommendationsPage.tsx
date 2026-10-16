import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, type Recommendation } from '../api'
import { useUsers } from '../context/user-context'
import { useAsync } from '../hooks/useAsync'
import { IMPACT_HINT } from '../lib/advice'
import { moneyWhole, monthLabel } from '../lib/format'
import { ActionChip } from '../components/ActionChip'
import { AuditPanel } from '../components/AuditPanel'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'

function Card({ rec, userId, defaultOpen }: { rec: Recommendation; userId: number; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const revised = rec.attempts - 1
  return (
    <li className="rounded-lg border border-slate-200 bg-white">
      <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-800 text-xs font-semibold text-white" title="Rank by annual impact">
              {rec.rank}
            </span>
            <ActionChip action={rec.action} />
            <span className="text-sm font-medium text-slate-700">{rec.target}</span>
            {revised > 0 && (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-200">
                revised after {revised} rejection{revised === 1 ? '' : 's'}
              </span>
            )}
          </div>
          <h2 className="mt-3 text-lg font-semibold">{rec.title}</h2>
          <p className="mt-1 whitespace-pre-line text-sm text-slate-700">{rec.body}</p>
        </div>
        <div className="shrink-0 sm:text-right">
          <div className="text-2xl font-semibold tabular-nums">{moneyWhole(rec.impact_annual_cents)}</div>
          <div className="text-xs text-slate-500">annual impact</div>
          <div className="text-xs text-slate-400">{IMPACT_HINT[rec.action]}</div>
        </div>
      </div>
      <div className="border-t border-slate-100 px-5 py-3">
        <button
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="text-sm font-medium text-emerald-700 hover:underline"
        >
          {open ? '▾ Hide why this was recommended' : '▸ Why this was recommended'}
        </button>
      </div>
      {open && (
        <div className="border-t border-slate-100 bg-slate-50 p-5">
          <AuditPanel recId={rec.id} userId={userId} />
        </div>
      )}
    </li>
  )
}

export function RecommendationsPage() {
  const { selected, loading: usersLoading } = useUsers()
  const userId = selected?.id
  const load = useCallback(async () => {
    if (userId == null) return null
    try {
      return await api.getRecommendations(userId)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null
      throw e
    }
  }, [userId])
  const { data, error, loading, reload } = useAsync(load)
  const openId = (data?.items.find((r) => r.attempts > 1) ?? data?.items[0])?.id

  return (
    <div className="max-w-4xl">
      <PageTitle
        title="Recommendations"
        subtitle="Specific actions from the Advisor, each checked by the Verifier before it is shown. Ranked by the annual impact the Verifier recomputed from your transactions."
      />
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!error && !data && (loading || usersLoading) && <Loading label="Loading recommendations…" />}
      {!error && !loading && !usersLoading && userId == null && (
        <Empty title="No profile yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload transactions</Link> and run the pipeline to get recommendations.
        </Empty>
      )}
      {!error && userId != null && data === null && !loading && (
        <Empty title="No recommendations yet">
          Recommendations come from a finished pipeline run. Start one from{' '}
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload</Link>.
        </Empty>
      )}
      {data && !error && (
        <div className={loading ? 'opacity-60' : ''}>
          <p className="mb-4 text-sm text-slate-500">
            {data.run_id && (
              <>
                From run <Link to={`/runs/${data.run_id}`} className="font-mono text-emerald-700 hover:underline">{data.run_id.slice(0, 8)}</Link>
              </>
            )}
            {data.as_of_month && <> · numbers as of {monthLabel(data.as_of_month)}</>}
          </p>
          {data.items.length === 0 ? (
            <Empty title={data.run_id ? 'The latest run produced no recommendations' : 'No recommendations yet'}>
              {data.run_id
                ? 'The Advisor found nothing that the Verifier accepted. Open the run to see what it tried.'
                : 'Run the pipeline on this profile first.'}
            </Empty>
          ) : (
            <ul className="space-y-4">
              {data.items.map((r) => (
                // keyed on the run too, so a new run collapses the old audit panels; the first revised
                // recommendation (a rejection then an accepted redraft) starts open, else the top one
                <Card key={`${data.run_id}-${r.id}`} rec={r} userId={userId!} defaultOpen={r.id === openId} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
