import { useCallback } from 'react'
import { api, ApiError, type CategorizationMetrics, type EvalSummary, type ForecastMetrics } from '../api'
import { useAsync } from '../hooks/useAsync'
import { dateTime, moneyWhole, pct } from '../lib/format'
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

/** Forecast backtest. `before` is the earlier method version (fc-v1), null when its result file is not available. */
function ForecastBacktest({ f, before }: { f: ForecastMetrics; before: ForecastMetrics | null }) {
  const active = f.method_version ?? 'forecast'
  const cents = (c: number | null | undefined) => (c == null ? 'n/a' : moneyWhole(c))
  const row = (label: string, cells: React.ReactNode[], strong = false) => (
    <tr className={strong ? 'bg-emerald-50/50 font-medium' : ''}>
      <td className="px-4 py-2">{label}</td>
      {cells.map((c, i) => <td key={i} className="px-4 py-2 text-right">{c}</td>)}
    </tr>
  )
  return (
    <section>
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Forecast backtest · development set</h2>
      <div role="note" className="mb-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <span className="font-semibold">Development set; fc-v2 was tuned after seeing the backtest.</span> Next-month total spend,
        {f.origins != null && <> {f.origins} forecasts (3 personas × origin months)</>}; too few to separate small differences.
      </div>
      <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium">Method</th>
              <th className="px-4 py-2 text-right font-medium">MAPE</th>
              <th className="px-4 py-2 text-right font-medium">Mean error</th>
              <th className="px-4 py-2 text-right font-medium">80% band coverage</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 tabular-nums">
            {active !== 'fc-v1' &&
              row(
                'fc-v1 (before the one-off allowance)',
                before
                  ? [pct(before.mape), cents(before.mean_error_cents), pct(before.coverage_80)]
                  : [<span key="m" className="text-xs italic text-slate-400">result file not available</span>, '', ''],
              )}
            {row(`${active} (current)`, [pct(f.mape), cents(f.mean_error_cents), pct(f.coverage_80)], true)}
            {row('Naive: last month', [pct(f.naive_mape), cents(f.baseline_mean_error_cents?.naive_last), 'no band'])}
            {row('Naive: 3-month mean', [pct(f.naive_mean_mape), cents(f.baseline_mean_error_cents?.naive_mean), 'no band'])}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        MAPE = mean absolute percentage error (lower is better). Mean error = forecast minus actual; negative means the forecast was
        too low. Coverage = share of next-month balances that fell inside the p10 to p90 range (80% would be well calibrated).
      </p>
    </section>
  )
}

function Results({ summary, c, before }: { summary: EvalSummary; c: CategorizationMetrics; before: ForecastMetrics | null }) {
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

      {summary.forecast && <ForecastBacktest f={summary.forecast} before={before} />}

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
      const latest = await api.getEvalLatest()
      // the earlier forecast method, for the before/after row; absent file is not an error
      let before: ForecastMetrics | null = null
      if (latest.forecast && latest.forecast.method_version !== 'fc-v1') {
        try {
          before = (await api.getEvalLatest('fc-v1')).forecast ?? null
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e
        }
      }
      return { latest, before }
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
      {data && !error && (data.latest.categorization ? <Results summary={data.latest} c={data.latest.categorization} before={data.before} /> : <Empty title="This result has no categorization metrics" />)}
    </div>
  )
}
