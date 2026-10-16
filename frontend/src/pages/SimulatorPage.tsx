import { useCallback, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, ApiError, type ForecastPoint, type ScenarioItem, type SimulateResponse, type Verdict } from '../api'
import { useUsers } from '../context/user-context'
import { useAsync } from '../hooks/useAsync'
import { errorMessage, money, moneyCompact, monthLabel, pct } from '../lib/format'
import { ChartCard, LegendRow, TipBox } from '../components/ChartCard'
import { ErrorBox, InsufficientData, Loading, PageTitle } from '../components/State'

const EXAMPLES = [
  'Can I afford a $2,000 trip next month?',
  'What if I buy a $1,200 laptop in December?',
  'What if my rent goes up by $150 a month starting January?',
]

const GRID = '#e2e8f0'
const AXIS_TEXT = { fontSize: 11, fill: '#64748b' }
const BRAND = '#059669'
const BASE = '#475569'
const BAND = '#a7f3d0'

const VERDICT: Record<Verdict, { label: string; badge: string; why: string }> = {
  affordable: {
    label: 'Affordable',
    badge: 'bg-emerald-50 text-emerald-800 ring-emerald-300',
    why: 'Even the low-end (p10) balance stays at or above $0 in every month.',
  },
  tight: {
    label: 'Tight',
    badge: 'bg-amber-50 text-amber-900 ring-amber-300',
    why: 'The expected (p50) balance stays at or above $0, but the low-end (p10) balance dips below it.',
  },
  not_affordable: {
    label: 'Not affordable',
    badge: 'bg-rose-50 text-rose-800 ring-rose-300',
    why: 'The expected (p50) balance falls below $0.',
  },
}

interface Row {
  month: string
  baseline: number
  scenario: number
  range: [number, number]
  p10: number
  p90: number
}

const toRows = (base: ForecastPoint[], proj: ForecastPoint[]): Row[] =>
  proj.map((p, i) => ({
    month: p.month,
    baseline: base[i]?.balance_p50 ?? p.balance_p50,
    scenario: p.balance_p50,
    range: [p.balance_p10, p.balance_p90],
    p10: p.balance_p10,
    p90: p.balance_p90,
  }))

function Tip({ active, label, payload }: { active?: boolean; label?: string | number; payload?: ReadonlyArray<{ payload?: Row }> }) {
  const r = payload?.[0]?.payload
  if (!active || !r) return null
  return (
    <TipBox
      title={monthLabel(String(label))}
      rows={[
        { label: 'with scenario, middle estimate', value: money(r.scenario), color: BRAND },
        { label: 'with scenario, 80% range low', value: money(r.p10) },
        { label: 'with scenario, 80% range high', value: money(r.p90) },
        { label: 'baseline, middle estimate', value: money(r.baseline), color: BASE },
        { label: 'difference', value: money(r.scenario - r.baseline) },
      ]}
    />
  )
}

function ScenarioLine({ it }: { it: ScenarioItem }) {
  const when =
    it.kind === 'one_off'
      ? `once, in ${monthLabel(it.start_month)}`
      : `per month from ${monthLabel(it.start_month)}${it.months ? ` for ${it.months} months` : ' onward'}`
  return (
    <li>
      <span className="font-medium">{it.label}</span>: {it.amount_cents < 0 ? 'spend' : 'receive'}{' '}
      <span className="tabular-nums">{money(Math.abs(it.amount_cents))}</span> {when}
    </li>
  )
}

