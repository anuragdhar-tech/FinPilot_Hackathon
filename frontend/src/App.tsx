import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { Placeholder } from './components/Placeholder'
import { UserProvider } from './context/UserProvider'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { UploadPage } from './pages/UploadPage'

export default function App() {
  return (
    <BrowserRouter>
      <UserProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Placeholder title="Dashboard" />} />
            <Route path="transactions" element={<Placeholder title="Transactions" />} />
            <Route path="upload" element={<UploadPage />} />
            <Route path="eval" element={<Placeholder title="Eval" />} />
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
