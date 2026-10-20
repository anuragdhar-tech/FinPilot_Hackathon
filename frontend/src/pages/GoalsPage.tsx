import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, ApiError, type Forecast, type GoalMonth, type GoalProgress, type GoalStatus, type Recommendation } from '../api'
import { useUsers } from '../context/user-context'
import { useAsync } from '../hooks/useAsync'
import { useRunPolling } from '../hooks/useRunPolling'
import { claimQualifier, claimValue, KIND_LABEL } from '../lib/advice'
import { money, moneyCompact, monthLabel } from '../lib/format'
import { ActionChip } from '../components/ActionChip'
import { AuditPanel } from '../components/AuditPanel'
import { ChartCard, LegendRow, TipBox } from '../components/ChartCard'
import { StatusChip } from '../components/StatusChip'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'

const GRID = '#e2e8f0'
const AXIS_TEXT = { fontSize: 11, fill: '#64748b' }
const BRAND = '#059669'
const PLAN = '#64748b'
const BAND = '#94a3b8'
const card = 'rounded-lg border border-slate-200 bg-white p-5'
const input =
  'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500'
const button = 'rounded-md px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50'

const STATUS: Record<GoalStatus, { label: string; style: string; meaning: string }> = {
  ahead: {
    label: 'Ahead',
    style: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
    meaning: 'Even the low-end (p10) projection reaches the target by the target month.',
  },
  on_track: {
    label: 'On track',
    style: 'bg-sky-50 text-sky-800 ring-sky-200',
    meaning: 'The middle (p50) projection reaches the target; the low-end (p10) one does not.',
  },
  behind: {
    label: 'Behind',
    style: 'bg-amber-50 text-amber-900 ring-amber-300',
    meaning: 'The middle (p50) projection falls short of the target by the target month.',
  },
  achieved: { label: 'Achieved', style: 'bg-emerald-600 text-white ring-emerald-600', meaning: 'Saved the target by the target month.' },
  missed: { label: 'Missed', style: 'bg-rose-50 text-rose-800 ring-rose-200', meaning: 'Did not save the target by the target month.' },
}

function StatusBadge({ status, large = false }: { status: GoalStatus; large?: boolean }) {
  const s = STATUS[status]
  return (
    <span
      title={s.meaning}
      className={`inline-flex items-center rounded-full font-semibold ring-1 ring-inset ${s.style} ${large ? 'px-3 py-1 text-sm' : 'px-2 py-0.5 text-xs'}`}
    >
      {s.label}
    </span>
  )
}

const addMonths = (ym: string, n: number) => {
  const [y, m] = ym.split('-').map(Number)
  const k = y * 12 + (m - 1) + n
  return `${Math.floor(k / 12)}-${String((k % 12) + 1).padStart(2, '0')}`
}

const monthsFrom = (first: string, last: string) => {
  const out: string[] = []
  for (let m = first; m <= last; m = addMonths(m, 1)) out.push(m)
  return out
}

/** gap > 0 = short of the target on the middle projection; < 0 = ahead of it. */
const gapText = (gap: number) => (gap > 0 ? `${money(gap)} short` : gap < 0 ? `${money(-gap)} ahead` : 'exactly on target')

// ---- create ----

