import type { ReactNode } from 'react'
import { errorMessage } from '../lib/format'

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-3 py-10 text-sm text-slate-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-emerald-600" />
      {label}
    </div>
  )
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
      <div className="font-medium">Something went wrong</div>
      <p className="mt-1 break-words">{errorMessage(error)}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 rounded-md border border-rose-300 bg-white px-3 py-1 text-xs font-medium hover:bg-rose-100"
        >
          Retry
        </button>
      )}
    </div>
  )
}

export const NOT_ENOUGH_HISTORY = 'Not enough history yet, need at least 3 months'

/** Shown instead of a projection when the profile has under 3 complete months of transactions. */
export function InsufficientData({ reason }: { reason: string | null }) {
  return (
    <div role="status" className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
      <div className="font-medium">{NOT_ENOUGH_HISTORY}</div>
      <p className="mt-1 text-amber-800">
        {reason ?? 'The forecast needs at least 3 complete months of transactions.'} Upload more history to get a forecast,
        what-if results and recommendations.
      </p>
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
      <div className="font-medium text-slate-700">{title}</div>
      {children && <div className="mt-1 text-sm text-slate-500">{children}</div>}
    </div>
  )
}

export function PageTitle({ title, subtitle }: { title: string; subtitle?: ReactNode }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-semibold">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
    </div>
  )
}
