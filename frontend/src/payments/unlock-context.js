/**
 * The editing-unlock context and the hook that reads it.
 *
 * Separate from UnlockProvider.jsx for the same reason theme-context.js and
 * auth-context.js are separate from their providers: Fast Refresh can only
 * hot-swap a module whose exports are all components.
 */

import { createContext, useContext } from 'react'

export const UnlockContext = createContext(null)

/**
 * LOCKED is the fallback, and that is the safe direction.
 *
 * A component rendered with no provider above it gets a panel that cannot be
 * edited, rather than one that looks editable and 402s on the first click.
 * (The server would refuse either way - this only decides which screen the
 * user sees.)
 */
const FALLBACK = {
  isUnlocked: false,
  isPaying: false,
  error: null,
  unlock: async () => {},
  lock: () => {},
  clearError: () => {},
}

/**
 * Whether this editing round is paid for, and the two verbs that change it.
 *
 * @returns {{
 *   isUnlocked: boolean,
 *   isPaying: boolean,
 *   error: Error | null,
 *   unlock: () => Promise<boolean>,
 *   lock: () => void,
 *   clearError: () => void,
 * }}
 */
export function useUnlock() {
  return useContext(UnlockContext) ?? FALLBACK
}
