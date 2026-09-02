/**
 * The last number that successfully signed in, kept so the login form can
 * prefill it.
 *
 * WHAT THIS IS, AND MORE IMPORTANTLY WHAT IT IS NOT
 * -------------------------------------------------
 * This stores ONE string: a phone number in E.164, the same thing the user
 * typed into a visible form field. It is a convenience, in the same category
 * as the browser's own autofill, and it is deliberately the only thing here.
 *
 * It is NOT a session, and nothing downstream may treat it as one. In
 * particular it does not store, and must never be extended to store:
 *
 *   - an ID token or refresh token. Firebase owns those, in its own
 *     localStorage entries, under its own key schedule. A second copy of a
 *     credential is a second thing to leak and a second thing to expire
 *     wrong.
 *   - `portfolio_id`, or anything else the backend said. AuthContext.jsx
 *     re-fetches that on every boot on purpose - see the note there about a
 *     cached id being a claim about server state that nothing revalidates.
 *   - "this user was signed in", in any form. Whether a session exists is
 *     Firebase's answer to give, and a real login still means a fresh OTP and
 *     a token the backend verifies against Google's certificates.
 *
 * So the worst an attacker with access to this value learns is a phone number
 * they could have read off the login screen of the browser they are already
 * sitting at. It buys them nothing: the number alone cannot pass the OTP.
 *
 * WHY IT SURVIVES LOG OUT
 * -----------------------
 * Signing out clears the SESSION, which is the security boundary - the next
 * sign-in has to pass a real OTP again. The number is not part of that
 * boundary, and clearing it would only mean the user retypes their own phone
 * number to receive a code on that same phone. "Not you?" on the login screen
 * calls forgetPhone() for the case where that assumption is wrong.
 */

/**
 * Namespaced to match `portfolio-risk:theme` in theme.js.
 *
 * The prefix is the OLD product name and stays that way: a localStorage key is
 * not user-facing text, and renaming it would silently orphan the entries in
 * every browser that already has one - which for the theme key means every
 * returning user's colour choice quietly reverting. One namespace, chosen
 * once; the rebrand does not reach in here.
 */
export const STORAGE_KEY = 'portfolio-risk:last-phone'

/**
 * E.164, which is the only form Firebase accepts: a +, a country code that
 * cannot start with 0, then up to fourteen more digits.
 *
 * This module owns the rule rather than the login form, because the two places
 * that need it are "validate what the user typed" and "validate what came back
 * out of storage", and the second is the one reading from a source it does not
 * control. localStorage is writable by anything that runs on this origin and
 * survives across versions of this app - so a value in it is untrusted input,
 * and gets checked on the way out, not just on the way in.
 */
const E164 = /^\+[1-9]\d{7,14}$/

/** Is this a number Firebase would accept? */
export const isE164 = (value) => typeof value === 'string' && E164.test(value)

/**
 * The remembered number, or null.
 *
 * Returns null rather than throwing on every failure path - no entry, a
 * malformed entry, or storage that refuses to be read at all (Safari private
 * mode, a browser set to block site data). The caller's fallback is the
 * default dial code, so there is nothing a failure here can break beyond the
 * user typing their number as they did before.
 */
export function readRememberedPhone() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    return isE164(stored) ? stored : null
  } catch {
    return null
  }
}

/**
 * Remember a number that has just signed in successfully.
 *
 * Only ever called AFTER `confirm(code)` resolves. Storing it at "Send OTP"
 * would remember numbers that never passed verification - including typos,
 * which would then be prefilled back at the user on their next visit.
 *
 * Silently ignores anything that is not a valid E.164 string, so a caller
 * cannot poison the entry by passing a half-typed field.
 */
export function rememberPhone(number) {
  if (!isE164(number)) return
  try {
    window.localStorage.setItem(STORAGE_KEY, number)
  } catch {
    // Storage unavailable. The prefill is a convenience; losing it costs the
    // user one field of typing and nothing else.
  }
}

/** Forget it. Behind "Not you?" on the login screen. */
export function forgetPhone() {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing to do, and nothing depends on the removal having happened.
  }
}
