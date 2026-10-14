import type { AgentEvent } from '../api'
import { dateTime, duration } from '../lib/format'
import { StatusChip } from './StatusChip'

const DOT: Record<string, string> = {
  ok: 'bg-emerald-500',
  rejected: 'bg-amber-500',
  error: 'bg-rose-500',
}

/** Generic: renders whatever events the run produced, in seq order (retries, rejections, any node). */
export function RunTimeline({ events, waiting }: { events: AgentEvent[]; waiting?: boolean }) {
  if (!events.length) {
    return <p className="py-4 text-sm text-slate-500">{waiting ? 'Waiting for the first event…' : 'No events recorded.'}</p>
  }
  const ordered = [...events].sort((a, b) => a.seq - b.seq)
  return (
    <ol className="relative ml-2 border-l border-slate-200">
      {ordered.map((e) => {
        const tokens =
          e.prompt_tokens != null || e.completion_tokens != null
            ? `${(e.prompt_tokens ?? 0).toLocaleString()} in / ${(e.completion_tokens ?? 0).toLocaleString()} out tokens`
            : null
        const meta = [e.model, tokens, duration(e.duration_ms), dateTime(e.ts)].filter(Boolean)
        return (
          <li key={e.seq} className="relative pb-5 pl-6 last:pb-0">
            <span className={`absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-slate-50 ${DOT[e.status] ?? 'bg-slate-400'}`} />
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs tabular-nums text-slate-400">#{e.seq}</span>
              <span className="font-mono text-sm font-medium">{e.node}</span>
              <StatusChip status={e.status} />
              {e.attempt > 1 && (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">attempt {e.attempt}</span>
              )}
            </div>
            {e.summary && <p className="mt-1 break-words text-sm text-slate-700">{e.summary}</p>}
            {meta.length > 0 && <p className="mt-1 text-xs text-slate-500">{meta.join(' · ')}</p>}
            {(e.input_ref || e.output_ref) && (
              <p className="mt-0.5 break-all font-mono text-xs text-slate-400">
                {e.input_ref && <>in: {e.input_ref}</>} {e.output_ref && <>out: {e.output_ref}</>}
              </p>
            )}
          </li>
        )
      })}
    </ol>
  )
}
