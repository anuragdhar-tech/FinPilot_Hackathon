import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, LabelList, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { CHART_CATEGORIES, OTHER_COLOR, categoryColor, categoryLabel } from '../lib/categories'
import { balanceSeries, incomeVsSpend, OTHER_KEY, spendByCategory, spendOf, spendTable, STACK_KEYS, topMerchants } from '../lib/dashboard'
import { money, moneyCompact, moneyWhole, monthLabel } from '../lib/format'
import { useProfileTransactions } from '../hooks/useProfileTransactions'
import { ChartCard, LegendRow, TipBox } from '../components/ChartCard'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'

const SURFACE = '#ffffff'
const GRID = '#e2e8f0'
const AXIS_TEXT = { fontSize: 11, fill: '#64748b' }
const BRAND = '#059669' // emerald-600: single-series charts and income
const SPEND = '#475569' // slate-600: spend (emphasis form: brand hue vs gray)
const H = 300

interface SegProps {
  x?: number
  y?: number
  width?: number
  height?: number
  fill?: string
  payload?: Record<string, number | string>
}

/** One stacked segment: a 2px surface gap separates neighbours, only the top segment gets the 4px rounded end. */
function Segment({ x = 0, y = 0, width = 0, height = 0, fill, payload, stackKey }: SegProps & { stackKey: string }) {
  if (height <= 0 || width <= 0) return null
  const idx = STACK_KEYS.indexOf(stackKey as (typeof STACK_KEYS)[number])
  const isTop = STACK_KEYS.slice(idx + 1).every((k) => !(payload?.[k] as number))
  const r = isTop ? Math.min(4, height / 2, width / 2) : 0
  const d = `M${x},${y + height} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + width - r},${y} Q${x + width},${y} ${x + width},${y + r} L${x + width},${y + height} Z`
  return <path d={d} fill={fill} stroke={SURFACE} strokeWidth={2} />
}

interface TipProps {
  active?: boolean
  label?: string | number
  payload?: ReadonlyArray<{ dataKey?: string | number; value?: number | string; color?: string; payload?: Record<string, number | string> }>
}

const labelOf = (key: string) => (key === OTHER_KEY ? 'other (all remaining + uncategorized)' : categoryLabel(key))
const colorOf = (key: string) => (key === OTHER_KEY ? OTHER_COLOR : categoryColor(key))

function StackTip({ active, label, payload }: TipProps) {
  if (!active || !payload?.length) return null
  const rows = [...payload].reverse().filter((p) => Number(p.value) > 0)
  const total = rows.reduce((a, p) => a + Number(p.value), 0)
  return (
    <TipBox
      title={monthLabel(String(label))}
      rows={[
        ...rows.map((p) => ({ label: labelOf(String(p.dataKey)), value: money(Number(p.value)), color: colorOf(String(p.dataKey)) })),
        { label: 'total spend', value: money(total) },
      ]}
    />
  )
}

function IncomeSpendTip({ active, label, payload }: TipProps) {
  if (!active || !payload?.length) return null
  return (
    <TipBox
      title={monthLabel(String(label))}
      rows={payload.map((p) => ({ label: String(p.dataKey), value: money(Number(p.value)), color: p.color }))}
    />
  )
}

function BalanceTip({ active, label, payload }: TipProps) {
  if (!active || !payload?.length) return null
  return <TipBox title={String(label)} rows={[{ label: 'end-of-day balance', value: money(Number(payload[0].value)), color: BRAND }]} />
}

const tipOf = (Tip: (p: TipProps) => React.ReactNode) => (p: unknown) => Tip(p as TipProps)

