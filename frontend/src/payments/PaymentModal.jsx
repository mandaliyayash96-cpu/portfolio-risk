/**
 * The ₹9 prompt, shown only when the server has actually refused a write.
 *
 * By the time this is on screen the user has already filled something in and
 * pressed Save, so it opens with THEIR sentence - "Add RELIANCE.NS ×10" - and
 * puts the price underneath. The old flow said the price first, to somebody who
 * had not yet decided they wanted to type anything.
 *
 * Nothing here talks to Razorpay. The provider owns the order/checkout/verify
 * choreography and the parked write; this file owns one dialog, its focus, and
 * the wording for four states:
 *
 *   idle        the charge, and a button that pays it
 *   paying      checkout is open on top of us, everything disabled
 *   saving      paid, re-running the write they submitted
 *   failed      why, and which button helps - see `needsPayment` below
 *
 * ACCESSIBILITY
 * -------------
 * A real dialog: `aria-modal`, a labelled title, focus moved in on open and
 * restored on close, Tab wrapped inside, Escape to cancel. All of it is
 * suspended while a payment is in flight, because there is a Razorpay iframe
 * over this and stealing focus back from it - or letting Escape close the
 * window that is mid-transaction - is how a user ends up paying with nothing
 * on screen to receive it.
 */

import { useCallback, useEffect, useRef } from 'react'

import { UNLOCK_PRICE_LABEL } from '../api/payments'
import { useUnlock } from './unlock-context'

/** Focusable children, for the tab trap. */
const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function LockIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M6.5 10.5V8a5.5 5.5 0 0 1 11 0v2.5M5.5 10.5h13a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function PaymentModal() {
  const {
    pending,
    failure,
    isPaying,
    isRetrying,
    isUnlocked,
    justPaid,
    payAndRetry,
    cancelPending,
  } = useUnlock()

  const dialogRef = useRef(null)
  const primaryRef = useRef(null)
  const restoreFocusRef = useRef(null)

  const isBusy = isPaying || isRetrying
  const isOpen = pending !== null

  /**
   * Which problem the user has, and therefore which button they need.
   *
   * A round that is open means the money side is settled, so a failure here is
   * the write's own - a rejected ticker, a CSV the backend would not take - and
   * the button says "Try again", not "Pay". Offering to charge somebody a
   * second ₹9 to fix a typo is the single worst thing this dialog could do.
   */
  const needsPayment = !isUnlocked || failure?.phase === 'payment'

  const close = useCallback(() => {
    if (isBusy) return
    cancelPending()
  }, [cancelPending, isBusy])

  // Remember where focus was, move it into the dialog, and put it back on the
  // way out. Without the restore, dismissing the modal drops the user at the
  // top of the document rather than on the button they pressed.
  useEffect(() => {
    if (!isOpen) return undefined

    restoreFocusRef.current = document.activeElement
    primaryRef.current?.focus()

    const restore = restoreFocusRef.current
    return () => {
      if (restore instanceof HTMLElement && document.contains(restore)) {
        restore.focus()
      }
    }
  }, [isOpen])

  // The page behind a modal must not scroll. Restored to whatever it was, not
  // to '', so this cannot clobber a value someone else set.
  useEffect(() => {
    if (!isOpen) return undefined
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [isOpen])

  // Escape closes; Tab wraps. Both are off while paying - see the note at the
  // top about the checkout iframe.
  useEffect(() => {
    if (!isOpen) return undefined

    function onKeyDown(event) {
      if (event.key === 'Escape') {
        event.preventDefault()
        close()
        return
      }
      if (event.key !== 'Tab' || isBusy) return

      const focusable = dialogRef.current?.querySelectorAll(FOCUSABLE)
      if (!focusable?.length) return

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [isOpen, isBusy, close])

  if (!isOpen) return null

  const { summary } = pending

  let primaryLabel = `Pay ${UNLOCK_PRICE_LABEL} & Save`
  if (isPaying) primaryLabel = 'Opening checkout…'
  else if (isRetrying) primaryLabel = 'Saving…'
  else if (!needsPayment) primaryLabel = 'Try again'

  return (
    <div
      className="modal-scrim"
      // A click on the backdrop is a dismissal, but only when it is the
      // backdrop itself - without the target check, a click that starts inside
      // the dialog and drifts out closes it and throws the form away.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close()
      }}
    >
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="payment-modal-title"
        aria-describedby="payment-modal-summary"
      >
        <div className="modal__head">
          <span className="modal__icon" aria-hidden="true">
            <LockIcon />
          </span>
          <div className="modal__heading">
            <h2 className="modal__title" id="payment-modal-title">
              {needsPayment ? 'One payment to save this' : 'Save this change'}
            </h2>
            <p className="modal__subtitle">
              {needsPayment
                ? `Editing costs ${UNLOCK_PRICE_LABEL} per round. Nothing you typed has been lost.`
                : 'Your editing round is already paid for — this just needs another go.'}
            </p>
          </div>
          <button
            type="button"
            className="modal__close"
            onClick={close}
            disabled={isBusy}
            aria-label="Close without saving"
          >
            ✕
          </button>
        </div>

        <div className="modal__body">
          <div className="pay-summary" id="payment-modal-summary">
            <p className="pay-summary__label">You’re about to</p>
            <p className="pay-summary__action">{summary.action}</p>
            {summary.detail && <p className="pay-summary__detail">{summary.detail}</p>}
          </div>

          {/* The charge, on its own line, said once. */}
          {needsPayment && (
            <div className="pay-line">
              <span className="pay-line__label">
                Editing round
                <span className="pay-line__sub">
                  Add, import and delete as much as you like until you close it
                </span>
              </span>
              <span className="pay-line__amount">{UNLOCK_PRICE_LABEL}</span>
            </div>
          )}

          {failure && (
            <p className="banner banner--error" role="alert">
              {failure.error?.code && failure.error.code !== 'checkout_dismissed' && (
                <strong>{failure.error.code}: </strong>
              )}
              {failure.error?.message ?? 'Something went wrong.'}
            </p>
          )}

          {/*
            Two different sentences, because two different journeys arrive here.
            `justPaid` is the one where money actually moved and the server
            verified a signature; without it this is a second attempt at a write
            on a round that was already open, and claiming a payment was
            verified would be inventing one.
          */}
          {isRetrying && (
            <p className="banner banner--good" role="status">
              {justPaid
                ? `Payment verified. Saving your ${summary.noun ?? 'changes'}…`
                : `Saving your ${summary.noun ?? 'changes'}…`}
            </p>
          )}

          {needsPayment && !failure && (
            <p className="pay-note">
              You will be taken to Razorpay to pay. We never see your card
              details, and <strong>the round only opens once the payment is
              verified</strong> — if it is cancelled or declined, nothing is
              charged and your details stay exactly as you left them.
            </p>
          )}
        </div>

        <div className="modal__foot">
          <button
            type="button"
            className="button button--ghost"
            onClick={close}
            disabled={isBusy}
          >
            Cancel
          </button>
          <button
            ref={primaryRef}
            type="button"
            className="button"
            onClick={payAndRetry}
            disabled={isBusy}
          >
            {isBusy && <span className="spinner spinner--inline" aria-hidden="true" />}
            {primaryLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
