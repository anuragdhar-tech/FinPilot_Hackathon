import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CATEGORIES, type Transaction } from '../api'
import { useProfileTransactions } from '../hooks/useProfileTransactions'
import { categoryLabel } from '../lib/categories'
import { money, monthLabel } from '../lib/format'
import { CategoryChip } from '../components/CategoryChip'
import { Empty, ErrorBox, Loading, PageTitle } from '../components/State'
import { TransactionPanel } from '../components/TransactionPanel'

const LOW_CONFIDENCE = 0.9
const UNCATEGORIZED = 'uncategorized'

const control =
  'rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm focus:border-emerald-600 focus:outline-none focus:ring-1 focus:ring-emerald-600'

export function TransactionsPage() {
  const { selected, usersLoading, data, error, loading, reload } = useProfileTransactions()

  const [month, setMonth] = useState('')
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [lowOnly, setLowOnly] = useState(false)
  const [open, setOpen] = useState<Transaction | null>(null)

  const all = data?.items
  const months = useMemo(() => [...new Set((all ?? []).map((t) => t.date.slice(0, 7)))].sort().reverse(), [all])
  const stats = useMemo(() => {
    const items = all ?? []
    return {
      uncategorized: items.filter((t) => t.category == null).length,
      low: items.filter((t) => t.confidence != null && t.confidence < LOW_CONFIDENCE).length,
    }
  }, [all])

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (all ?? []).filter(
      (t) =>
        (!month || t.date.startsWith(month)) &&
        (!category || (category === UNCATEGORIZED ? t.category == null : t.category === category)) &&
        (!q || t.description.toLowerCase().includes(q)) &&
        (!lowOnly || (t.confidence != null && t.confidence < LOW_CONFIDENCE)),
    )
  }, [all, month, category, query, lowOnly])

  const filtered = month !== '' || category !== '' || query !== '' || lowOnly
  const clear = () => {
    setMonth('')
    setCategory('')
    setQuery('')
    setLowOnly(false)
  }

  return (
    <div>
      <PageTitle
        title="Transactions"
        subtitle={selected ? `${selected.name}: every row with the category, confidence and source the Categorizer gave it.` : undefined}
      />

      {error && <ErrorBox error={error} onRetry={reload} />}
      {!error && !selected && !usersLoading && (
        <Empty title="No profile yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload a CSV</Link> to create one.
        </Empty>
      )}
      {!error && (usersLoading || (selected && !data && loading)) && <Loading label="Loading transactions…" />}
      {!error && selected && data === null && !loading && (
        <Empty title="No transactions yet">
          <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload a CSV</Link> for this profile.
        </Empty>
      )}

      {data && !error && (
        <div className={loading ? 'opacity-60' : ''}>
          {data.items.length === 0 ? (
            <Empty title="No transactions for this profile">
              <Link to="/upload" className="font-medium text-emerald-700 hover:underline">Upload a CSV</Link>
            </Empty>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <select aria-label="Month" value={month} onChange={(e) => setMonth(e.target.value)} className={control}>
                  <option value="">All months</option>
                  {months.map((m) => (
                    <option key={m} value={m}>{monthLabel(m)}</option>
                  ))}
                </select>
                <select aria-label="Category" value={category} onChange={(e) => setCategory(e.target.value)} className={control}>
                  <option value="">All categories</option>
                  <option value={UNCATEGORIZED}>⚠ Uncategorized</option>
                  {CATEGORIES.map((c) => (
                    <option key={c} value={c}>{categoryLabel(c)}</option>
                  ))}
                </select>
                <input
                  type="search"
                  aria-label="Search descriptions"
                  placeholder="Search descriptions…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  className={`${control} w-64`}
                />
                <label className="flex items-center gap-1.5 text-xs text-slate-600">
                  <input type="checkbox" checked={lowOnly} onChange={(e) => setLowOnly(e.target.checked)} className="accent-emerald-600" />
                  Low confidence (&lt;{LOW_CONFIDENCE})
                </label>
                {filtered && (
                  <button onClick={clear} className="text-xs font-medium text-emerald-700 hover:underline">Clear filters</button>
                )}
                <span className="ml-auto text-xs text-slate-500">
                  {rows.length.toLocaleString()} of {data.total.toLocaleString()} ·{' '}
                  <span className={stats.uncategorized ? 'font-semibold text-rose-700' : ''}>{stats.uncategorized} uncategorized</span> ·{' '}
                  {stats.low} low confidence
                </span>
              </div>

              {rows.length === 0 ? (
                <Empty title="No transactions match these filters">
                  <button onClick={clear} className="font-medium text-emerald-700 hover:underline">Clear filters</button>
                </Empty>
              ) : (
                <div className="max-h-[72vh] overflow-auto rounded-lg border border-slate-200 bg-white">
                  <table className="w-full text-left text-sm">
                    <thead className="sticky top-0 z-10 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                      <tr>
                        <th className="px-4 py-2 font-medium">Date</th>
                        <th className="px-4 py-2 font-medium">Description</th>
                        <th className="px-4 py-2 text-right font-medium">Amount</th>
                        <th className="px-4 py-2 font-medium">Category</th>
                        <th className="px-4 py-2 font-medium">Confidence</th>
                        <th className="px-4 py-2 font-medium">Source</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {rows.map((t) => {
                        const low = t.confidence != null && t.confidence < LOW_CONFIDENCE
                        return (
                          <tr
                            key={t.id}
                            tabIndex={0}
                            onClick={() => setOpen(t)}
                            onKeyDown={(e) => e.key === 'Enter' && setOpen(t)}
                            className={`cursor-pointer hover:bg-slate-50 focus:bg-slate-50 focus:outline-none ${
                              t.category == null ? 'bg-rose-50 shadow-[inset_3px_0_0_0_#e11d48] hover:bg-rose-100' : ''
                            }`}
                          >
                            <td className="whitespace-nowrap px-4 py-2 tabular-nums text-slate-600">{t.date}</td>
                            <td className="max-w-md truncate px-4 py-2" title={t.description}>{t.description}</td>
                            <td className={`whitespace-nowrap px-4 py-2 text-right tabular-nums ${t.amount_cents < 0 ? 'text-rose-600' : 'text-emerald-700'}`}>
                              {money(t.amount_cents)}
                            </td>
                            <td className="px-4 py-2"><CategoryChip category={t.category} /></td>
                            <td className={`px-4 py-2 tabular-nums ${low ? 'font-semibold text-amber-700' : 'text-slate-600'}`}>
                              {t.confidence == null ? '—' : t.confidence.toFixed(2)}
                            </td>
                            <td className="px-4 py-2">
                              {t.from_cache && (
                                <span className="rounded-full bg-sky-50 px-2 py-0.5 text-xs font-medium text-sky-700 ring-1 ring-inset ring-sky-200">cached</span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {open && <TransactionPanel txn={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