function CreateGoal({ userId, asOf, onCreated }: { userId: number; asOf: string; onCreated: () => void }) {
  const [name, setName] = useState('Emergency fund')
  const [amount, setAmount] = useState('')
  const [start, setStart] = useState(asOf)
  const [target, setTarget] = useState(addMonths(asOf, 6))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const amountOk = /^\d+(\.\d{1,2})?$/.test(amount.trim()) && Number(amount) > 0

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.createGoal(
        { name: name.trim(), target_cents: Math.round(Number(amount) * 100), target_month: target, start_month: start },
        userId,
      )
      onCreated()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className={`${card} max-w-2xl space-y-4`}>
      <div>
        <h2 className="text-sm font-semibold">Set a savings goal</h2>
        <p className="mt-1 text-xs text-slate-500">
          Savings are the growth of this account's balance from the end of the start month. Starting in an earlier month
          rewinds the profile's clock to that month, so you can replay the following months one at a time and watch the
          status and advice change.
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="text-sm">
          <span className="font-medium">Name</span>
          <input className={`${input} mt-1`} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />
        </label>
        <label className="text-sm">
          <span className="font-medium">Save (dollars)</span>
          <input className={`${input} mt-1`} inputMode="decimal" placeholder="2400" value={amount} onChange={(e) => setAmount(e.target.value)} />
          {amount && !amountOk && <span className="mt-1 block text-xs text-rose-700">Enter a positive amount, e.g. 2400 or 2400.50</span>}
        </label>
        <label className="text-sm">
          <span className="font-medium">Replay from (start month)</span>
          <input className={`${input} mt-1`} type="month" value={start} max={asOf} onChange={(e) => setStart(e.target.value)} required />
          <span className="mt-1 block text-xs text-slate-500">
            {start < asOf ? `Rewinds the clock from ${monthLabel(asOf)} to ${monthLabel(start)}.` : 'Starts at the profile\'s current month.'}
          </span>
        </label>
        <label className="text-sm">
          <span className="font-medium">By (target month)</span>
          <input className={`${input} mt-1`} type="month" value={target} min={addMonths(start, 1)} max={addMonths(start, 12)} onChange={(e) => setTarget(e.target.value)} required />
          <span className="mt-1 block text-xs text-slate-500">1 to 12 months after the start month (the forecast horizon).</span>
        </label>
      </div>
      {error != null && <ErrorBox error={error} />}
      <button type="submit" disabled={busy || !amountOk || !name.trim()} className={`${button} bg-emerald-600 text-white hover:bg-emerald-700`}>
        {busy ? 'Checking the goal…' : 'Create goal'}
      </button>
    </form>
  )
}

// ---- chart: planned vs saved vs projected ----

interface Row {
  month: string
  planned?: number
  saved?: number
  p50?: number
  p10?: number
}

function GoalChart({ progress, forecast }: { progress: GoalProgress; forecast: Forecast | null }) {
  const { goal, current, months } = progress
  const rows = useMemo(() => {
    const byMonth = new Map(months.map((m) => [m.month, m]))
    const out: Row[] = monthsFrom(goal.start_month, goal.target_month).map((month) => ({ month, saved: byMonth.get(month)?.saved_cents }))
    // the plan is a straight line from 0 at the start month to the target at the target month
    out[0].planned = 0
    out[out.length - 1].planned = goal.target_cents
    // projection: this month's stored forecast, as savings (balance minus the balance at the end of the start month)
    if (current && !['achieved', 'missed'].includes(current.status) && forecast && forecast.as_of_month === current.month) {
      const base = forecast.start_balance_cents - current.saved_cents
      const at = new Map(forecast.points.map((p) => [p.month, p]))
      for (const r of out) {
        const p = at.get(r.month)
        if (r.month === current.month) Object.assign(r, { p50: current.saved_cents, p10: current.saved_cents })
        else if (p) Object.assign(r, { p50: p.balance_p50 - base, p10: p.balance_p10 - base })
      }
    }
    return out
  }, [goal, current, months, forecast])

  const fmt = (v: number | undefined) => (v == null ? '—' : money(v))
  return (
    <ChartCard
      title="Saved vs plan"
      subtitle={
        <>
          Saved = balance growth since the end of {monthLabel(goal.start_month)}. The projection is this month's forecast (middle estimate
          and the low end of its 80% range).
        </>
      }
      legend={
        <LegendRow
          items={[
            { label: 'saved', color: BRAND },
            { label: 'plan (straight line)', color: PLAN },
            { label: 'projected, middle (p50)', color: BRAND },
            { label: 'projected, low end (p10)', color: BAND },
          ]}
        />
      }
      chart={
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke={GRID} />
            <XAxis dataKey="month" tickFormatter={(m: string) => monthLabel(m, true)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
            <YAxis tickFormatter={(v) => moneyCompact(v)} tickLine={false} axisLine={false} tick={AXIS_TEXT} width={56} />
            <ReferenceLine y={goal.target_cents} stroke={PLAN} strokeDasharray="2 4" label={{ value: 'target', position: 'insideTopLeft', fontSize: 11, fill: '#64748b' }} />
            <ReferenceLine y={0} stroke={GRID} />
            <Tooltip
              cursor={{ stroke: '#cbd5e1', strokeWidth: 1 }}
              content={({ active, label, payload }) => {
                const r = payload?.[0]?.payload as Row | undefined
                if (!active || !r) return null
                return (
                  <TipBox
                    title={monthLabel(String(label))}
                    rows={[
                      ...(r.saved != null ? [{ label: 'saved', value: money(r.saved), color: BRAND }] : []),
                      ...(r.p50 != null ? [{ label: 'projected, middle', value: money(r.p50), color: BRAND }] : []),
                      ...(r.p10 != null ? [{ label: 'projected, low end', value: money(r.p10), color: BAND }] : []),
                    ]}
                  />
                )
              }}
            />
            <Line dataKey="planned" stroke={PLAN} strokeWidth={1.5} strokeDasharray="6 4" dot={false} connectNulls isAnimationActive={false} />
            <Line dataKey="p10" stroke={BAND} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            <Line dataKey="p50" stroke={BRAND} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            <Line dataKey="saved" stroke={BRAND} strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      }
      table={{
        columns: ['Month', 'Saved', 'Projected (p50)', 'Projected (p10)'],
        rows: rows.map((r) => [monthLabel(r.month), fmt(r.saved), fmt(r.p50), fmt(r.p10)]),
      }}
    />
  )
}

