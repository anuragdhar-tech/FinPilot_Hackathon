import { categoryColor, categoryLabel } from '../lib/categories'

/** A category with its color dot; null means the Categorizer left the row uncategorized. */
export function CategoryChip({ category }: { category: string | null }) {
  if (category == null) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-rose-300 bg-rose-100 px-2 py-0.5 text-xs font-semibold text-rose-800">
        <span aria-hidden>⚠</span> uncategorized
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: categoryColor(category) }} />
      {categoryLabel(category)}
    </span>
  )
}
