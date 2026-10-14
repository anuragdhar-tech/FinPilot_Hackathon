import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { Placeholder } from './components/Placeholder'
import { UserProvider } from './context/UserProvider'

export default function App() {
  return (
    <BrowserRouter>
      <UserProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Placeholder title="Dashboard" />} />
            <Route path="transactions" element={<Placeholder title="Transactions" />} />
            <Route path="upload" element={<Placeholder title="Upload" />} />
            <Route path="eval" element={<Placeholder title="Eval" />} />
            <Route path="simulator" element={<Placeholder title="Simulator" />} />
            <Route path="goals" element={<Placeholder title="Goals" />} />
            <Route path="recommendations" element={<Placeholder title="Recommendations" />} />
            <Route path="runs" element={<Placeholder title="Agent Runs" />} />
          </Route>
        </Routes>
      </UserProvider>
    </BrowserRouter>
  )
}
