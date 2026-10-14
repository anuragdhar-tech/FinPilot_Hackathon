import { useEffect, useState } from 'react'
import { api, useMocks } from '../api'

export function Placeholder({ title }: { title: string }) {
  const [health, setHealth] = useState<string>('checking…')

  useEffect(() => {
    api.health().then((h) => setHealth(h.status)).catch(() => setHealth('unreachable'))
  }, [])

  return (
    <div>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-slate-500">Not built yet.</p>
      <p className="mt-6 text-xs text-slate-400">
        API: {useMocks ? 'mocked' : 'real'} · health: {health}
      </p>
    </div>
  )
}
