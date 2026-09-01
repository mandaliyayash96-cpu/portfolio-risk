/**
 * Owns "is this editing round paid for", for as long as the dashboard is open.
 *
 * WHY THE STATE LIVES HERE AND NOT IN THE PANEL
 * ---------------------------------------------
 * <ManageHoldings> is mounted in TWO places - under the dashboard, and on the
 * empty-portfolio error page as its recovery path. Adding the first holding
 * moves the app from one to the other, which unmounts the panel and mounts a
 * new one. State held inside it would be lost at exactly that moment: the user
 * would pay ₹9, add a position, and find the panel locked again mid-round.
 *
 * Held here - above both branches, in <App> - a remount costs nothing.
 *
 * WHY IT IS NOT PERSISTED
 * -----------------------
 * Nothing here touches localStorage, and that is the product decision made
 * mechanical: a reload starts a new round, and a new round costs ₹9. Restoring
 * an unlock across reloads would need somewhere to restore it FROM, and every
 * such place is either a lie the client tells itself or an endpoint that would
 * let a client skip paying.
 *
 * WHAT THIS COMPONENT IS NOT
 * --------------------------
 * It is not the gate. The server refuses every unpaid write with a 402
 * whatever this state says; `isUnlocked` decides which SCREEN is shown, not
 * what is permitted. That is why the fallback in unlock-context.js can be a
 * plain `false` with no security consequence.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '../api/client'
import {
  createUnlockOrder,
  finishEditingRound,
  loadCheckout,
  openCheckout,
  verifyUnlockPayment,
} from '../api/payments'
import { useAuth } from '../auth/auth-context'
import { UnlockContext } from './unlock-context'

export function UnlockProvider({ children }) {
  const { phone } = useAuth()
  const [isUnlocked, setIsUnlocked] = useState(false)
  const [isPaying, setIsPaying] = useState(false)
  const [error, setError] = useState(null)

  // Read by the unmount cleanup below, which must not re-run every time the
  // lock state changes - a dependency on `isUnlocked` would fire the cleanup
  // on unlock and end the round it just paid for. Synced in an effect rather
  // than assigned during render, which React forbids: a ref written while
  // rendering is a value that can disagree with what was painted.
  const unlockedRef = useRef(false)
  useEffect(() => {
    unlockedRef.current = isUnlocked
  }, [isUnlocked])

  /**
   * Buy a round: order -> checkout -> verify.
   *
   * Resolves true when the round is open. Never throws: a failed or cancelled
   * payment is an ordinary outcome here, and it is reported through `error`
   * so the panel can render it beside the button that caused it.
   */
  const unlock = useCallback(async () => {
    if (isPaying || isUnlocked) return isUnlocked

    setIsPaying(true)
    setError(null)
    try {
      // Both before opening the sheet: a CDN failure or a 503 from our own
      // backend should be a message on the panel, not a half-open checkout.
      const [order] = await Promise.all([createUnlockOrder(), loadCheckout()])
      const receipt = await openCheckout(order, { contact: phone ?? '' })

      // The payment exists at Razorpay by now and still means nothing here.
      // THIS is the call that unlocks anything, because it is the one the
      // server checks a signature on.
      const verified = await verifyUnlockPayment(receipt)
      if (!verified?.unlocked) {
        throw new ApiError('Payment was taken but the unlock did not open. Please contact support.', {
          code: 'unlock_not_opened',
        })
      }

      setIsUnlocked(true)
      return true
    } catch (paymentError) {
      setError(
        paymentError instanceof ApiError
          ? paymentError
          : new ApiError('Payment failed. Nothing has been charged.'),
      )
      return false
    } finally {
      setIsPaying(false)
    }
  }, [isPaying, isUnlocked, phone])

  /**
   * End the round - the user closed the panel.
   *
   * The UI locks IMMEDIATELY and the server is told afterwards, without
   * awaiting it. The user has finished editing either way, and a slow or
   * failed request must not leave the panel looking open. If the call is lost,
   * the grant expires on the server twenty minutes after payment.
   */
  const lock = useCallback(() => {
    setIsUnlocked(false)
    setError(null)
    finishEditingRound().catch(() => {
      // Deliberately silent. There is nothing the user can do about it and
      // nothing they need to know: the round is over on this screen, and the
      // server's TTL closes it there.
    })
  }, [])

  const clearError = useCallback(() => setError(null), [])

  // Leaving the dashboard - a logout, or the tab's last render - ends the
  // round. A reload does not reach this, which is what the server's TTL is
  // for; this only covers the departures React can see.
  useEffect(() => {
    return () => {
      if (unlockedRef.current) {
        finishEditingRound().catch(() => {})
      }
    }
  }, [])

  const value = useMemo(
    () => ({ isUnlocked, isPaying, error, unlock, lock, clearError }),
    [isUnlocked, isPaying, error, unlock, lock, clearError],
  )

  return <UnlockContext.Provider value={value}>{children}</UnlockContext.Provider>
}
