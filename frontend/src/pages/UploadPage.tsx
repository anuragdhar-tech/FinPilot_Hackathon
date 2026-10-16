import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError, type UploadResult } from '../api'
import { useRunPolling } from '../hooks/useRunPolling'
import { useUsers } from '../context/user-context'
import { errorMessage } from '../lib/format'
import { rememberRun } from '../lib/runsStore'
import { RunTimeline } from '../components/RunTimeline'
import { StatusChip } from '../components/StatusChip'
import { PageTitle } from '../components/State'

type Mode = 'new' | 'append'

const card = 'rounded-lg border border-slate-200 bg-white p-5'
const input =
  'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-600 focus:outline-none focus:ring-1 focus:ring-emerald-600 disabled:bg-slate-100'

export function UploadPage() {
  const { selected, refresh } = useUsers()
  const [file, setFile] = useState<File | null>(null)
  const [mode, setMode] = useState<Mode>('new')
  const [name, setName] = useState('')
  const [opening, setOpening] = useState('')
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<unknown>(null)
  const [result, setResult] = useState<UploadResult | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [runError, setRunError] = useState<unknown>(null)
  const { run, error: pollError } = useRunPolling(runId)

  const effectiveMode: Mode = mode === 'append' && selected ? 'append' : 'new'
  const openingNumber = opening.trim() === '' ? null : Number(opening)
  const openingInvalid = openingNumber != null && !Number.isFinite(openingNumber)

  async function onUpload(e: FormEvent) {
    e.preventDefault()
    if (!file || openingInvalid) return
    setUploading(true)
    setUploadError(null)
    setResult(null)
    setRunId(null)
    setRunError(null)
    try {
      const res = await api.uploadTransactions(
        effectiveMode === 'append' && selected
          ? { file, userId: selected.id }
          : {
              file,
              name: name.trim() || undefined,
              openingBalanceCents: openingNumber == null ? undefined : Math.round(openingNumber * 100),
            },
      )
      setResult(res)
      await refresh(res.user_id) // the uploaded profile becomes the selected one
    } catch (err) {
      setUploadError(err)
    } finally {
      setUploading(false)
    }
  }

  async function onRun() {
    if (!result) return
    setStarting(true)
    setRunError(null)
    try {
      const { run_id } = await api.createRun(result.batch_id)
      rememberRun({ run_id, batch_id: result.batch_id, user_id: result.user_id, started_at: new Date().toISOString() })
      setRunId(run_id)
    } catch (err) {
      setRunError(err)
    } finally {
      setStarting(false)
    }
  }

  const errorLines = (err: unknown) =>
    err instanceof ApiError && err.details.length > 1 ? err.details : [errorMessage(err)]
  const running = run?.status === 'pending' || run?.status === 'running'

  return (
    <div className="max-w-3xl">
      <PageTitle title="Upload" subtitle="Load a bank CSV (external_id, date, description, amount), then run the pipeline: categorize, analyze, forecast, advise and verify." />

      <form onSubmit={onUpload} className="space-y-4">
        <div className={card}>
          <label className="block text-sm font-medium" htmlFor="csv">1. CSV file</label>
          <input
            id="csv"
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="mt-2 block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-emerald-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-emerald-700 hover:file:bg-emerald-100"
          />
          {file && <p className="mt-2 text-xs text-slate-500">{file.name} · {(file.size / 1024).toFixed(1)} KB</p>}
        </div>

        <div className={card}>
          <div className="text-sm font-medium">2. Where should it go?</div>
          <div className="mt-3 space-y-4">
            <label className="flex items-start gap-3 text-sm">
              <input type="radio" name="mode" checked={effectiveMode === 'new'} onChange={() => setMode('new')} className="mt-1 accent-emerald-600" />
              <span className="flex-1">
                <span className="font-medium">New profile</span>
                <span className="mt-2 grid gap-3 sm:grid-cols-2">
                  <input
                    className={input}
                    placeholder="Name (defaults to the file name)"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    disabled={effectiveMode !== 'new'}
                    aria-label="Profile name"
                  />
                  <input
                    className={input}
                    placeholder="Opening balance in dollars (optional)"
                    inputMode="decimal"
                    value={opening}
                    onChange={(e) => setOpening(e.target.value)}
                    disabled={effectiveMode !== 'new'}
                    aria-label="Opening balance in dollars"
                  />
                </span>
                {openingInvalid && effectiveMode === 'new' && <span className="mt-1 block text-xs text-rose-700">Enter a number, e.g. 4200 or 4200.50</span>}
              </span>
            </label>
            <label className={`flex items-center gap-3 text-sm ${selected ? '' : 'text-slate-400'}`}>
              <input
                type="radio"
                name="mode"
                checked={effectiveMode === 'append'}
                disabled={!selected}
                onChange={() => setMode('append')}
                className="accent-emerald-600"
              />
              <span>
                <span className="font-medium">Add to {selected ? selected.name : 'selected profile'}</span>
                <span className="ml-2 text-xs text-slate-500">
                  {selected ? 'rows with an external_id already stored are skipped' : 'no profile exists yet'}
                </span>
              </span>
            </label>
          </div>
        </div>

        <button
          type="submit"
          disabled={!file || uploading || (effectiveMode === 'new' && openingInvalid)}
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {uploading ? 'Uploading…' : 'Upload'}
        </button>
      </form>

      {uploadError != null && (
        <div role="alert" className="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <div className="font-medium">Upload rejected, nothing was stored</div>
          <ul className="mt-1 list-disc pl-5">
            {errorLines(uploadError).slice(0, 10).map((line, i) => (
              <li key={i} className="break-words">{line}</li>
            ))}
          </ul>
          {errorLines(uploadError).length > 10 && <p className="mt-1">…and {errorLines(uploadError).length - 10} more</p>}
        </div>
      )}

      {result && (
        <div className={`${card} mt-6`}>
          <div className="text-sm font-medium">3. Uploaded</div>
          <p className="mt-2 text-sm">
            <b className="text-lg">{result.count.toLocaleString()}</b> new transactions stored ·{' '}
            <b className="text-lg">{result.duplicates_skipped.toLocaleString()}</b> duplicates skipped
          </p>
          <p className="mt-1 text-xs text-slate-500">batch {result.batch_id.slice(0, 8)}… · profile #{result.user_id}</p>

          {result.count === 0 ? (
            <p className="mt-4 text-sm text-slate-500">Nothing new in this file, so there is nothing to categorize.</p>
          ) : (
            <button
              onClick={onRun}
              disabled={starting || runId != null}
              className="mt-4 rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {starting ? 'Starting…' : runId ? 'Run started' : 'Run pipeline'}
            </button>
          )}
          {runError != null && <p role="alert" className="mt-3 text-sm text-rose-700">Could not start the run: {errorMessage(runError)}</p>}
        </div>
      )}

      {runId && (
        <div className={`${card} mt-6`}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm font-medium">Pipeline run</span>
            {run && <StatusChip status={run.status} />}
            <Link to={`/runs/${runId}`} className="text-xs text-emerald-700 hover:underline">open in Agent Runs</Link>
            {run?.status === 'done' && (
              <span className="ml-auto flex flex-wrap gap-2">
                <Link to="/recommendations" className="rounded-md bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-100">
                  View recommendations →
                </Link>
                <Link to="/transactions" className="rounded-md bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-100">
                  View transactions →
                </Link>
              </span>
            )}
          </div>
          {run?.error && <p role="alert" className="mt-3 text-sm text-rose-700">Run failed: {run.error}</p>}
          {pollError && <p className="mt-3 text-sm text-amber-700">Connection problem while polling: {pollError.message} (retrying)</p>}
          <div className="mt-4">
            <RunTimeline events={run?.events ?? []} waiting={running || !run} />
          </div>
        </div>
      )}
    </div>
  )
}
