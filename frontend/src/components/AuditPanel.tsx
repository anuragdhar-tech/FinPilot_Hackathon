import { useCallback, useMemo, useState } from 'react'
import { api, ApiError, type AuditAttempt, type AuditRecord, type Transaction } from '../api'
import { useAsync } from '../hooks/useAsync'
import { claimQualifier, claimValue, KIND_LABEL, RULE_LABEL } from '../lib/advice'
import { dateTime, money, monthLabel } from '../lib/format'
import { ActionChip } from './ActionChip'
import { StatusChip } from './StatusChip'
import { ErrorBox, Loading } from './State'

const EVIDENCE_PREVIEW = 6

const failedCount = (a: AuditAttempt) => a.checks.filter((c) => !c.ok).length + a.rule_checks.filter((r) => !r.ok).length

const BORDER: Record<string, string> = {
  accepted: 'border-emerald-300',
  rejected: 'border-amber-300',
  dropped: 'border-rose-300',
}

const Mark = ({ ok }: { ok: boolean }) => (
  <span className={`inline-flex items-center gap-1 font-medium ${ok ? 'text-emerald-700' : 'text-rose-700'}`}>
    <span aria-hidden>{ok ? '✓' : '✗'}</span>
    {ok ? 'pass' : 'fail'}
  </span>
)

/** Two-step (or n-step) strip: each draft in order, so "rejected, then accepted" reads at a glance. */
function Journey({ attempts }: { attempts: AuditAttempt[] }) {
  const first = attempts[0]
  const last = attempts[attempts.length - 1]
  const headline =
    attempts.length === 1
      ? last.status === 'accepted'
        ? 'Passed the Verifier on the first draft.'
        : `Only one draft, and it was ${last.status}.`
      : last.status === 'accepted'
        ? `The first draft was rejected; the Advisor redrafted it${attempts.length > 2 ? ` ${attempts.length - 1} times` : ''} and draft ${last.attempt} passed.`
        : `Rejected ${attempts.length - 1} time(s); draft ${last.attempt} was ${last.status}.`
  return (
    <div>
      <p className="text-sm font-medium text-slate-800">{headline}</p>
      <ol className="mt-3 flex flex-wrap items-stretch gap-y-2">
        {attempts.map((a, i) => (
          <li key={a.rec_id} className="flex items-center">
            <div className={`rounded-lg border bg-white px-3 py-2 ${BORDER[a.status] ?? 'border-slate-200'}`}>
              <div className="flex items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-800 text-xs font-semibold text-white">{a.attempt}</span>
                <StatusChip status={a.status} />
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {failedCount(a) === 0 ? 'all checks passed' : `${failedCount(a)} check${failedCount(a) === 1 ? '' : 's'} failed`}
              </div>
            </div>
            {i < attempts.length - 1 && (
              <span className="mx-2 flex items-center text-slate-400" aria-label="then redrafted">
                <span className="h-px w-5 bg-slate-300" />
                <span aria-hidden>→</span>
              </span>
            )}
          </li>
        ))}
      </ol>
      {first.status === 'rejected' && attempts.length > 1 && (
        <p className="mt-2 text-xs text-slate-500">The Advisor received the failed checks below as structured feedback before drafting again.</p>
      )}
    </div>
  )
}

