import type { Category } from '../api'

// Seven categories get a fixed hue from the validated categorical palette (in palette order);
// the other eight share a neutral gray ("Other") so the stacked chart never needs more than 8 colors.
// Color follows the category, never its rank, so filtering never repaints a category.
export const CHART_CATEGORIES = [
  'housing', 'groceries', 'dining', 'food_delivery', 'transport', 'shopping', 'utilities',
] as const satisfies readonly Category[]

const HUES: Record<string, string> = {
  housing: '#2a78d6',
  groceries: '#eb6834',
  dining: '#1baf7a',
  food_delivery: '#eda100',
  transport: '#e87ba4',
  shopping: '#008300',
  utilities: '#6250d6',
}
export const OTHER_COLOR = '#94a3b8'

export const categoryColor = (c: string) => HUES[c] ?? OTHER_COLOR
export const categoryLabel = (c: string) => c.replace('_', ' ')