// ---- stepper and month list ----

function Stepper({ progress }: { progress: GoalProgress }) {
  const checked = new Map(progress.months.map((m) => [m.month, m]))
  return (
    <ol className="flex flex-wrap gap-1.5" aria-label="Replay months">
      {monthsFrom(progress.goal.start_month, progress.goal.target_month).map((m) => {
        const c = checked.get(m)
        const isNow = m === progress.as_of_month
        const tone = !c
          ? 'border-dashed border-slate-300 text-slate-400'
          : c.status === 'behind' || c.status === 'missed'
            ? 'border-amber-300 bg-amber-50 text-amber-900'
            : 'border-emerald-300 bg-emerald-50 text-emerald-800'
        return (
          <li
            key={m}
            className={`rounded-md border px-2 py-1 text-xs tabular-nums ${tone} ${isNow ? 'ring-2 ring-emerald-600 ring-offset-1' : ''}`}
            title={c ? `${monthLabel(m)}: ${STATUS[c.status].label}` : `${monthLabel(m)}: not checked`}
            aria-current={isNow ? 'step' : undefined}
          >
            {monthLabel(m, true)}
          </li>
        )
      })}
    </ol>
  )
}

function GoalClaims({ rec }: { rec: Recommendation }) {
  const goalClaims = rec.claims.filter((c) => c.kind.startsWith('goal_'))
  if (!goalClaims.length) return null
  return (
    <ul className="mt-2 flex flex-wrap gap-1.5">
      {goalClaims.map((c, i) => (
        <li key={i} className="rounded-md bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
          {KIND_LABEL[c.kind]}: <span className="font-semibold tabular-nums">{claimValue(c.unit, c.value)}</span>
          {claimQualifier(c) && <span className="text-slate-500"> ({claimQualifier(c)})</span>}
        </li>
      ))}
    </ul>
  )
}

function AdviceCard({ rec, userId }: { rec: Recommendation; userId: number }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="rounded-md border border-slate-200 bg-white">
      <div className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <ActionChip action={rec.action} />
          <span className="text-sm font-medium text-slate-700">{rec.target}</span>
          {rec.attempts > 1 && <span className="text-xs text-amber-800">revised after {rec.attempts - 1} rejection{rec.attempts === 2 ? '' : 's'}</span>}
        </div>
        <h4 className="mt-2 font-semibold">{rec.title}</h4>
        <p className="mt-1 whitespace-pre-line text-sm text-slate-700">{rec.body}</p>
        <GoalClaims rec={rec} />
        <button onClick={() => setOpen(!open)} aria-expanded={open} className="mt-3 text-sm font-medium text-emerald-700 hover:underline">
          {open ? '▾ Hide why' : '▸ Why this was recommended'}
        </button>
      </div>
      {open && (
        <div className="border-t border-slate-100 bg-slate-50 p-4">
          <AuditPanel recId={rec.id} userId={userId} />
        </div>
      )}
    </li>
  )
}

