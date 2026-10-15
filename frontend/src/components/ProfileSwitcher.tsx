import { Link } from 'react-router-dom'
import { monthLabel } from '../lib/format'
import { useUsers } from '../context/user-context'
import { DeleteProfileButton } from './DeleteProfileButton'

export function ProfileSwitcher() {
  const { users, loading, error, selected, select, refresh } = useUsers()

  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-rose-700" role="alert">
        <span>Profiles unavailable: {error.message}</span>
        <button onClick={() => refresh()} className="rounded border border-rose-300 px-2 py-0.5 text-xs hover:bg-rose-50">
          Retry
        </button>
      </div>
    )
  }
  if (!users.length) {
    return loading ? (
      <span className="text-sm text-slate-400">Loading profiles…</span>
    ) : (
      <span className="text-sm text-slate-500">
        No profiles yet.{' '}
        <Link to="/upload" className="font-medium text-emerald-700 hover:underline">
          Upload a CSV
        </Link>
      </span>
    )
  }
  return (
    <div className="flex items-center gap-3">
      <label className="flex items-center gap-3 text-sm">
        <span className="text-slate-500">Profile</span>
        <select
          value={selected?.id ?? ''}
          onChange={(e) => select(Number(e.target.value))}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 font-medium focus:border-emerald-600 focus:outline-none focus:ring-1 focus:ring-emerald-600"
        >
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.name} · {u.txn_count} txns
            </option>
          ))}
        </select>
        {selected && <span className="hidden text-slate-400 md:inline">as of {monthLabel(selected.as_of_month)}</span>}
      </label>
      {selected && <DeleteProfileButton user={selected} />}
    </div>
  )
}
