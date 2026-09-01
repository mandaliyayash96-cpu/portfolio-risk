/**
 * Owns "who is signed in", and turns that into a portfolio id.
 *
 * TWO HALVES, AND THEY FAIL SEPARATELY
 * ------------------------------------
 * Being signed in is two facts, not one:
 *
 *   1. Firebase says this browser holds a valid identity (`firebaseUser`).
 *      Persisted in localStorage, so it survives a refresh with no server
 *      involved.
 *   2. Our backend has an account and a portfolio for that identity
 *      (`session`, from POST /api/auth/session/).
 *
 * The first can be true while the second is not - the API is down, or still
 * starting. That is NOT a reason to throw the user back to the login screen:
 * they are authenticated, and signing in again would fix nothing. So the two
 * are tracked separately and <App> renders a retryable error for that state
 * instead of a phone form.
 *
 * WHY THE SESSION CALL RUNS ON EVERY BOOT
 * ---------------------------------------
 * The portfolio id could be cached in localStorage beside the Firebase token.
 * It deliberately is not. A cached id is a claim about server state that
 * nothing revalidates - and the moment it is wrong (a portfolio deleted, a
 * different backend, a stale demo database) every panel on the dashboard
 * silently reads somebody else's numbers, or 404s. One request on boot is a
 * cheap price for that id always being true.
 *
 * WHY `isLoading` IS DERIVED AND NOT STORED
 * -----------------------------------------
 * "Loading" is not an independent fact - it is entirely determined by the four
 * pieces of state below, and a fifth `useState` holding it would be a second
 * copy of an answer already on hand. Storing it also means writing it from
 * inside an effect, which cascades renders and is what
 * `react-hooks/set-state-in-effect` exists to stop. Computed here, the effect
 * only ever sets state from an async callback, which is the pattern that rule
 * is asking for.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { onAuthStateChanged, signOut } from 'firebase/auth'

import { startSession } from '../api/client'
import { auth } from '../firebase'
import { AuthContext } from './auth-context'

export function AuthProvider({ children }) {
  // Has Firebase answered "is anyone signed in" yet? Distinct from
  // `firebaseUser === null`, which is also what "signed out" looks like -
  // telling the two apart is what keeps the login screen from flashing over a
  // session that is about to be restored.
  const [authResolved, setAuthResolved] = useState(false)
  const [firebaseUser, setFirebaseUser] = useState(null)
  const [session, setSession] = useState(null)
  const [sessionError, setSessionError] = useState(null)

  /**
   * Firebase's answer to "who is signed in", now and whenever it changes.
   *
   * onAuthStateChanged fires once on subscribe with the restored user (or
   * null) - which is what makes a returning user land straight on the
   * dashboard - and again on every sign-in and sign-out, so `logout()` needs
   * to do nothing but call signOut and let this listener clear the rest.
   */
  useEffect(() => {
    return onAuthStateChanged(auth, (user) => {
      setAuthResolved(true)
      setFirebaseUser(user)
      if (!user) {
        // Signed out: drop the backend session with it, or the next sign-in
        // would flash the previous user's portfolio before its own arrives.
        setSession(null)
        setSessionError(null)
      }
    })
  }, [])

  /**
   * Exchange the Firebase identity for the backend session.
   *
   * Runs when there is a user, no session, and no failure standing. Those three
   * guards are also the retry mechanism: `retrySession` clears the error, which
   * changes a dependency, which re-runs this. No attempt counter needed.
   */
  useEffect(() => {
    if (!firebaseUser || session || sessionError) return undefined

    let ignore = false
    startSession()
      .then((data) => {
        if (!ignore) setSession(data)
      })
      .catch((error) => {
        // Kept, not thrown. <App> renders it with a Retry and a Sign out,
        // which are the only two things that can actually help.
        if (!ignore) setSessionError(error)
      })

    return () => {
      ignore = true
    }
  }, [firebaseUser, session, sessionError])

  /**
   * Told by <LoginScreen> that Firebase has just accepted an OTP.
   *
   * Starts no request: the credential is already inside Firebase by the time
   * this runs, so `onAuthStateChanged` fires, `firebaseUser` changes and the
   * effect above fetches the session on its own. Calling startSession() here
   * as well would POST twice for one sign-in.
   *
   * What it does do is clear any failure left over from a previous identity,
   * so a stale error cannot block the new one's session exchange.
   */
  const login = useCallback(() => {
    setSessionError(null)
  }, [])

  /**
   * Sign out of Firebase. The listener above clears everything else.
   *
   * The local state is cleared here as well rather than waiting, because
   * signOut() is a promise and the moment before it resolves is a moment of
   * the dashboard still showing somebody's positions.
   */
  const logout = useCallback(async () => {
    setSession(null)
    setSessionError(null)
    try {
      await signOut(auth)
    } catch (error) {
      // Nothing useful to do: the local state is already cleared, so the user
      // is looking at the login screen either way.
      console.warn('Sign-out failed', error?.code ?? error)
    }
  }, [])

  /** Re-run the session exchange after it failed. */
  const retrySession = useCallback(() => {
    setSessionError(null)
  }, [])

  const value = useMemo(() => {
    // Two waits, one flag: for Firebase to answer at all, and then for the
    // backend to answer for the user it named. An error ends the second one -
    // a failed session is resolved, not pending.
    const isLoading = !authResolved || Boolean(firebaseUser && !session && !sessionError)

    return {
      firebaseUser,
      session,
      isLoading,
      // Both halves. A component asking "is this user signed in" almost always
      // means "can I read their data", and that needs the portfolio id too.
      isAuthenticated: Boolean(firebaseUser && session),
      sessionError,
      // Preferred from the SESSION, not from firebaseUser.phoneNumber: the
      // backend's copy is the one it verified and keyed the account on, so the
      // header cannot disagree with the data below it.
      phone: session?.phone ?? firebaseUser?.phoneNumber ?? null,
      portfolioId: session?.portfolio_id ?? null,
      login,
      logout,
      retrySession,
    }
  }, [authResolved, firebaseUser, session, sessionError, login, logout, retrySession])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
