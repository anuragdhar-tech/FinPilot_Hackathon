import { useEffect } from 'react'
import type { Transaction } from '../api'
import { money } from '../lib/format'
import { CategoryChip } from './CategoryChip'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-slate-900">{children}</dd>
    </div>
  )
}

export function TransactionPanel({ txn, onClose }: { txn: Transaction; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-20 flex justify-end bg-slate-900/20" onClick={onClose}>
      <aside
        role="dialog"
        aria-label="Transaction details"
        onClick={(e) => e.stopPropagation()}
        className="h-full w-full max-w-md overflow-y-auto border-l border-slate-200 bg-white p-6 shadow-xl"
      >
        <div className="flex items-start justify-between gap-4">
          <h2 className="text-lg font-semibold">Transaction</h2>
          <button onClick={onClose} aria-label="Close" className="rounded-md px-2 py-1 text-slate-500 hover:bg-slate-100">
            ✕
          </button>
        </div>
        <p className="mt-3 break-words font-mono text-sm">{txn.description}</p>
        <p className={`mt-2 text-2xl font-semibold tabular-nums ${txn.amount_cents < 0 ? 'text-rose-600' : 'text-emerald-700'}`}>
          {money(txn.amount_cents)}
        </p>

        <dl className="mt-6 space-y-4">
          <Field label="Date">{txn.date}</Field>
          <Field label="External id">
            <span className="font-mono">{txn.external_id}</span>
          </Field>
          <Field label="Category">
            <CategoryChip category={txn.category} />
          </Field>
          {txn.category == null ? (
            <div role="note" className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
              Not categorized. Either no categorization run has covered this row yet, or the Categorizer could not
              produce a valid answer for it. Check Agent Runs for the run's events.
            </div>
          ) : (
            <>
              <Field label="Confidence">{txn.confidence?.toFixed(2) ?? '—'}</Field>
              <Field label="Source">{txn.from_cache ? 'cached (label reused from the merchant cache)' : 'LLM (looked at directly)'}</Field>
              <Field label="Model">{txn.model ?? '—'}</Field>
              <Field label="Prompt version">{txn.prompt_version ?? '—'}</Field>
              <Field label="Rationale">{txn.rationale || '—'}</Field>
            </>
          )}
        </dl>
      </aside>
    </div>
  )
}
