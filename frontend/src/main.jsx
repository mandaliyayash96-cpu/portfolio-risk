import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { ThemeProvider } from './ThemeProvider.jsx'

// ThemeProvider wraps everything because the toggle in <Header> and the chart
// palettes deep inside the panels are the same piece of state. index.html has
// already set data-theme by this point; the provider takes ownership of it.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
)
