/**
 * The gate: who is looking, and what do they get to see.
 *
 * Four states, and each is a different screen:
 *
 *   loading          Firebase has not yet said whether this browser holds a
 *                    session. A splash, NOT the login screen - rendering the
 *                    phone form for the half-second before a persisted user is
 *                    restored is the flash that persistence exists to avoid.
 *   signed out       <LoginScreen>.
 *   signed in, but   The identity is good and the backend is not. They are
 *   no session       authenticated, so throwing them back to the phone form
 *                    would be a lie about what is wrong; this screen offers
 *                    the only two things that help - retry, or sign out.
 *   signed in        <Dashboard>, on THEIR portfolio id.
 *
 * The dashboard itself moved to Dashboard.jsx unchanged, except that the
 * hardcoded `PORTFOLIO_ID = 1` became a prop. This file is the only thing in
 * the app that decides whose data is on screen.
 *
 * <UnlockProvider> wraps the dashboard rather than living inside it, because
 * the holdings panel is mounted in two different branches of Dashboard and the
 * paid editing round has to survive moving between them. See the note at the
 * top of payments/UnlockProvider.jsx.
 */

import { useAuth } from './auth/auth-context'
import LoginScreen from './auth/LoginScreen'
import Dashboard from './Dashboard'
import { UnlockProvider } from './payments/UnlockProvider'

export default function App() {
  const { isLoading, firebaseUser, session, sessionError, portfolioId, retrySession, logout } =
    useAuth()

  if (isLoading) {
    return (
      <main className="page page--centered">
        <div className="status">
          <span className="spinner" aria-hidden="true" />
          <p className="status__title">Signing you in…</p>
        </div>
      </main>
    )
  }

  if (!firebaseUser) {
    return <LoginScreen />
  }

  // Authenticated, but the backend has not given us an account to read. Almost
  // always the API not being up yet, which is why the message names it.
  if (!session) {
    return (
      <main className="page page--centered">
        <div className="status status--error" role="alert">
          <p className="status__badge">{sessionError?.code ?? 'session_error'}</p>
          <p className="status__title">Signed in, but the API did not answer</p>
          <p className="status__detail">
            {sessionError?.message ??
              'Could not load your account from the backend. It may still be starting.'}
          </p>
          <div className="status__actions">
            <button type="button" className="button" onClick={retrySession}>
              Retry
            </button>
            <button type="button" className="button button--ghost" onClick={logout}>
              Sign out
            </button>
          </div>
        </div>
      </main>
    )
  }

  return (
    <UnlockProvider>
      <Dashboard portfolioId={portfolioId} />
    </UnlockProvider>
  )
}