export function DashboardPage() {
  const { selected, usersLoading, data, error, loading, reload } = useProfileTransactions()
  const txns = data?.items

  const view = useMemo(() => {
    if (!txns?.length || !selected) return null
    const series = balanceSeries(txns, selected.opening_balance_cents)
    const monthEnd = new Map<string, number>()
    const firstOfMonth = new Map<string, string>()
    for (const p of series) {
      monthEnd.set(p.date.slice(0, 7), p.balance)
      if (!firstOfMonth.has(p.date.slice(0, 7))) firstOfMonth.set(p.date.slice(0, 7), p.date)
    }
    return {
      stacked: spendByCategory(txns).map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, typeof v === 'number' ? Math.max(0, v) : v]))),
      table: spendTable(txns),
      ivs: incomeVsSpend(txns),
      merchants: topMerchants(txns),
      series,
      monthEnd: [...monthEnd.entries()],
      ticks: [...firstOfMonth.values()],
      uncategorizedSpendRows: txns.filter((t) => t.category == null && spendOf(t) > 0).length,
      first: txns.reduce((a, t) => (t.date < a ? t.date : a), txns[0].date),
      last: txns.reduce((a, t) => (t.date > a ? t.date : a), txns[0].date),
    }
  }, [txns, selected])

  const stackLegend = [...CHART_CATEGORIES, OTHER_KEY].map((k) => ({ label: k === OTHER_KEY ? 'other' : categoryLabel(k), color: colorOf(k) }))

  return (
    <div>
      <PageTitle
        title="Dashboard"
        subtitle={
          view && selected
            ? `${selected.name} · ${monthLabel(view.first.slice(0, 7))} to ${monthLabel(view.last.slice(0, 7))} · ${txns!.length.toLocaleString()} transactions · opening balance ${money(selected.opening_balance_cents)}`
            : undefined
        }
      />

      {error && <ErrorBox error={error} onRetry={reload} />}
      {!error && !selected && !usersLoading && (
        <Empty title="No profile yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload a CSV</Link> to see your spending.
        </Empty>
      )}
      {!error && (usersLoading || (selected && !data && loading)) && <Loading label="Loading transactions…" />}
      {!error && selected && !loading && (data === null || data?.items.length === 0) && (
        <Empty title="No transactions yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload a CSV</Link> for this profile.
        </Empty>
      )}

      {view && selected && !error && (
        <div className={`grid gap-6 lg:grid-cols-2 ${loading ? 'opacity-60' : ''}`}>
          <ChartCard
            className="lg:col-span-2"
            title="Monthly spend by category"
            subtitle="Seven categories keep their own color; everything else, including uncategorized rows, is grouped as other. The table lists all of them."
            legend={<LegendRow items={stackLegend} />}
            chart={
              <ResponsiveContainer width="100%" height={H}>
                <BarChart data={view.stacked} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke={GRID} />
                  <XAxis dataKey="month" tickFormatter={(m) => monthLabel(m, true)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
                  <YAxis tickFormatter={(v) => moneyCompact(v)} tickLine={false} axisLine={false} tick={AXIS_TEXT} width={52} />
                  <Tooltip content={tipOf(StackTip)} cursor={{ fill: '#f1f5f9' }} />
                  {STACK_KEYS.map((k) => (
                    <Bar
                      key={k}
                      dataKey={k}
                      stackId="spend"
                      fill={colorOf(k)}
                      maxBarSize={24}
                      isAnimationActive={false}
                      shape={(p: unknown) => <Segment {...(p as SegProps)} stackKey={k} />}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            }
            table={{
              columns: ['Category', ...view.table.months.map((m) => monthLabel(m, true)), 'Total'],
              rows: view.table.rows.map((r) => [r.category === 'uncategorized' ? 'uncategorized' : categoryLabel(r.category), ...r.values.map((v) => (v ? moneyWhole(v) : '–')), moneyWhole(r.total)]),
            }}
          />
          {view.uncategorizedSpendRows > 0 && (
            <p className="-mt-3 text-xs text-rose-700 lg:col-span-2">
              {view.uncategorizedSpendRows} uncategorized spending rows are counted in "other".
            </p>
          )}

          <ChartCard
            title="Income vs spend"
            subtitle="Per month, in dollars."
            legend={<LegendRow items={[{ label: 'income', color: BRAND }, { label: 'spend', color: SPEND }]} />}
            chart={
              <ResponsiveContainer width="100%" height={H}>
                <BarChart data={view.ivs} barGap={2} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke={GRID} />
                  <XAxis dataKey="month" interval={0} tickFormatter={(m: string) => monthLabel(m).slice(0, 3)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
                  <YAxis tickFormatter={(v) => moneyCompact(v)} tickLine={false} axisLine={false} tick={AXIS_TEXT} width={52} />
                  <Tooltip content={tipOf(IncomeSpendTip)} cursor={{ fill: '#f1f5f9' }} />
                  <Bar dataKey="income" fill={BRAND} radius={[4, 4, 0, 0]} maxBarSize={12} isAnimationActive={false} />
                  <Bar dataKey="spend" fill={SPEND} radius={[4, 4, 0, 0]} maxBarSize={12} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            }
            table={{
              columns: ['Month', 'Income', 'Spend', 'Net'],
              rows: view.ivs.map((r) => [monthLabel(r.month), money(r.income), money(r.spend), money(r.income - r.spend)]),
            }}
          />

          <ChartCard
            title="Top merchants by spend"
            subtitle="Grouped by cleaned bank descriptor, so one merchant can appear under more than one name."
            chart={
              <ResponsiveContainer width="100%" height={H}>
                <BarChart data={view.merchants} layout="vertical" margin={{ top: 8, right: 64, bottom: 0, left: 0 }}>
                  <XAxis type="number" hide />
                  <YAxis
                    type="category"
                    dataKey="merchant"
                    width={176}
                    tickLine={false}
                    axisLine={false}
                    tick={AXIS_TEXT}
                    tickFormatter={(v: string) => (v.length > 26 ? `${v.slice(0, 25)}…` : v)}
                  />
                  <Bar dataKey="spend" fill={BRAND} radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive={false}>
                    <LabelList dataKey="spend" position="right" formatter={(v: unknown) => moneyWhole(Number(v))} style={{ fontSize: 11, fill: '#334155' }} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            }
            table={{
              columns: ['Merchant (cleaned descriptor)', 'Spend', 'Transactions'],
              rows: view.merchants.map((m) => [m.merchant, money(m.spend), m.count]),
            }}
          />

          <ChartCard
            className="lg:col-span-2"
            title="Balance over time"
            subtitle="End-of-day balance: opening balance plus every transaction up to that day (transfers included)."
            chart={
              <ResponsiveContainer width="100%" height={H}>
                <LineChart data={view.series} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke={GRID} />
                  <XAxis dataKey="date" ticks={view.ticks} tickFormatter={(d: string) => monthLabel(d.slice(0, 7), true)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
                  <YAxis tickFormatter={(v) => moneyCompact(v)} tickLine={false} axisLine={false} tick={AXIS_TEXT} width={52} />
                  {view.series.some((p) => p.balance < 0) && <ReferenceLine y={0} stroke="#94a3b8" />}
                  <Tooltip content={tipOf(BalanceTip)} cursor={{ stroke: '#cbd5e1', strokeWidth: 1 }} />
                  <Line
                    type="monotone"
                    dataKey="balance"
                    stroke={BRAND}
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    dot={false}
                    activeDot={{ r: 4, fill: BRAND, stroke: SURFACE, strokeWidth: 2 }}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            }
            table={{
              columns: ['Month', 'End-of-month balance'],
              rows: view.monthEnd.map(([m, b]) => [monthLabel(m), money(b)]),
            }}
          />

          <p className="text-xs text-slate-500 lg:col-span-2">
            Income = inflows categorized income. Spend = outflows in every other category except transfers (moving money between
            your own accounts), net of refunds; uncategorized outflows count as spend. Computed in the browser from the transactions
            above.
          </p>
        </div>
      )}
    </div>
  )
}
