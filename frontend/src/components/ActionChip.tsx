import type { Action } from '../api'
import { ACTION_LABEL } from '../lib/advice'

const STYLE: Record<Action, string> = {
  cancel: 'bg-rose-50 text-rose-700 ring-rose-200',
  review_price_increase: 'bg-amber-50 text-amber-800 ring-amber-200',
  reduce_spending: 'bg-sky-50 text-sky-700 ring-sky-200',
}

export function ActionChip({ action }: { action: string }) {
  const known = action in ACTION_LABEL
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${
        known ? STYLE[action as Action] : 'bg-slate-100 text-slate-600 ring-slate-200'
      }`}
    >
      {known ? ACTION_LABEL[action as Action] : action}
    </span>
  )
}
