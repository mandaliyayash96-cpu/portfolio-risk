/**
 * The payments half of the API surface, plus the checkout script that goes
 * with it.
 *
 * Split from client.js the way alerts.js is: this is one feature's calls, and
 * they share nothing with the risk report but the envelope and the Bearer
 * token - both of which come from client.js and neither of which is repeated
 * here.
 *
 * WHAT THIS FILE DOES NOT DO
 * --------------------------
 * It does not decide whether the user may edit. The server does, on every
 * write, and the only thing that unlocks anything is a signature it verified
 * itself. Everything below is the choreography that gets a signature made -
 * if any of it were bypassed, the write endpoints would still answer 402.
 */

import { ApiError, postEnveloped } from './client'

/** Razorpay's hosted checkout. The only script this app loads from a CDN. */
const CHECKOUT_SRC = 'https://checkout.razorpay.com/v1/checkout.js'

/** What the panel is worth, for the copy that has to say a price. */
export const UNLOCK_PRICE_LABEL = '₹9'

/**
 * Start an editing round: a ₹9 order, and the public key to open it with.
 *
 * Sends no body. The amount is a server-side constant on purpose - an amount
 * the browser can name is an amount the browser can set to zero.
 *
 * @returns {Promise<{order_id: string, amount: number, currency: string,
 *   key_id: string}>} `amount` is in PAISE, which is what the checkout widget
 *   expects too, so it is passed straight through without conversion.
 */
export function createUnlockOrder() {
  return postEnveloped('/api/payments/order/')
}

/**
 * Hand the signed callback back for verification.
 *
 * This is the call that actually unlocks anything. Until it resolves, the
 * payment has happened at Razorpay but means nothing here - the server has not
 * checked the signature yet, and an unverified payment unlocks nothing.
 */
export function verifyUnlockPayment({ razorpay_order_id, razorpay_payment_id, razorpay_signature }) {
  return postEnveloped('/api/payments/verify/', {
    razorpay_order_id,
    razorpay_payment_id,
    razorpay_signature,
  })
}

/**
 * End the editing round - the user closed the panel.
 *
 * Idempotent, and deliberately fire-and-forget at the call sites: a failed
 * "finish" must never block the UI from locking. The grant it failed to
 * consume expires on its own twenty minutes after payment.
 */
export function finishEditingRound() {
  return postEnveloped('/api/payments/finish/')
}

/**
 * Load Razorpay's checkout script once, and resolve when `window.Razorpay` exists.
 *
 * Loaded on demand rather than in index.html: a signed-out visitor, and a
 * signed-in one who never edits, should not pay for a third-party script on
 * every page load. The promise is memoised, so two clicks do not inject two
 * <script> tags.
 *
 * A CDN that cannot be reached must not look like a failed payment, hence the
 * explicit ApiError with its own code rather than a bare reject.
 */
let checkoutScript = null

export function loadCheckout() {
  if (window.Razorpay) return Promise.resolve(window.Razorpay)
  if (checkoutScript) return checkoutScript

  checkoutScript = new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = CHECKOUT_SRC
    script.async = true
    script.onload = () => {
      if (window.Razorpay) resolve(window.Razorpay)
      else reject(new ApiError('Razorpay checkout loaded but did not start.', { code: 'checkout_unavailable' }))
    }
    script.onerror = () => {
      // Let a later attempt retry rather than caching the failure forever -
      // this is usually a dropped connection, not a broken deployment.
      checkoutScript = null
      script.remove()
      reject(
        new ApiError(
          'Could not load the Razorpay checkout. Check your connection and try again.',
          { code: 'checkout_unavailable' },
        ),
      )
    }
    document.body.appendChild(script)
  })

  return checkoutScript
}

/**
 * Open checkout for an order and resolve with what Razorpay hands back.
 *
 * THREE OUTCOMES, AND THEY ARE NOT THE SAME THING
 * -----------------------------------------------
 *   paid       -> resolves with {razorpay_order_id, razorpay_payment_id,
 *                 razorpay_signature}, which still has to be VERIFIED.
 *   dismissed  -> rejects with code `checkout_dismissed`. The user closed the
 *                 sheet. Not an error to apologise for; the caller says
 *                 "payment cancelled" and leaves the panel locked.
 *   failed     -> rejects with code `checkout_failed` and Razorpay's own
 *                 description, which names the real reason (card declined,
 *                 wrong OTP at the bank, expired attempt).
 *
 * Flattening those three into "payment failed" is how a user who simply
 * changed their mind ends up thinking the app is broken.
 *
 * @param {object} order   as returned by createUnlockOrder()
 * @param {object} profile {name, contact} to prefill; contact is the phone the
 *                         user signed in with, which saves them typing it.
 */
export function openCheckout(order, profile = {}) {
  return new Promise((resolve, reject) => {
    let settled = false

    const razorpay = new window.Razorpay({
      key: order.key_id,
      order_id: order.order_id,
      amount: order.amount,
      currency: order.currency,
      name: 'Portfolio Risk',
      description: 'Unlock holdings editing for one session',
      prefill: { contact: profile.contact ?? '' },
      // The rupee-coloured accent, so the sheet does not arrive looking like a
      // different product. Razorpay renders its own light sheet in both
      // themes; this is the one colour we get to choose.
      theme: { color: '#1d4ed8' },
      handler(response) {
        settled = true
        resolve(response)
      },
      modal: {
        ondismiss() {
          // Fires on a successful payment too in some flows, after `handler`.
          // The guard is what keeps a completed payment from being reported as
          // a cancellation a moment later.
          if (settled) return
          settled = true
          reject(new ApiError('Payment cancelled.', { code: 'checkout_dismissed' }))
        },
      },
    })

    razorpay.on('payment.failed', (event) => {
      if (settled) return
      settled = true
      reject(
        new ApiError(event?.error?.description ?? 'The payment did not go through.', {
          code: 'checkout_failed',
        }),
      )
    })

    razorpay.open()
  })
}
