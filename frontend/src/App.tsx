import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { Placeholder } from './components/Placeholder'
import { UserProvider } from './context/UserProvider'
import { Loading } from './components/State'
import { EvalPage } from './pages/EvalPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { TransactionsPage } from './pages/TransactionsPage'
import { UploadPage } from './pages/UploadPage'

// The dashboard pulls in the charting library, so it loads as its own chunk.
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })))

export default function App() {
  return (
    <BrowserRouter>
      <UserProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route
              index
              element={
                <Suspense fallback={<Loading />}>
                  <DashboardPage />
                </Suspense>
              }
            />
            <Route path="transactions" element={<TransactionsPage />} />
            <Route path="upload" element={<UploadPage />} />
            <Route path="eval" element={<EvalPage />} />
            <Route path="simulator" element={<Placeholder title="Simulator" />} />
            <Route path="goals" element={<Placeholder title="Goals" />} />
            <Route path="recommendations" element={<Placeholder title="Recommendations" />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="runs/:runId" element={<RunDetailPage />} />
          </Route>
        </Routes>
      </UserProvider>
    </BrowserRouter>
  )
}
