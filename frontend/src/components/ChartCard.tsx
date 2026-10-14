import { useState, type ReactNode } from 'react'

export interface TableView {
  columns: string[]
  rows: (string | number)[][]
}

/** A chart with a Chart / Table toggle: the table is the accessible twin that carries every value. */
export function ChartCard({
  title,
  subtitle,
  legend,
  chart,
  table,
  className = '',
}: {
  title: string
  subtitle?: ReactNode
  legend?: ReactNode
  chart: ReactNode
  table: TableView
  className?: string
}) {
  const [view, setView] = useState<'chart' | 'table'>('chart')
  const tab = (v: 'chart' | 'table', label: string) => (
    <button
      onClick={() => setView(v)}
      aria-pressed={view === v}
      className={`px-2.5 py-1 text-xs font-medium ${view === v ? 'bg-emerald-50 text-emerald-700' : 'text-slate-500 hover:bg-slate-50'}`}
    >
      {label}
    </button>
  )
  return (
    <section className={`rounded-lg border border-slate-200 bg-white p-5 ${className}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
        </div>
        <div className="flex shrink-0 divide-x divide-slate-200 overflow-hidden rounded-md border border-slate-200">
          {tab('chart', 'Chart')}
          {tab('table', 'Table')}
        </div>
      </div>
      {view === 'chart' ? (
        <>
          {legend && <div className="mt-3">{legend}</div>}
          <div className="mt-3">{chart}</div>
        </>
      ) : (
        <div className="mt-3 max-h-80 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-white text-slate-500">
              <tr>
                {table.columns.map((c, i) => (
                  <th key={c} className={`whitespace-nowrap border-b border-slate-200 px-2 py-1.5 font-medium ${i ? 'text-right' : ''}`}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((r, ri) => (
                <tr key={ri} className="border-b border-slate-100">
                  {r.map((cell, i) => (
                    <td key={i} className={`whitespace-nowrap px-2 py-1.5 ${i ? 'text-right tabular-nums' : 'font-medium'}`}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function LegendRow({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: i.color }} />
          {i.label}
        </li>
      ))}
    </ul>
  )
}

/** Tooltip body: value leads (strong), label follows (secondary), line keys not boxes. */
export function TipBox({ title, rows }: { title: string; rows: { label: string; value: string; color?: string }[] }) {
  return (
    <div className="min-w-40 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-medium text-slate-900">{title}</div>
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-2 py-0.5">
          {r.color && <span className="h-0.5 w-3 rounded" style={{ backgroundColor: r.color }} />}
          <span className="font-semibold tabular-nums text-slate-900">{r.value}</span>
          <span className="text-slate-500">{r.label}</span>
        </div>
      ))}
    </div>
  )
}
