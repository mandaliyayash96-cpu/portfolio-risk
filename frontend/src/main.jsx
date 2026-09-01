import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { AuthProvider } from './auth/AuthContext.jsx'
import { ThemeProvider } from './ThemeProvider.jsx'

// ThemeProvider wraps everything because the toggle in <Header> and the chart
// palettes deep inside the panels are the same piece of state. index.html has
// already set data-theme by this point; the provider takes ownership of it.
//
// AuthProvider sits INSIDE it, and the order matters: the login screen and the
// "signing you in" splash both render before anyone is authenticated, and both
// have to be themed. Auth inside theme means every screen in the app is themed;
// the other way round, the two screens a signed-out visitor actually sees would
// be the only unthemed ones.
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ThemeProvider>
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  </StrictMode>,
)