function MonthRow({ m, userId, targetCents }: { m: GoalMonth; userId: number; targetCents: number }) {
  const drafting = m.run_status === 'running' || m.run_status === 'pending'
  return (
    <li className={card}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="text-base font-semibold">{monthLabel(m.month)}</h3>
        <StatusBadge status={m.status} />
        <span className="text-sm text-slate-600">
          saved {money(m.saved_cents)} of {money(targetCents)} · plan {money(m.planned_cents)} ·{' '}
          <span className={m.gap_cents > 0 ? 'font-medium text-amber-800' : 'font-medium text-emerald-700'}>{gapText(m.gap_cents)}</span>
        </span>
        <Link to={`/runs/${m.run_id}`} className="ml-auto font-mono text-xs text-emerald-700 hover:underline">
          run {m.run_id.slice(0, 8)}
        </Link>
      </div>
      {m.projected_p50_cents != null && (
        <p className="mt-1 text-xs text-slate-500">
          Projected savings at the target month: {money(m.projected_p50_cents)} middle, {money(m.projected_p10_cents ?? 0)} low end
          {m.projected_completion ? ` · reaches the target in ${monthLabel(m.projected_completion)}` : ' · does not reach the target within the forecast'}
        </p>
      )}
      <div className="mt-3 text-sm">
        {m.decision === 'advise' && (
          <p className="text-slate-600">
            <span className="font-medium text-slate-800">Advisor ran this month</span> ({m.reason}).
          </p>
        )}
        {m.decision === 'carry' && m.advice_from_month && (
          <p>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">carried from {m.advice_from_month}</span>{' '}
            <span className="text-slate-600">{m.reason}.</span>
          </p>
        )}
        {m.decision === 'final' && <p className="text-slate-600">{m.reason}.</p>}
      </div>
      {drafting && (
        <div role="status" className="mt-3 flex items-center gap-2 text-sm text-slate-500">
          <StatusChip status={m.run_status} /> The Advisor is drafting this month's advice and the Verifier is checking it…
        </div>
      )}
      {m.run_status === 'failed' && <p className="mt-3 text-sm text-rose-700">The advice run failed; open the run for the error.</p>}
      {!drafting && m.decision !== 'final' && (
        m.recommendations.length ? (
          <ul className="mt-3 space-y-3">
            {m.recommendations.map((r) => (
              <AdviceCard key={`${m.month}-${r.id}`} rec={r} userId={userId} />
            ))}
          </ul>
        ) : (
          m.run_status !== 'failed' && <p className="mt-2 text-sm text-slate-500">No recommendation passed the Verifier for this month.</p>
        )
      )}
    </li>
  )
}

// ---- page ----