function Result({ r, coverage }: { r: SimulateResponse; coverage: number | null }) {
  const v = VERDICT[r.verdict]
  const rows = toRows(r.baseline, r.projected)
  const rewritten = r.number_check.issues.length > 0
  return (
    <div className="mt-6 space-y-4">
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className={`rounded-full px-3 py-1 text-sm font-semibold ring-1 ring-inset ${v.badge}`}>{v.label}</span>
          <span className="text-sm text-slate-600">{v.why}</span>
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          <div>
            <div className="text-xs text-slate-500">Lowest expected balance (p50)</div>
            <div className="text-2xl font-semibold tabular-nums">{money(r.lowest_p50.cents)}</div>
            <div className="text-xs text-slate-500">in {monthLabel(r.lowest_p50.month)}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Lowest low-end balance (p10)</div>
            <div className="text-2xl font-semibold tabular-nums">{money(r.lowest_p10.cents)}</div>
            <div className="text-xs text-slate-500">in {monthLabel(r.lowest_p10.month)}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">Change vs. no scenario, end of horizon</div>
            <div className="text-2xl font-semibold tabular-nums">{money(r.end_delta_cents)}</div>
            <div className="text-xs text-slate-500">in {monthLabel(r.projected[r.projected.length - 1].month)}</div>
          </div>
        </div>

        <div className="mt-5 border-t border-slate-100 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold">Explanation</h2>
            <span
              className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600"
              title={r.explanation_source === 'llm' ? 'Written by the language model; every number was checked against the computed values.' : 'The model text failed the number check twice, so this sentence was written by code.'}
            >
              written by: {r.explanation_source === 'llm' ? 'model' : 'template'}
            </span>
          </div>
          <p className="mt-2 whitespace-pre-line text-sm text-slate-800">{r.explanation}</p>
          {rewritten && (
            <p className="mt-2 text-xs text-amber-800">
              Number check: {r.number_check.passed ? 'the first explanation contained numbers that were not computed values and was rewritten' : 'the model explanation failed the check twice and was replaced by the template'}.{' '}
              <span className="break-words text-slate-500">{r.number_check.issues.join('; ')}</span>
            </p>
          )}
        </div>

        <div className="mt-4 border-t border-slate-100 pt-4">
          <h2 className="text-sm font-semibold">What I understood</h2>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-slate-700">
            {r.scenario.items.map((it, i) => <ScenarioLine key={i} it={it} />)}
          </ul>
        </div>
      </section>

      <ChartCard
        title="Balance: baseline vs. with your scenario"
        subtitle={<>Next {r.projected.length} months. The green band is the 80% range (p10 to p90) with the scenario; the gray line is the unchanged baseline.</>}
        legend={
          <LegendRow
            items={[
              { label: 'with scenario, middle estimate', color: BRAND },
              { label: 'with scenario, 80% range', color: BAND },
              { label: 'baseline, middle estimate', color: BASE },
            ]}
          />
        }
        chart={
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke={GRID} />
              <XAxis dataKey="month" tickFormatter={(m: string) => monthLabel(m, true)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
              <YAxis
                tickFormatter={(v) => moneyCompact(v)}
                tickLine={false}
                axisLine={false}
                tick={AXIS_TEXT}
                width={56}
                domain={[(min: number) => Math.min(0, min), 'auto']}
              />
              <ReferenceLine y={0} stroke="#94a3b8" />
              <Tooltip content={(p) => Tip(p as Parameters<typeof Tip>[0])} cursor={{ stroke: '#cbd5e1', strokeWidth: 1 }} />
              <Area type="monotone" dataKey="range" stroke="none" fill={BAND} fillOpacity={0.6} isAnimationActive={false} />
              <Line type="monotone" dataKey="baseline" stroke={BASE} strokeWidth={2} strokeDasharray="5 3" dot={false} isAnimationActive={false} />
              <Line type="monotone" dataKey="scenario" stroke={BRAND} strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
        }
        table={{
          columns: ['Month', 'Baseline (p50)', 'Scenario low (p10)', 'Scenario middle (p50)', 'Scenario high (p90)', 'Difference'],
          rows: rows.map((x) => [monthLabel(x.month), money(x.baseline), money(x.p10), money(x.scenario), money(x.p90), money(x.scenario - x.baseline)]),
        }}
      />

      <p role="note" className="text-xs text-slate-500">
        The p10 to p90 range is optimistic: in the backtest the 80% range contained only{' '}
        {coverage != null ? <>about {pct(coverage, 0)}</> : 'a smaller share'} of actual balances, not 80%.{' '}
        <Link to={`/runs/${r.run_id}`} className="text-emerald-700 hover:underline">See the run</Link>
      </p>
    </div>
  )
}

function Simulator({ userId }: { userId: number }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<SimulateResponse | null>(null)
  const [error, setError] = useState<unknown>(null)
  const loadCoverage = useCallback(async () => {
    try {
      return (await api.getEvalLatest()).forecast?.coverage_80 ?? null
    } catch {
      return null // the note then says it without a number
    }
  }, [])
  const { data: coverage } = useAsync(loadCoverage)
  const loadForecast = useCallback(async () => {
    try {
      return await api.getForecast(userId)
    } catch {
      return null // no run yet or not reachable: the simulate call reports it
    }
  }, [userId])
  const { data: forecast } = useAsync(loadForecast)
  const tooLittle = forecast?.insufficient_data === true

  async function run(question: string) {
    const q = question.trim()
    if (!q || busy || tooLittle) return
    setBusy(true)
    setError(null)
    try {
      setResult(await api.simulate({ text: q }, userId))
    } catch (e) {
      setResult(null)
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    void run(text)
  }

  return (
    <div className="max-w-4xl">
      <PageTitle title="Simulator" subtitle="Ask a what-if in plain words. It is added to your forecast and the verdict comes from a fixed rule, not from the model." />

      {tooLittle && (
        <div className="mb-4">
          <InsufficientData reason={forecast.reason} />
        </div>
      )}

      <form onSubmit={onSubmit} className="rounded-lg border border-slate-200 bg-white p-5">
        <label htmlFor="whatif" className="text-sm font-medium">What do you want to try?</label>
        <textarea
          id="whatif"
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void run(text)
          }}
          placeholder="e.g. Can I afford a $2,000 trip next month?"
          className="mt-2 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-600 focus:outline-none focus:ring-1 focus:ring-emerald-600"
        />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="submit"
            disabled={!text.trim() || busy || tooLittle}
            className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy ? 'Simulating…' : 'Simulate'}
          </button>
          <span className="text-xs text-slate-400">or try:</span>
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              disabled={busy || tooLittle}
              onClick={() => {
                setText(ex)
                void run(ex)
              }}
              className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {ex}
            </button>
          ))}
        </div>
      </form>

      {busy && <Loading label="Reading your question and projecting the balance…" />}
      {error != null && !busy && <SimError error={error} onRetry={() => void run(text)} />}
      {result && !busy && <Result r={result} coverage={coverage ?? null} />}
    </div>
  )
}

