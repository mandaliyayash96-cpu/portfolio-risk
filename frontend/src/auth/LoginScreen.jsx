/**
 * Phone sign-in: a number, an SMS, six digits.
 *
 * THE reCAPTCHA IS THE PART THAT GOES WRONG
 * -----------------------------------------
 * Firebase requires an app-verification token before it will send an SMS, and
 * for the web that means reCAPTCHA. "Invisible" means the user never sees a
 * puzzle *unless Google decides they should* - not that there is nothing
 * there. Four rules follow from how it actually behaves, and most of them are
 * bugs that only appear on the SECOND attempt:
 *
 *   1. A reCAPTCHA token is SINGLE USE. Once signInWithPhoneNumber has
 *      consumed it - whether it succeeded or failed - the token is spent, and
 *      verify() would hand back that same spent string next time, because
 *      grecaptcha caches the response against the widget. Not retiring it is
 *      why phone screens "work once, then the button does nothing".
 *   2. But spending a TOKEN is not the same as spending the VERIFIER, and that
 *      difference is the whole of `resetVerifier` below. This screen used to
 *      destroy the widget after every attempt and build a new one. A brand-new
 *      widget arrives at Google's risk model with no history of this page, and
 *      a visitor with no history is precisely who gets shown the photo grid -
 *      so the rebuild that was meant to keep sign-in working was also making
 *      it harder. The verifier is now built ONCE and RESET between attempts:
 *      same guarantee that no token is used twice, far less suspicion.
 *   3. The container element must be in the DOM when the verifier is
 *      constructed, which is why it is built on the first click (against a
 *      ref) rather than at module load.
 *   4. StrictMode mounts every component twice in development. Without the
 *      cleanup below, the second mount would leave an orphaned widget behind
 *      and Firebase would complain about a container that already has one.
 *
 * IN DEVELOPMENT THERE IS NO WIDGET AT ALL
 * ----------------------------------------
 * Against localhost, src/firebase.js sets `appVerificationDisabledForTesting`
 * and Firebase swaps in a mock reCAPTCHA - no script from Google, no widget,
 * no challenge - which is what makes a Firebase TEST number sign in instantly
 * and with no photo grid. That flag is read by the RecaptchaVerifier
 * CONSTRUCTOR, which is why it is set at module scope over there rather than
 * here. Everything below still runs unchanged; it is just talking to a mock.
 * See firebase.js for the three guards that keep it out of a production build.
 *
 * THE NUMBER IS PREFILLED, THE VERIFICATION IS NOT SKIPPED
 * --------------------------------------------------------
 * A returning visitor arrives with their number already in the box, because
 * the last one to sign in successfully on this browser was written to
 * localStorage (remembered-phone.js). That is the whole of the "fast
 * re-login": one tap on Send OTP, then the six digits.
 *
 * It shortens the TYPING, never the CHECKING. There is no path here that turns
 * a remembered number into a session - no cached token, no cached portfolio id,
 * no "this browser was trusted last time". Every arrival at this screen ends in
 * a real OTP and a fresh Firebase credential, exactly as it did before, because
 * the alternative is a login screen that can be passed by editing localStorage.
 *
 * Most returning users never see this screen at all: the Firebase session is
 * persisted in localStorage (firebase.js) and restored on boot, so they land on
 * the dashboard until they explicitly log out. Logging out clears that session
 * and keeps the number - the security boundary is the credential, not the phone
 * number printed on the form.
 *
 * WHAT THIS COMPONENT DOES NOT DO
 * -------------------------------
 * It does not talk to our backend, and it does not decide what happens after a
 * successful sign-in. Firebase holds the credential; <AuthProvider> notices,
 * exchanges it for a session and renders the dashboard. This file's job ends
 * at `confirm(code)`.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { RecaptchaVerifier, signInWithPhoneNumber } from 'firebase/auth'

import { auth, IS_PHONE_TEST_MODE } from '../firebase'
import BrandMark from '../components/BrandMark'
import ThemeToggle from '../components/ThemeToggle'
import { useAuth } from './auth-context'
import { firebaseErrorMessage } from './firebase-errors'
import { forgetPhone, isE164, readRememberedPhone, rememberPhone } from './remembered-phone'

/** India. The dashboard prices NSE equities in rupees; this is who it is for. */
const DEFAULT_DIAL_CODE = '+91'

