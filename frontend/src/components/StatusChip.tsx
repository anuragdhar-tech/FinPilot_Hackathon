const STYLES: Record<string, string> = {
  ok: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  done: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  rejected: 'bg-amber-50 text-amber-800 ring-amber-200',
  accepted: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  dropped: 'bg-rose-50 text-rose-700 ring-rose-200',
  error: 'bg-rose-50 text-rose-700 ring-rose-200',
  failed: 'bg-rose-50 text-rose-700 ring-rose-200',
  running: 'bg-sky-50 text-sky-700 ring-sky-200',
  pending: 'bg-slate-100 text-slate-600 ring-slate-200',
}

/** Works for run states (pending/running/done/failed), event statuses (ok/rejected/error) and draft statuses (accepted/rejected/dropped). */
export function StatusChip({ status }: { status: string }) {
  const live = status === 'running' || status === 'pending'
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        STYLES[status] ?? STYLES.pending
      }`}
    >
      {live && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {status}
    </span>
  )
}