function SimError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.status === 422) {
    return (
      <div role="alert" className="mt-6 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
        <div className="font-medium">I couldn't turn that into a what-if scenario</div>
        <p className="mt-1 break-words">{error.message}</p>
        <p className="mt-2 text-amber-800">Say what you would buy or change, how much, and when. For example: "Can I afford a $2,000 trip next month?"</p>
      </div>
    )
  }
  if (error instanceof ApiError && error.status === 404) {
    return (
      <div role="alert" className="mt-6 rounded-lg border border-slate-300 bg-white p-4 text-sm text-slate-700">
        <div className="font-medium">No forecast to simulate on yet</div>
        <p className="mt-1">
          {errorMessage(error)}. Run the pipeline from <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload</Link> first.
        </p>
      </div>
    )
  }
  return <div className="mt-6"><ErrorBox error={error} onRetry={onRetry} /></div>
}

export function SimulatorPage() {
  const { selected, loading } = useUsers()
  if (loading && !selected) return <Loading label="Loading profile…" />
  if (!selected) {
    return (
      <div className="max-w-4xl">
        <PageTitle title="Simulator" />
        <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
          No profile yet. <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload transactions</Link> and run the pipeline first.
        </div>
      </div>
    )
  }
  // keyed on the profile so switching profiles never shows another profile's result
  return <Simulator key={selected.id} userId={selected.id} />
}