/** Firebase phone auth sends six digits. */
const OTP_LENGTH = 6

/*
 * The E.164 check is `isE164`, imported above. It is checked here as well as by
 * Firebase so a typo costs a render rather than a round trip and a burnt
 * reCAPTCHA token - and it lives in remembered-phone.js because that module has
 * to apply the same rule to a value read back out of localStorage. One rule,
 * one definition.
 */

/** Strip everything a person types for readability: spaces, dashes, brackets. */
function normalisePhone(raw) {
  return raw.replace(/[\s()–—-]/g, '')
}

export default function LoginScreen() {
  const { login } = useAuth()

  // 'phone' -> enter the number; 'code' -> enter the six digits.
  const [step, setStep] = useState('phone')

  /*
   * The number this browser last signed in with, if any.
   *
   * Read ONCE, in a lazy initialiser, not on every render: localStorage is
   * synchronous and this is the render path. Held in state as well as seeded
   * into the field because the two answer different questions - `phone` is what
   * is in the box right now (the user may be editing it), `rememberedPhone` is
   * whether there is anything to forget, which is what decides if the "Not you?"
   * link is shown at all.
   */
  const [rememberedPhone, setRememberedPhone] = useState(readRememberedPhone)
  const [phone, setPhone] = useState(() => readRememberedPhone() ?? DEFAULT_DIAL_CODE)
  const [code, setCode] = useState('')
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [isBusy, setIsBusy] = useState(false)

  /*
   * Is the box still showing the number we remembered? Drives both the
   * "welcome back" copy and the "Not you?" link. Derived rather than stored, so
   * it cannot drift out of step with the field the user is typing into - the
   * link disappears the moment they change a digit.
   */
  const isPrefilled = Boolean(rememberedPhone) && phone === rememberedPhone

  // The <div> Firebase renders the invisible widget into.
  const recaptchaRef = useRef(null)
  // The verifier itself, and the pending confirmation returned by
  // signInWithPhoneNumber. Refs, not state: neither is rendered, and putting
  // them in state would re-render the form for no visible reason.
  const verifierRef = useRef(null)
  const confirmationRef = useRef(null)
  const codeInputRef = useRef(null)

  /**
   * Destroy the widget. Safe to call when there is nothing to destroy.
   *
   * This is the teardown, NOT the between-attempts step - that is
   * `resetVerifier`. It runs on unmount, and as the fallback when a reset
   * fails.
   */
  const clearRecaptcha = useCallback(() => {
    try {
      verifierRef.current?.clear()
    } catch {
      // Already cleared, or the container went with an unmount. Nothing to do.
    }
    verifierRef.current = null
  }, [])

  /**
   * Retire the token, keep the widget.
   *
   * grecaptcha caches the response against the widget id, and
   * RecaptchaVerifier.verify() returns that cached value when it finds one -
   * so without this, a second sign-in attempt would re-send the token the
   * first one already burnt and fail as `auth/invalid-app-credential`.
   * reset() drops the cached response; the next verify() executes afresh.
   *
   * `render()` is idempotent - the verifier memoises its widget-id promise -
   * so this is one local call and re-renders nothing.
   *
   * `window.grecaptcha` is absent in development, where the verifier is
   * driving a mock, hence the optional call rather than a bare one. The mock
   * issues its own token per attempt, so there is nothing there to reset.
   *
   * If any of that throws, the widget is in a state we cannot reason about.
   * Destroying it means the next attempt builds a clean one - the old
   * behaviour, still correct, just no longer the every-time path.
   */
  const resetVerifier = useCallback(async () => {
    if (!verifierRef.current) return
    try {
      const widgetId = await verifierRef.current.render()
      window.grecaptcha?.reset(widgetId)
    } catch {
      clearRecaptcha()
    }
  }, [clearRecaptcha])

  // StrictMode's second mount, and every real unmount.
  useEffect(() => clearRecaptcha, [clearRecaptcha])

  // Move the cursor to the code box the moment it appears, so the user can
  // type the SMS straight in without reaching for the mouse.
  useEffect(() => {
    if (step === 'code') codeInputRef.current?.focus()
  }, [step])

  /**
   * The one verifier for this screen: built on first use, reused after.
   *
   * Lazy because the container has to be in the DOM first, and kept because
   * rebuilding it is what invites challenges. Between attempts it is
   * `resetVerifier` that runs, not this.
   */
  function getVerifier() {
    if (verifierRef.current) return verifierRef.current
    verifierRef.current = new RecaptchaVerifier(auth, recaptchaRef.current, {
      size: 'invisible',
    })
    return verifierRef.current
  }

  async function sendCode(event) {
    event?.preventDefault()
    if (isBusy) return

    const number = normalisePhone(phone)
    if (!isE164(number)) {
      setError(
        'Enter the number in international format, starting with the country code - e.g. +91 98765 43210.',
      )
      return
    }

    setIsBusy(true)
    setError(null)
    setNotice(null)

    try {
      confirmationRef.current = await signInWithPhoneNumber(auth, number, getVerifier())
      setPhone(number)
      setCode('')
      setStep('code')
      setNotice(`Code sent to ${number}.`)
    } catch (sendError) {
      setError(firebaseErrorMessage(sendError, 'send'))
    } finally {
      // Success or failure, assume the token was spent - Firebase gives no way
      // to ask. Resetting unconditionally is the rule that has no second case
      // to get wrong, and this is the ONLY place a token is retired, which is
      // what lets `resend` and `changeNumber` below do nothing about it.
      //
      // Awaited, not fired and forgotten: `isBusy` is still true here, so the
      // reset completes before the button comes back and there is no window in
      // which a second click could reuse the token this one just burnt.
      await resetVerifier()
      setIsBusy(false)
    }
  }

  async function verifyCode(event) {
    event?.preventDefault()
    if (isBusy) return

    const digits = code.replace(/\D/g, '')
    if (digits.length !== OTP_LENGTH) {
      setError(`Enter the ${OTP_LENGTH}-digit code from the SMS.`)
      return
    }
    if (!confirmationRef.current) {
      setError('That code has expired. Send a new one.')
      setStep('phone')
      return
    }

    setIsBusy(true)
    setError(null)

    try {
      await confirmationRef.current.confirm(digits)
      // Signed in. Firebase now holds the credential and has already told
      // <AuthProvider>; this only puts the spinner up for the gap before the
      // session lands. This component unmounts a moment later.
      //
      // The number is remembered HERE - after the OTP was accepted, not when it
      // was sent - so a typo that never verified is never prefilled back at the
      // user. It survives log out on purpose; see remembered-phone.js.
      rememberPhone(phone)
      login()
    } catch (verifyError) {
      setError(firebaseErrorMessage(verifyError, 'verify'))
      setIsBusy(false)
    }
    // No `finally`: on success the component is on its way out, and dropping
    // `isBusy` would flash an enabled "Verify" button under the spinner.
  }

  /**
   * Back to step one.
   *
   * The verifier stays. It was reset when the last attempt finished, so it is
   * already clean - and keeping the widget across a change of number is
   * exactly the continuity that stops the next send from looking like a brand
   * new visitor.
   */
  function changeNumber() {
    confirmationRef.current = null
    setStep('phone')
    setCode('')
    setError(null)
    setNotice(null)
  }

  /**
   * "Not you?" - drop the remembered number and empty the field back to the
   * dial code.
   *
   * The escape hatch for a shared browser, and for a developer switching
   * between a Firebase test number and a real one. It clears only the stored
   * NUMBER; there is no session to clear here, because this link is only
   * reachable from the signed-out login screen.
   */
  function useDifferentNumber() {
    forgetPhone()
    setRememberedPhone(null)
    setPhone(DEFAULT_DIAL_CODE)
    setError(null)
    setNotice(null)
  }

  /** A second SMS to the same number. Same reasoning: reuse, do not rebuild. */
  function resend() {
    confirmationRef.current = null
    setCode('')
    setError(null)
    sendCode()
  }

  return (
    <main className="page page--centered">
      <div className="login">
        <div className="login__toolbar">
          <ThemeToggle />
        </div>

        {/*
          The mark is the same component the dashboard header uses, on purpose:
          this is the only screen a signed-out visitor ever sees, so it is where
          the product has to introduce itself as the thing they will land in.
        */}
        <div className="login__brand">
          <BrandMark />
          <div>
            <p className="login__wordmark">Clarisk</p>
            <p className="login__eyebrow">See your risk clearly.</p>
          </div>
        </div>

        <h1 className="login__title">Sign in</h1>
        <p className="login__subtitle">
          {step === 'code'
            ? `Enter the ${OTP_LENGTH}-digit code sent to ${phone}.`
            : isPrefilled
              ? 'Welcome back. Send a fresh code to the number below to sign in again.'
              : 'We will text you a six-digit code. Your portfolio is tied to this number.'}
        </p>

        {/*
          Development only, and it earns its space: with app verification off,
          a real phone number fails in a way that looks like a broken login
          rather than a deliberate setting. Saying so here is cheaper than the
          ten minutes it otherwise costs. `IS_PHONE_TEST_MODE` is a build-time
          constant, so this whole branch is dropped from a production bundle.
        */}
        {IS_PHONE_TEST_MODE && (
          <p className="banner banner--warn login__banner" role="status">
            <strong>Development mode.</strong> App verification is off, so there is no
            reCAPTCHA at all — but only a Firebase <em>test</em> number will sign in,
            with its fixed code. No SMS is sent.
          </p>
        )}

        {step === 'phone' ? (
          <form className="login__form" onSubmit={sendCode} noValidate>
            <label className="login__field">
              <span className="login__label">Phone number</span>
              <input
                type="tel"
                name="phone"
                autoComplete="tel"
                inputMode="tel"
                className="login__input"
                value={phone}
                onChange={(changeEvent) => setPhone(changeEvent.target.value)}
                placeholder="+91 98765 43210"
                aria-invalid={error ? 'true' : undefined}
                disabled={isBusy}
              />
            </label>

            <button type="submit" className="button login__submit" disabled={isBusy}>
              {isBusy && <span className="spinner spinner--inline" aria-hidden="true" />}
              {isBusy ? 'Sending…' : 'Send OTP'}
            </button>

            {/*
              Only while the box still holds the remembered number. The moment
              the user edits the field they are already using a different
              number, and a link offering to do what they are doing is noise.
            */}
            {isPrefilled && (
              <div className="login__links">
                <button
                  type="button"
                  className="login__link"
                  onClick={useDifferentNumber}
                  disabled={isBusy}
                >
                  Not you? Use a different number
                </button>
              </div>
            )}
          </form>
        ) : (
          <form className="login__form" onSubmit={verifyCode} noValidate>
            <label className="login__field">
              <span className="login__label">Verification code</span>
              <input
                ref={codeInputRef}
                type="text"
                name="otp"
                // The two attributes that make a phone offer the SMS code as a
                // one-tap autofill. Worth more than any amount of styling here.
                autoComplete="one-time-code"
                inputMode="numeric"
                className="login__input login__input--code"
                value={code}
                onChange={(changeEvent) =>
                  setCode(changeEvent.target.value.replace(/\D/g, '').slice(0, OTP_LENGTH))
                }
                placeholder="123456"
                maxLength={OTP_LENGTH}
                aria-invalid={error ? 'true' : undefined}
                disabled={isBusy}
              />
            </label>

            <button type="submit" className="button login__submit" disabled={isBusy}>
              {isBusy && <span className="spinner spinner--inline" aria-hidden="true" />}
              {isBusy ? 'Verifying…' : 'Verify'}
            </button>

            <div className="login__links">
              <button
                type="button"
                className="login__link"
                onClick={changeNumber}
                disabled={isBusy}
              >
                Change number
              </button>
              <span className="login__link-sep" aria-hidden="true" />
              <button type="button" className="login__link" onClick={resend} disabled={isBusy}>
                Resend code
              </button>
            </div>
          </form>
        )}

        {error && (
          <p className="banner banner--error login__banner" role="alert">
            {error}
          </p>
        )}
        {!error && notice && (
          <p className="banner banner--good login__banner" role="status">
            {notice}
          </p>
        )}

        {/*
          Firebase renders the invisible reCAPTCHA widget in here. It must exist
          in the DOM before getVerifier() runs and must NOT be conditionally
          rendered - unmounting it between steps would take the widget with it.
        */}
        <div ref={recaptchaRef} className="login__recaptcha" />

        <p className="login__foot">
          {IS_PHONE_TEST_MODE
            ? 'reCAPTCHA is mocked in development. No SMS is sent to test numbers.'
            : 'Protected by reCAPTCHA. Standard SMS charges may apply.'}
        </p>
      </div>
    </main>
  )
}
