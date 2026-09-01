/**
 * Owns "is this editing round paid for", and the pay-at-submit choreography.
 *
 * THE FLOW, AND WHY IT IS THIS WAY ROUND
 * --------------------------------------
 * The panel used to be locked until ₹9 was paid, which asked a first-time user
 * for money before they had seen a form, let alone decided they wanted to fill
 * one in. Now every form is open, and payment is asked for at the moment the
 * server actually refuses the write:
 *
 *   gatedWrite(summary, perform)
 *     -> perform()                          the write, tried for real
 *     -> resolves                           done. No modal, nothing charged.
 *     -> rejects with 402                   park the job, open the modal
 *     -> rejects with anything else         the caller's problem, rethrown
 *
 *   payAndRetry()   order -> checkout -> verify -> perform() AGAIN
 *
 * The retry is the part that makes the promise honest: `gatedWrite` resolves
 * with the write's real result whenever it finally lands, so the caller writes
 * one `await` and never learns whether a payment happened in the middle of it.
 *
 * WHY THE JOB IS A REF AND THE MODAL IS STATE
 * -------------------------------------------
 * `jobRef` holds the two things React must not re-render for - the function to
 * re-run and the deferred's resolve/reject. `pending` holds the one thing the
 * screen needs, which is what to say the user is buying. Putting the resolvers
 * in state would make every keystroke-driven re-render carry a pair of
 * closures, and putting the summary in a ref would mean the modal never
 * repaints when it changes.
 *
 * WHY THE STATE LIVES HERE AND NOT IN THE PANEL
 * ---------------------------------------------
 * <ManageHoldings> is mounted in TWO places - under the dashboard, and on the
 * empty-portfolio error page as its recovery path. Adding the first holding
 * moves the app from one to the other, which unmounts the panel and mounts a
 * new one. A paid round held inside it would be lost at exactly that moment.
 * Held here - above both branches, in <App> - a remount costs nothing.
 *
 * The holdings table's delete button goes through the same `gatedWrite`, and it
 * lives in <Dashboard>, which is another reason this cannot be panel state.
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
 * It is not the gate. The server refuses every unpaid write with a 402 whatever
 * this state says - which is exactly why the flow above can be built out of
 * "just try it and see". `isUnlocked` decides which SCREEN is shown, never what
 * is permitted.
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
import PaymentModal from './PaymentModal'
import { PAYMENT_CANCELLED, UnlockContext, isPaymentRequired } from './unlock-context'

export function UnlockProvider({ children }) {
  const { phone } = useAuth()
  const [isUnlocked, setIsUnlocked] = useState(false)
  // Checkout is open, or the order/verify calls around it are in flight.
  const [isPaying, setIsPaying] = useState(false)
  // The parked write is being re-run, after a payment that verified.
  const [isRetrying, setIsRetrying] = useState(false)
  // What the modal is asking the user to pay for. null when it is closed.
  const [pending, setPending] = useState(null)
  // True from the moment a payment verifies until the modal closes. Exists so
  // the modal can say "Payment verified" without lying on the OTHER path that
  // reaches the same retry - pressing "Try again" after a write that failed on
  // its own merits, where no money moved and nothing was verified.
  const [justPaid, setJustPaid] = useState(false)
  // {error, phase}. `phase` is what tells the modal whether to offer "Pay ₹9"
  // again or "Try again" - a declined card and a rejected ticker need
  // different buttons, and flattening them into one error would give the user
  // a Pay button for a problem no payment fixes.
  const [failure, setFailure] = useState(null)

  const jobRef = useRef(null)

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
   * Attempt a write; on a 402, park it and open the payment modal.
   *
   * Resolves with whatever `perform` resolves with, whether that is on the
   * first attempt or after a payment. Rejects with the write's own error for
   * any other failure, and with code `payment_cancelled` if the user closes
   * the modal without paying.
   *
   * `perform` is called MORE THAN ONCE in the payment path, so it has to be
   * re-runnable: it is passed as a thunk rather than a promise for exactly that
   * reason. A promise would already have been started - and already have
   * failed - by the time it got here.
   *
   * ONE PARKED JOB AT A TIME
   * -----------------------
   * The modal is a scrim over the whole page with focus trapped inside it, so
   * a second submit while one is parked should be unreachable. The guard below
   * exists anyway, because the failure mode if it ever happened is silent and
   * permanent: overwriting `jobRef` would drop the first job's `resolve` on the
   * floor, and the form that called it would sit on "Adding…" forever. Refusing
   * the second write is a visible error; losing the first one is not.
   */
  const gatedWrite = useCallback(
    (summary, perform) =>
      new Promise((resolve, reject) => {
        perform().then(resolve, (writeError) => {
          if (!isPaymentRequired(writeError)) {
            reject(writeError)
            return
          }
          if (jobRef.current) {
            reject(
              new ApiError('Finish the payment already in progress, then try this again.', {
                code: 'payment_in_progress',
              }),
            )
            return
          }
          jobRef.current = { perform, resolve, reject }
          setFailure(null)
          setPending({ summary })
        })
      }),
    [],
  )

  /**
   * Buy a round: order -> checkout -> verify.
   *
   * Throws rather than swallowing, because the caller below needs to know
   * whether to go on and retry the write. The three ways this fails are
   * distinguished by `openCheckout` and kept distinct all the way to the modal:
   * cancelled, declined, and "we could not take payments at all".
   */
  const purchase = useCallback(async () => {
    // Both before opening the sheet: a CDN failure or a 503 from our own
    // backend should be a message on the modal, not a half-open checkout.
    const [order] = await Promise.all([createUnlockOrder(), loadCheckout()])
    const receipt = await openCheckout(order, { contact: phone ?? '' })

    // The payment exists at Razorpay by now and still means nothing here. THIS
    // is the call that unlocks anything, because it is the one the server
    // checks a signature on.
    const verified = await verifyUnlockPayment(receipt)
    if (!verified?.unlocked) {
      throw new ApiError(
        'Payment was taken but the unlock did not open. Please contact support.',
        { code: 'unlock_not_opened' },
      )
    }
    setIsUnlocked(true)
    setJustPaid(true)
  }, [phone])

  /**
   * The modal's primary button: pay if we must, then save what they entered.
   *
   * Skips the payment leg entirely when a round is already open, which is what
   * makes this double as the "Try again" button after a write that failed for
   * its own reasons - a bad ticker, say. The user is not charged twice for one
   * round, and the second attempt goes straight to the write.
   */
  const payAndRetry = useCallback(async () => {
    if (isPaying || isRetrying || !jobRef.current) return

    setFailure(null)

    if (!unlockedRef.current) {
      setIsPaying(true)
      try {
        await purchase()
      } catch (paymentError) {
        setFailure({
          error:
            paymentError instanceof ApiError
              ? paymentError
              : new ApiError('Payment failed. Nothing has been charged.'),
          phase: 'payment',
        })
        return
      } finally {
        setIsPaying(false)
      }
    }

    // Paid. Now save the thing they filled in, which is the only reason any of
    // this happened.
    setIsRetrying(true)
    try {
      const value = await jobRef.current.perform()
      const { resolve } = jobRef.current
      jobRef.current = null
      setPending(null)
      setJustPaid(false)
      resolve(value)
    } catch (writeError) {
      // A 402 on the RETRY means this screen's idea of the round is stale - the
      // grant expired between paying and saving (the server's TTL is twenty
      // minutes) or was finished from somewhere else. Left as a write failure
      // it would offer "Try again", which would 402 again forever; corrected to
      // a payment failure it offers the one thing that actually helps.
      if (isPaymentRequired(writeError)) {
        setIsUnlocked(false)
        unlockedRef.current = false
        setJustPaid(false)
        setFailure({
          error: new ApiError(
            'That editing round expired before this could be saved. Pay to open a new one — your details are still here.',
            { code: 'unlock_expired' },
          ),
          phase: 'payment',
        })
        return
      }
      // The round IS open - the payment went through and this is the write
      // failing on its own merits. Kept in the modal so the user can fix it and
      // press again without paying a second time.
      setFailure({ error: writeError, phase: 'write' })
    } finally {
      setIsRetrying(false)
    }
  }, [isPaying, isRetrying, purchase])

  /**
   * Close the modal without saving.
   *
   * The rejection carries the WRITE's error when there is one, so a user who
   * paid, hit a rejected ticker and then gave up sees the real reason on the
   * form rather than "payment cancelled" - which would be a lie about a payment
   * that succeeded. Otherwise it is the cancellation sentinel, which callers
   * treat as "keep their data, say nothing".
   */
  const cancelPending = useCallback(() => {
    if (isPaying || isRetrying) return

    const job = jobRef.current
    jobRef.current = null
    setPending(null)
    setJustPaid(false)

    const writeFailure = failure?.phase === 'write' ? failure.error : null
    setFailure(null)

    job?.reject(
      writeFailure ??
        new ApiError('Payment cancelled — nothing was saved.', { code: PAYMENT_CANCELLED }),
    )
  }, [failure, isPaying, isRetrying])

  /**
   * End the round - the user is done editing.
   *
   * The UI locks IMMEDIATELY and the server is told afterwards, without
   * awaiting it. The user has finished either way, and a slow or failed request
   * must not leave the panel looking open. If the call is lost, the grant
   * expires on the server twenty minutes after payment.
   */
  const lock = useCallback(() => {
    setIsUnlocked(false)
    finishEditingRound().catch(() => {
      // Deliberately silent. There is nothing the user can do about it and
      // nothing they need to know: the round is over on this screen, and the
      // server's TTL closes it there.
    })
  }, [])

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
    () => ({
      isUnlocked,
      isPaying,
      isRetrying,
      justPaid,
      pending,
      failure,
      gatedWrite,
      payAndRetry,
      cancelPending,
      lock,
    }),
    [
      isUnlocked,
      isPaying,
      isRetrying,
      justPaid,
      pending,
      failure,
      gatedWrite,
      payAndRetry,
      cancelPending,
      lock,
    ],
  )

  return (
    <UnlockContext.Provider value={value}>
      {children}
      {/*
        Rendered here rather than inside the panel, and mounted once for the
        whole app. Three different components can park a write - the add form,
        the CSV import and the holdings table's delete - and they live in
        different branches of the tree. One modal above all of them is the only
        arrangement where two of them cannot open two.
      */}
      <PaymentModal />
    </UnlockContext.Provider>
  )
}
