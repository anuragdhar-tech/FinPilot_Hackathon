import { NavLink, Outlet } from 'react-router-dom'
import { useMocks } from '../api'
import { ProfileSwitcher } from './ProfileSwitcher'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/transactions', label: 'Transactions' },
  { to: '/upload', label: 'Upload' },
  { to: '/runs', label: 'Agent Runs' },
  { to: '/eval', label: 'Eval' },
  { to: '/simulator', label: 'Simulator' },
  { to: '/goals', label: 'Goals' },
  { to: '/recommendations', label: 'Recommendations' },
]

export function AppShell() {
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-slate-200 bg-white p-4">
        <div className="mb-6 text-lg font-semibold text-emerald-700">FinPilot</div>
        <nav className="flex flex-col gap-1">
          {NAV.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `rounded-md px-3 py-2 text-sm font-medium ${
                  isActive
                    ? 'bg-emerald-50 text-emerald-700'
                    : 'text-slate-600 hover:bg-slate-100'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-8">
          <span className={`text-xs font-medium ${useMocks ? 'rounded bg-amber-100 px-2 py-1 text-amber-800' : 'text-slate-400'}`}>
            {useMocks ? 'MOCK DATA (VITE_USE_MOCKS=true)' : 'Live backend'}
          </span>
          <ProfileSwitcher />
        </header>
        <main className="flex-1 p-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
