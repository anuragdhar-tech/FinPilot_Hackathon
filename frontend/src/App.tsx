import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { Placeholder } from './components/Placeholder'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Placeholder title="Dashboard" />} />
          <Route path="simulator" element={<Placeholder title="Simulator" />} />
          <Route path="goals" element={<Placeholder title="Goals" />} />
          <Route path="recommendations" element={<Placeholder title="Recommendations" />} />
          <Route path="runs" element={<Placeholder title="Agent Runs" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
