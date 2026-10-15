import { useCallback } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, ApiError, type ForecastPoint } from '../api'
import { useAsync } from '../hooks/useAsync'
import { money, moneyCompact, monthLabel } from '../lib/format'
import { ChartCard, LegendRow, TipBox } from './ChartCard'
import { Empty, ErrorBox, Loading } from './State'

const GRID = '#e2e8f0'
const AXIS_TEXT = { fontSize: 11, fill: '#64748b' }
const BRAND = '#059669'
const BAND = '#94a3b8'
const ONE_OFF_LABEL = 'one-off allowance'

const oneOff = (p: ForecastPoint) => p.by_category.one_off ?? 0

interface TipProps {
  active?: boolean
  label?: string | number
  payload?: ReadonlyArray<{ payload?: ForecastPoint }>
}

function ForecastTip({ active, label, payload }: TipProps) {
  const p = payload?.[0]?.payload
  if (!active || !p) return null
  return (
    <TipBox
      title={monthLabel(String(label))}
      rows={[
        { label: 'balance, middle estimate', value: money(p.balance_p50), color: BRAND },
        { label: 'balance, 80% range low', value: money(p.balance_p10), color: BAND },
        { label: 'balance, 80% range high', value: money(p.balance_p90), color: BAND },
        { label: 'expected income', value: money(p.income_cents) },
        { label: 'expected spend', value: money(p.spend_cents) },
        { label: `of which ${ONE_OFF_LABEL}`, value: money(oneOff(p)) },
      ]}
    />
  )
}

export function ForecastCard({ userId, className = '' }: { userId?: number; className?: string }) {
  const load = useCallback(async () => {
    try {
      return await api.getForecast(userId)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null // no run yet
      throw e
    }
  }, [userId])
  const { data, error, loading, reload } = useAsync(load)

  if (error) return <ErrorBox error={error} onRetry={reload} />
  if (!data && loading) return <Loading label="Loading forecast…" />
  if (!data) {
    return (
      <Empty title="No forecast yet">
        The forecast is produced by an analysis run on this profile's uploaded transactions.
      </Empty>
    )
  }

  return (
    <ChartCard
      className={className}
      title="Balance forecast"
      subtitle={
        <>
          Next {data.horizon_months} months after {monthLabel(data.as_of_month)}, starting from {money(data.start_balance_cents)}. The
          projected spend includes an allowance for one-off spending (the average of flagged one-off purchases over the last 6 months),
          shown separately in the table and tooltip. The dashed lines are the 80% range (p10 to p90).
        </>
      }
      legend={
        <LegendRow
          items={[
            { label: 'balance, middle estimate', color: BRAND },
            { label: '80% range (low / high)', color: BAND },
          ]}
        />
      }
      chart={
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={data.points} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke={GRID} />
            <XAxis dataKey="month" tickFormatter={(m: string) => monthLabel(m, true)} tickLine={false} axisLine={{ stroke: GRID }} tick={AXIS_TEXT} />
            <YAxis tickFormatter={(v) => moneyCompact(v)} tickLine={false} axisLine={false} tick={AXIS_TEXT} width={52} />
            <Tooltip content={(p) => ForecastTip(p as TipProps)} cursor={{ stroke: '#cbd5e1', strokeWidth: 1 }} />
            <Line type="monotone" dataKey="balance_p90" stroke={BAND} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="balance_p10" stroke={BAND} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="balance_p50" stroke={BRAND} strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      }
      table={{
        columns: ['Month', 'Income', 'Spend', `of which ${ONE_OFF_LABEL}`, 'Balance low (p10)', 'Balance middle (p50)', 'Balance high (p90)'],
        rows: data.points.map((p) => [
          monthLabel(p.month),
          money(p.income_cents),
          money(p.spend_cents),
          money(oneOff(p)),
          money(p.balance_p10),
          money(p.balance_p50),
          money(p.balance_p90),
        ]),
      }}
    />
  )
}
