/**
 * The editing-unlock context and the hook that reads it.
 *
 * Separate from UnlockProvider.jsx for the same reason theme-context.js and
 * auth-context.js are separate from their providers: Fast Refresh can only
 * hot-swap a module whose exports are all components.
 *
 * WHAT CHANGED WITH PAY-AT-SUBMIT
 * -------------------------------
 * This context used to expose `unlock()` - "take ₹9, then let them type". It
 * no longer does, because nothing in the app asks for money before the user has
 * done anything. What it exposes instead is `gatedWrite`, which RUNS the write
 * and only involves payment if the server says payment is needed.
 *
 * That inversion is the whole feature, and it is why there is no `unlock` here
 * any more: an unlock is now something that happens on the way to saving
 * something, never a thing a user buys on its own.
 */

import { createContext, useContext } from 'react'

export const UnlockContext = createContext(null)

/**
 * The fallback, for a component rendered with no provider above it.
 *
 * `gatedWrite` still performs the write - which is the safe direction, because
 * the SERVER is the gate. Without a provider there is no modal to open, so a
 * 402 simply surfaces to the caller as the error it is, and the caller already
 * knows how to render an error. The old fallback returned a locked panel; there
 * is no locked panel any more, so there is nothing to lock.
 */
const FALLBACK = {
  isUnlocked: false,
  isPaying: false,
  isRetrying: false,
  justPaid: false,
  pending: null,
  failure: null,
  gatedWrite: (summary, perform) => perform(),
  payAndRetry: async () => {},
  cancelPending: () => {},
  lock: () => {},
}

/**
 * The paid-editing state, and the verbs that move it.
 *
 * @returns {{
 *   isUnlocked: boolean,
 *   isPaying: boolean,
 *   isRetrying: boolean,
 *   justPaid: boolean,
 *   pending: {summary: {action: string, detail?: string, noun?: string}} | null,
 *   failure: {error: Error, phase: 'payment' | 'write'} | null,
 *   gatedWrite: <T>(
 *     summary: {action: string, detail?: string, noun?: string},
 *     perform: () => Promise<T>,
 *   ) => Promise<T>,
 *   payAndRetry: () => Promise<void>,
 *   cancelPending: () => void,
 *   lock: () => void,
 * }}
 */
export function useUnlock() {
  return useContext(UnlockContext) ?? FALLBACK
}

/**
 * Did the server refuse this because the round is not paid for?
 *
 * Checks the STATUS first and the code second. The status is the contract -
 * `PaymentRequiredError.status_code` is 402 and nothing else in the API returns
 * one - while the code is a string that a future refactor could rename. Both
 * are accepted so neither alone is load-bearing.
 */
export function isPaymentRequired(error) {
  return error?.status === 402 || error?.code === 'payment_required'
}

/**
 * The sentinel a cancelled payment rejects with.
 *
 * Callers test for this code to tell "the user changed their mind" apart from
 * "the write failed". The first must leave their typed data alone and show no
 * error; the second is a real failure with a real message.
 */
export const PAYMENT_CANCELLED = 'payment_cancelled'