export function GoalsPage() {
  const { selected, loading: usersLoading, refresh } = useUsers()
  const userId = selected?.id
  const load = useCallback(async () => {
    if (userId == null) return null
    const goals = await api.getGoals(userId)
    if (!goals.length) return { progress: null, forecast: null }
    const progress = await api.getGoalProgress(goals[0].id)
    let forecast: Forecast | null = null
    try {
      forecast = await api.getForecast(userId, 12)
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) throw e
    }
    return { progress, forecast }
  }, [userId])
  const { data, error, loading, reload } = useAsync(load)
  const progress = data?.progress ?? null
  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)

  // the latest check's advice runs in the background: poll its run, then reload the goal
  const cur = progress?.current
  const pendingRun = cur && (cur.run_status === 'running' || cur.run_status === 'pending') ? cur.run_id : null
  const { run } = useRunPolling(pendingRun)
  const finished = run && (run.status === 'done' || run.status === 'failed') ? run.run_id : null
  const reloadedFor = useRef<string | null>(null)
  useEffect(() => {
    if (finished && reloadedFor.current !== finished) {
      reloadedFor.current = finished
      reload()
    }
  }, [finished, reload])

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label)
    setActionError(null)
    try {
      await fn()
      await refresh() // the profile's clock moved
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(null)
    }
  }

  const onCreated = () => {
    void refresh()
    reload()
  }

  return (
    <div className="max-w-5xl">
      <PageTitle
        title="Goals"
        subtitle="A savings goal checked against the forecast every month. Status and gap are computed in code; the Advisor runs again only when the status or the actionable patterns change."
      />
      {error && <ErrorBox error={error} onRetry={reload} />}
      {!error && !data && (loading || usersLoading) && <Loading label="Loading goal…" />}
      {!error && !usersLoading && userId == null && (
        <Empty title="No profile yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload transactions</Link> and run the pipeline first.
        </Empty>
      )}
      {!error && data && !progress && selected && <CreateGoal userId={selected.id} asOf={selected.as_of_month} onCreated={onCreated} />}
      {!error && progress && userId != null && (
        <div className={`space-y-6 ${loading ? 'opacity-70' : ''}`}>
          <section className={card}>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold">{progress.goal.name}</h2>
                <p className="text-sm text-slate-600">
                  Save {money(progress.goal.target_cents)} from the end of {monthLabel(progress.goal.start_month)} to the end of{' '}
                  {monthLabel(progress.goal.target_month)}.
                </p>
              </div>
              {cur && (
                <div className="text-right">
                  <StatusBadge status={cur.status} large />
                  <div className="mt-1 text-xs text-slate-500">as of {monthLabel(cur.month)}</div>
                </div>
              )}
            </div>
            {cur && (
              <dl className="mt-4 grid gap-4 sm:grid-cols-4">
                <div>
                  <dt className="text-xs text-slate-500">Saved so far</dt>
                  <dd className="text-xl font-semibold tabular-nums">{money(cur.saved_cents)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Plan by now</dt>
                  <dd className="text-xl font-semibold tabular-nums">{money(cur.planned_cents)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">{cur.projected_p50_cents == null ? 'Result' : 'Gap at the target month (p50)'}</dt>
                  <dd className={`text-xl font-semibold tabular-nums ${cur.gap_cents > 0 ? 'text-amber-800' : 'text-emerald-700'}`}>{gapText(cur.gap_cents)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Reaches the target (p50)</dt>
                  <dd className="text-xl font-semibold">{cur.projected_completion ? monthLabel(cur.projected_completion) : cur.projected_p50_cents == null ? '—' : 'not within 12 months'}</dd>
                </div>
              </dl>
            )}
            {cur && <p className="mt-2 text-xs text-slate-500">{STATUS[cur.status].meaning}</p>}

            <div className="mt-5 border-t border-slate-100 pt-4">
              <Stepper progress={progress} />
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button
                  onClick={() => act('advance', () => api.advanceGoal(progress.goal.id))}
                  disabled={busy != null || !progress.next_month}
                  className={`${button} bg-emerald-600 text-white hover:bg-emerald-700`}
                >
                  {busy === 'advance' ? 'Checking…' : progress.next_month ? `Advance one month → ${monthLabel(progress.next_month)}` : 'Advance one month'}
                </button>
                <button
                  onClick={() => act('reset', () => api.resetGoal(progress.goal.id))}
                  disabled={busy != null || pendingRun != null || progress.as_of_month >= progress.latest_data_month}
                  className={`${button} border border-slate-300 bg-white text-slate-700 hover:bg-slate-50`}
                  title={`Jump the clock to ${monthLabel(progress.latest_data_month)}, the latest month with data`}
                >
                  {busy === 'reset' ? 'Resetting…' : 'Reset to latest'}
                </button>
                <button
                  onClick={() => {
                    if (window.confirm(`Delete "${progress.goal.name}" and its month-by-month checks? The profile's clock returns to the latest data month.`)) {
                      void act('delete', () => api.deleteGoal(progress.goal.id))
                    }
                  }}
                  disabled={busy != null || pendingRun != null}
                  className={`${button} ml-auto text-rose-700 hover:bg-rose-50`}
                >
                  Delete goal
                </button>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                Profile clock: {monthLabel(progress.as_of_month)} (data through {monthLabel(progress.latest_data_month)}). While this goal exists the
                Dashboard, Simulator and Recommendations show the profile as of this month, and uploads do not move the clock.
                {progress.advance_blocked && <> {progress.advance_blocked}</>}
              </p>
              {actionError != null && (
                <div className="mt-3">
                  <ErrorBox error={actionError} />
                </div>
              )}
            </div>
          </section>

          <GoalChart progress={progress} forecast={data?.forecast ?? null} />

          <section>
            <h2 className="mb-3 text-sm font-semibold">Month by month</h2>
            <ol className="space-y-4">
              {[...progress.months].reverse().map((m) => (
                <MonthRow key={m.month} m={m} userId={userId} targetCents={progress.goal.target_cents} />
              ))}
            </ol>
          </section>
        </div>
      )}
    </div>
  )
}
