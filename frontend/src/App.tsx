import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { UserProvider } from './context/UserProvider'
import { Loading } from './components/State'
import { EvalPage } from './pages/EvalPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RecommendationsPage } from './pages/RecommendationsPage'
import { RunsPage } from './pages/RunsPage'
import { TransactionsPage } from './pages/TransactionsPage'
import { UploadPage } from './pages/UploadPage'

// The dashboard, the simulator and goals pull in the charting library, so they load as their own chunks.
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })))
const SimulatorPage = lazy(() => import('./pages/SimulatorPage').then((m) => ({ default: m.SimulatorPage })))
const GoalsPage = lazy(() => import('./pages/GoalsPage').then((m) => ({ default: m.GoalsPage })))

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
            <Route
              path="simulator"
              element={
                <Suspense fallback={<Loading />}>
                  <SimulatorPage />
                </Suspense>
              }
            />
            <Route
              path="goals"
              element={
                <Suspense fallback={<Loading />}>
                  <GoalsPage />
                </Suspense>
              }
            />
            <Route path="recommendations" element={<RecommendationsPage />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="runs/:runId" element={<RunDetailPage />} />
          </Route>
        </Routes>
      </UserProvider>
    </BrowserRouter>
  )
}
