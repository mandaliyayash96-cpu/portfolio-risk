/**
 * The auth context object and the hook that reads it.
 *
 * Separate from AuthContext.jsx for the same reason theme-context.js is
 * separate from ThemeProvider.jsx: Vite's Fast Refresh can only hot-swap a
 * module whose exports are all components, and a file exporting both
 * <AuthProvider> and useAuth() forces a full reload on every edit -
 * `react-refresh/only-export-components` flags exactly that.
 */

import { createContext, useContext } from 'react'

export const AuthContext = createContext(null)

/**
 * A signed-out, still-loading value for when there is no provider above.
 *
 * Unlike the theme's fallback this one is not "usable" - there is no sensible
 * anonymous identity - so it reports `isLoading: true`, which renders the boot
 * splash rather than flashing a login screen at a component mounted in
 * isolation (a test, a Storybook story).
 */
const FALLBACK = {
  firebaseUser: null,
  session: null,
  isLoading: true,
  isAuthenticated: false,
  sessionError: null,
  phone: null,
  portfolioId: null,
  login: async () => {},
  logout: async () => {},
  retrySession: () => {},
}

/**
 * The signed-in user, their backend session, and the two verbs.
 *
 * @returns {{
 *   firebaseUser: import('firebase/auth').User | null,
 *   session: {user_id: number, phone: string, portfolio_id: number} | null,
 *   isLoading: boolean,
 *   isAuthenticated: boolean,
 *   sessionError: Error | null,
 *   phone: string | null,
 *   portfolioId: number | null,
 *   login: () => Promise<object>,
 *   logout: () => Promise<void>,
 *   retrySession: () => void,
 * }}
 */
export function useAuth() {
  return useContext(AuthContext) ?? FALLBACK
}
