/**
 * Firebase error codes, turned into sentences a person can act on.
 *
 * Firebase's own `error.message` is written for a developer reading a console:
 * "Firebase: Error (auth/invalid-verification-code)." tells the user nothing
 * about what to do next. Every message below names the FIX, not the fault.
 *
 * The list is deliberately not exhaustive. It covers the codes this flow can
 * actually produce, plus the two that only ever appear during first-time setup
 * (`unauthorized-domain`, `operation-not-allowed`) - those two are worth
 * spelling out because they look like a broken app and are really a console
 * checkbox.
 */

/** Messages for the phone step - sending the code. */
const SEND_MESSAGES = {
  'auth/invalid-phone-number':
    'That does not look like a valid phone number. Use the international form, e.g. +91 98765 43210.',
  'auth/missing-phone-number': 'Enter a phone number first.',
  'auth/too-many-requests':
    'Too many attempts from this device. Wait a few minutes before trying again.',
  'auth/quota-exceeded': 'The daily SMS limit for this project has been reached. Try again later.',
  'auth/captcha-check-failed':
    'The reCAPTCHA check failed. Reload the page and try again.',
  'auth/invalid-app-credential':
    'The reCAPTCHA token was rejected. Reload the page and send the code again.',
  'auth/network-request-failed':
    'No connection to Firebase. Check your network and try again.',
  'auth/user-disabled': 'This number has been disabled. Contact support.',
  'auth/billing-not-enabled':
    'Phone sign-in is not billable on this Firebase project yet. Enable it in the console.',
  // Setup-time, not user-time. Both look like a broken button.
  'auth/operation-not-allowed':
    'Phone sign-in is switched off for this Firebase project. Enable it under Authentication → Sign-in method.',
  'auth/unauthorized-domain':
    'This address is not an authorised Firebase domain. Open the app at http://localhost:5173, or add this host under Authentication → Settings → Authorized domains.',
}

/** Messages for the code step - verifying what the user typed. */
const VERIFY_MESSAGES = {
  'auth/invalid-verification-code':
    'That code is not right. Check the six digits and try again.',
  'auth/missing-verification-code': 'Enter the six-digit code from the SMS.',
  'auth/code-expired': 'That code has expired. Send a new one.',
  // Firebase uses this for a confirmation object that has gone stale, which to
  // the user is the same thing as an expired code.
  'auth/session-expired': 'That code has expired. Send a new one.',
  'auth/too-many-requests':
    'Too many attempts. Wait a few minutes before trying again.',
  'auth/network-request-failed':
    'No connection to Firebase. Check your network and try again.',
  'auth/user-disabled': 'This number has been disabled. Contact support.',
}

/**
 * The message to show for a failure, given which step raised it.
 *
 * @param {unknown} error   whatever the Firebase SDK threw
 * @param {'send'|'verify'} step  which half of the flow was running
 * @returns {string} a sentence to render verbatim
 */
export function firebaseErrorMessage(error, step = 'send') {
  const code = error?.code ?? ''
  const table = step === 'verify' ? VERIFY_MESSAGES : SEND_MESSAGES
  if (table[code]) return table[code]

  // An unmapped code still has to say something useful, and the raw code is
  // the most useful thing left - it is what a search engine and the Firebase
  // docs both take. Better than "Something went wrong".
  if (code) return `Sign-in failed (${code}). Please try again.`
  return 'Sign-in failed. Please try again.'
}