function Evidence({ audit, txns, txnError }: { audit: AuditRecord; txns: Transaction[] | null; txnError: Error | null }) {
  const [all, setAll] = useState(false)
  const trigger = audit.trigger
  const ids = trigger?.evidence_txn_ids.length ?? 0
  const rows = useMemo(() => {
    const byId = new Map((txns ?? []).map((t) => [t.id, t]))
    return (trigger?.evidence_txn_ids ?? [])
      .map((id) => byId.get(id))
      .filter((t): t is Transaction => t != null)
      .sort((a, b) => b.date.localeCompare(a.date))
  }, [txns, trigger])
  const shown = all ? rows : rows.slice(0, EVIDENCE_PREVIEW)
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Triggered by</h4>
      {audit.trigger ? (
        <p className="mt-1 text-sm text-slate-800">{audit.trigger.title}</p>
      ) : (
        <p className="mt-1 text-sm text-slate-500">
          {audit.trigger_type} {audit.trigger_id} (no pattern record found for it)
        </p>
      )}
      {ids > 0 && (
        <div className="mt-3">
          <p className="text-xs text-slate-500">
            Evidence: {ids} transaction{ids === 1 ? '' : 's'} behind this pattern
          </p>
          {txnError && <p className="mt-1 text-xs text-amber-700">Could not load the transaction details: {txnError.message}</p>}
          {!txnError && txns && rows.length < ids && (
            <p className="mt-1 text-xs text-amber-700">{ids - rows.length} evidence transaction(s) are not in the selected profile's list.</p>
          )}
          {shown.length > 0 && (
            <div className="mt-2 overflow-x-auto rounded-md border border-slate-200 bg-white">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-3 py-1.5 font-medium">Date</th>
                    <th className="px-3 py-1.5 font-medium">Description</th>
                    <th className="px-3 py-1.5 text-right font-medium">Amount</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {shown.map((t) => (
                    <tr key={t.id}>
                      <td className="whitespace-nowrap px-3 py-1.5 tabular-nums">{t.date}</td>
                      <td className="px-3 py-1.5">{t.description}</td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-right tabular-nums">{money(t.amount_cents)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {rows.length > EVIDENCE_PREVIEW && (
            <button onClick={() => setAll(!all)} className="mt-2 text-xs font-medium text-emerald-700 hover:underline">
              {all ? 'Show fewer' : `Show all ${rows.length}`}
            </button>
          )}
        </div>
      )}
    </section>
  )
}

function ClaimsTable({ attempt }: { attempt: AuditAttempt }) {
  if (attempt.checks.length === 0) {
    return <p className="text-sm text-slate-500">No claim was well-formed enough to recompute.</p>
  }
  return (
    <div className="overflow-x-auto rounded-md border border-slate-200 bg-white">
      <table className="w-full text-left text-xs">
        <thead className="bg-slate-50 text-slate-500">
          <tr>
            <th className="px-3 py-1.5 font-medium">Claim</th>
            <th className="px-3 py-1.5 text-right font-medium">Advisor claimed</th>
            <th className="px-3 py-1.5 text-right font-medium">Verifier recomputed</th>
            <th className="px-3 py-1.5 font-medium">Result</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {attempt.checks.map((c, i) => {
            const q = claimQualifier(c.claim)
            return (
              <tr key={i} className={c.ok ? '' : 'bg-rose-50/60'}>
                <td className="px-3 py-2">
                  <div className="font-medium text-slate-800">{KIND_LABEL[c.claim.kind] ?? c.claim.kind}</div>
                  <div className="text-slate-500">
                    {q && <>{q} · </>}
                    <span className="font-mono">{c.claim.source_ref}</span>
                  </div>
                </td>
                <td className={`px-3 py-2 text-right tabular-nums ${c.ok ? '' : 'font-semibold text-rose-700'}`}>{claimValue(c.claim.unit, c.claim.value)}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {claimValue(c.claim.unit, c.recomputed)}
                  {c.tolerance > 0 && <div className="text-slate-400">±{claimValue(c.claim.unit, c.tolerance)}</div>}
                </td>
                <td className="px-3 py-2">
                  <Mark ok={c.ok} />
                  {!c.ok && c.note && <div className="mt-0.5 max-w-xs break-words text-rose-700">{c.note}</div>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Attempt({ a, isShown }: { a: AuditAttempt; isShown: boolean }) {
  return (
    <li className={`rounded-lg border-l-4 border bg-white p-4 ${BORDER[a.status] ?? 'border-slate-200'}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">Draft {a.attempt}</span>
        <StatusChip status={a.status} />
        <ActionChip action={a.action} />
        <span className="text-sm text-slate-600">{a.target}</span>
        {isShown && <span className="rounded-full bg-emerald-600 px-2 py-0.5 text-xs font-medium text-white">shown on the card</span>}
        <span className="ml-auto text-xs text-slate-400">{dateTime(a.ts)}</span>
      </div>

      <div className="mt-3 rounded-md bg-slate-50 p-3">
        <div className="text-sm font-medium">{a.title}</div>
        <p className="mt-1 whitespace-pre-line text-sm text-slate-700">{a.body}</p>
      </div>

      {a.reasons.length > 0 && (
        <div role="note" className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <div className="font-medium">Why the Verifier {a.status === 'dropped' ? 'dropped' : 'rejected'} it</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {a.reasons.map((r, i) => (
              <li key={i} className="break-words">{r}</li>
            ))}
          </ul>
        </div>
      )}

      <h4 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-slate-500">Claims, recomputed from the data</h4>
      <ClaimsTable attempt={a} />

      <h4 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-slate-500">Rule checks</h4>
      <ul className="space-y-1 text-sm">
        {a.rule_checks.map((r) => (
          <li key={r.rule} className="flex flex-wrap items-baseline gap-x-2">
            <Mark ok={r.ok} />
            <span className="text-slate-800">{RULE_LABEL[r.rule] ?? r.rule}</span>
            {!r.ok && r.note && <span className="break-words text-rose-700">{r.note}</span>}
          </li>
        ))}
      </ul>
    </li>
  )
}

/** "Why this was recommended": trigger + evidence, every draft in order, claims vs recomputed values, rule checks. */
export function AuditPanel({ recId, userId }: { recId: number; userId?: number }) {
  const loadAudit = useCallback(() => api.getAudit(recId), [recId])
  const { data: audit, error, loading, reload } = useAsync(loadAudit)
  const loadTxns = useCallback(async () => {
    try {
      return (await api.getTransactions(userId)).items
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return []
      throw e
    }
  }, [userId])
  const { data: txns, error: txnError } = useAsync(loadTxns)

  if (error) return <ErrorBox error={error} onRetry={reload} />
  if (!audit) return loading ? <Loading label="Loading the audit trail…" /> : null

  return (
    <div className="space-y-6">
      <Journey attempts={audit.attempts} />
      <Evidence audit={audit} txns={txns} txnError={txnError} />
      <section>
        <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Every draft, in order</h4>
        <ol className="space-y-4">
          {audit.attempts.map((a) => (
            <Attempt key={a.rec_id} a={a} isShown={a.rec_id === audit.recommendation_id} />
          ))}
        </ol>
      </section>
      <p className="text-xs text-slate-400">
        Run <span className="font-mono">{audit.run_id.slice(0, 8)}</span> · numbers as of {monthLabel(audit.inputs.as_of_month)}
        {audit.inputs.forecast_id != null && <> · forecast #{audit.inputs.forecast_id}</>} · {audit.model || 'model n/a'} · prompt {audit.prompt_version}
      </p>
    </div>
  )
}

