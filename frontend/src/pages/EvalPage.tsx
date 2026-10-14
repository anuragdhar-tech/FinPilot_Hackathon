import { useCallback } from 'react'
import { api, ApiError, type CategorizationMetrics, type EvalSummary } from '../api'
import { useAsync } from '../hooks/useAsync'
import { dateTime, pct } from '../lib/format'
import { CategoryChip } from '../components/CategoryChip'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'

function Tile({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-3xl font-semibold">{value}</div>
      <div className="mt-1 text-xs text-slate-400">{hint}</div>
    </div>
  )
}

function Results({ summary, c }: { summary: EvalSummary; c: CategorizationMetrics }) {
  const rows = Object.entries(c.per_category ?? {}).sort(([, a], [, b]) => b.support - a.support)
  return (
    <div className="space-y-6">
      <div role="note" className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <span className="font-semibold">Development set (prompt tuned on this data).</span> Treat these scores as optimistic, not as
        a held-out estimate.
      </div>

      <p className="text-sm text-slate-600">
        dataset <b className="font-medium text-slate-900">{summary.dataset}</b>
        {summary.seed != null && <> (seed {summary.seed})</>} · {c.n != null && <><b className="font-medium text-slate-900">{c.n.toLocaleString()}</b> transactions · </>}
        {c.uncategorized != null && <>{c.uncategorized} uncategorized · </>}
        model <b className="font-medium text-slate-900">{summary.models.categorizer ?? '—'}</b> · generated {dateTime(summary.run_at)} · git {summary.git_sha}
      </p>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Categorization · development set</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Tile label="Strict accuracy" value={pct(c.accuracy)} hint="primary label only" />
          <Tile label="Lenient accuracy" value={pct(c.lenient_accuracy)} hint="also accepts listed alternatives" />
          <Tile label="Macro-F1" value={c.macro_f1.toFixed(3)} hint="mean F1 over categories, strict" />
          <Tile label="Cache hit rate" value={pct(c.cache_hit_rate)} hint="rows labelled from the merchant cache" />
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-5">
        <section className="lg:col-span-3">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Per category (strict, development set)</h2>
          {rows.length === 0 ? (
            <Empty title="No per-category scores in this result" />
          ) : (
            <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2 font-medium">Category</th>
                    <th className="px-4 py-2 text-right font-medium">Precision</th>
                    <th className="px-4 py-2 text-right font-medium">Recall</th>
                    <th className="px-4 py-2 text-right font-medium">F1</th>
                    <th className="px-4 py-2 text-right font-medium">Support</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {rows.map(([category, s]) => (
                    <tr key={category}>
                      <td className="px-4 py-2"><CategoryChip category={category} /></td>
                      <td className="px-4 py-2 text-right">{pct(s.precision)}</td>
                      <td className="px-4 py-2 text-right">{pct(s.recall)}</td>
                      <td className={`px-4 py-2 text-right ${s.f1 < 0.9 ? 'font-semibold text-amber-700' : ''}`}>{pct(s.f1)}</td>
                      <td className="px-4 py-2 text-right text-slate-600">{s.support.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Top confusion pairs</h2>
          {c.top_confusions.length === 0 ? (
            <Empty title="No confusions recorded" />
          ) : (
            <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2 font-medium">True → predicted</th>
                    <th className="px-4 py-2 text-right font-medium">Count</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {c.top_confusions.map((x) => (
                    <tr key={`${x.true}>${x.predicted}`}>
                      <td className="px-4 py-2">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <CategoryChip category={x.true} /> <span className="text-slate-400">→</span> <CategoryChip category={x.predicted} />
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right font-semibold tabular-nums">{x.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

export function EvalPage() {
  const load = useCallback(async () => {
    try {
      return await api.getEvalLatest()
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null // no results file yet
      throw e
    }
  }, [])
  const { data, error, loading, reload } = useAsync(load)

  return (
    <div className="max-w-6xl">
      <PageTitle title="Eval" subtitle="Self-evaluation numbers, produced by the eval script and served from its results file." />
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!error && !data && loading && <Loading label="Loading eval results…" />}
      {!error && data === null && !loading && (
        <Empty title="No eval results yet">
          Generate them with <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">python -m eval.categorization</code>.
        </Empty>
      )}
      {data && !error && (data.categorization ? <Results summary={data} c={data.categorization} /> : <Empty title="This result has no categorization metrics" />)}
    </div>
  )
}
