import { useEffect, useState } from 'react'
import { api, ApiError, type User } from '../api'
import { useUsers } from '../context/user-context'
import { errorMessage } from '../lib/format'
import { forgetRunsForUser } from '../lib/runsStore'

/** Delete button for a profile, with a confirm dialog that names it. Deleting cannot be undone. */
export function DeleteProfileButton({ user }: { user: User }) {
  const { refresh } = useUsers()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || busy) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy])

  async function confirmDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteUser(user.id)
    } catch (e) {
      // 404 means it is already gone, so carry on; anything else stays open with the error
      if (!(e instanceof ApiError && e.status === 404)) {
        setError(errorMessage(e))
        setBusy(false)
        return
      }
    }
    forgetRunsForUser(user.id)
    setOpen(false)
    setBusy(false)
    await refresh() // selects another profile if one exists, otherwise the pages show their empty state
  }

  return (
    <>
      <button
        onClick={() => {
          setError(null)
          setOpen(true)
        }}
        aria-label={`Delete profile ${user.name}`}
        className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-rose-700 hover:border-rose-300 hover:bg-rose-50"
      >
        Delete
      </button>

      {open && (
        <div className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/30 p-4" onClick={() => !busy && setOpen(false)}>
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-title"
            aria-describedby="delete-body"
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl"
          >
            <h2 id="delete-title" className="text-lg font-semibold">Delete this profile?</h2>
            <p id="delete-body" className="mt-2 text-sm text-slate-700">
              This permanently deletes <b className="font-semibold">{user.name}</b> with its {user.txn_count.toLocaleString()} transactions,
              categorizations and runs. This cannot be undone.
            </p>
            {error && <p role="alert" className="mt-3 text-sm text-rose-700">Could not delete: {error}</p>}
            <div className="mt-5 flex justify-end gap-2">
              <button
                autoFocus
                onClick={() => setOpen(false)}
                disabled={busy}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                disabled={busy}
                className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-60"
              >
                {busy ? 'Deleting…' : 'Delete profile'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
