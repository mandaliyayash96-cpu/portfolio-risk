/**
 * Phone sign-in: a number, an SMS, six digits.
 *
 * THE reCAPTCHA IS THE PART THAT GOES WRONG
 * -----------------------------------------
 * Firebase requires an app-verification token before it will send an SMS, and
 * for the web that means reCAPTCHA. "Invisible" means the user never sees a
 * puzzle - not that there is nothing there. Three rules follow from how it
 * actually behaves, and every one of them is a bug that only appears on the
 * SECOND attempt:
 *
 *   1. A reCAPTCHA token is SINGLE USE. Once signInWithPhoneNumber has
 *      consumed it - whether it succeeded or failed - the verifier is spent.
 *      So the verifier is cleared after every attempt and rebuilt lazily on
 *      the next one. Reusing it is why phone screens "work once, then the
 *      button does nothing".
 *   2. The container element must be in the DOM when the verifier is
 *      constructed, which is why it is built on the first click (against a
 *      ref) rather than at module load.
 *   3. StrictMode mounts every component twice in development. Without the
 *      cleanup below, the second mount would leave an orphaned widget behind
 *      and Firebase would complain about a container that already has one.
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

import { auth } from '../firebase'
import ThemeToggle from '../components/ThemeToggle'
import { useAuth } from './auth-context'
import { firebaseErrorMessage } from './firebase-errors'

/** India. The dashboard prices NSE equities in rupees; this is who it is for. */
const DEFAULT_DIAL_CODE = '+91'

/** Firebase phone auth sends six digits. */
const OTP_LENGTH = 6

/**
 * E.164, which is the only form Firebase accepts: a +, a country code that
 * cannot start with 0, then up to fourteen more digits.
 *
 * Checked here as well as by Firebase so a typo costs a render rather than a
 * round trip and a burnt reCAPTCHA token.
 */
const E164 = /^\+[1-9]\d{7,14}$/

/** Strip everything a person types for readability: spaces, dashes, brackets. */
function normalisePhone(raw) {
  return raw.replace(/[\s()–—-]/g, '')
}

export default function LoginScreen() {
  const { login } = useAuth()

  // 'phone' -> enter the number; 'code' -> enter the six digits.
  const [step, setStep] = useState('phone')
  const [phone, setPhone] = useState(DEFAULT_DIAL_CODE)
  const [code, setCode] = useState('')
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [isBusy, setIsBusy] = useState(false)

  // The <div> Firebase renders the invisible widget into.
  const recaptchaRef = useRef(null)
  // The verifier itself, and the pending confirmation returned by
  // signInWithPhoneNumber. Refs, not state: neither is rendered, and putting
  // them in state would re-render the form for no visible reason.
  const verifierRef = useRef(null)
  const confirmationRef = useRef(null)
  const codeInputRef = useRef(null)

  /** Tear the widget down. Safe to call when there is nothing to tear down. */
  const clearRecaptcha = useCallback(() => {
    try {
      verifierRef.current?.clear()
    } catch {
      // Already cleared, or the container went with an unmount. Nothing to do.
    }
    verifierRef.current = null
  }, [])

  // StrictMode's second mount, and every real unmount.
  useEffect(() => clearRecaptcha, [clearRecaptcha])

  // Move the cursor to the code box the moment it appears, so the user can
  // type the SMS straight in without reaching for the mouse.
  useEffect(() => {
    if (step === 'code') codeInputRef.current?.focus()
  }, [step])

  /** Build the verifier if we do not have a live one. */
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
    if (!E164.test(number)) {
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
      // to ask. Clearing unconditionally is the rule that has no second case to
      // get wrong, and rebuilding an INVISIBLE widget costs the user nothing:
      // there is no challenge to solve, only a new widget to render.
      clearRecaptcha()
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
      login()
    } catch (verifyError) {
      setError(firebaseErrorMessage(verifyError, 'verify'))
      setIsBusy(false)
    }
    // No `finally`: on success the component is on its way out, and dropping
    // `isBusy` would flash an enabled "Verify" button under the spinner.
  }

  /** Back to step one. The spent verifier goes with it. */
  function changeNumber() {
    confirmationRef.current = null
    clearRecaptcha()
    setStep('phone')
    setCode('')
    setError(null)
    setNotice(null)
  }

  /** Send a second SMS to the same number. */
  function resend() {
    confirmationRef.current = null
    clearRecaptcha()
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

        <p className="login__eyebrow">Investor Portfolio Monitoring &amp; Risk Management</p>
        <h1 className="login__title">Sign in</h1>
        <p className="login__subtitle">
          {step === 'phone'
            ? 'We will text you a six-digit code. Your portfolio is tied to this number.'
            : `Enter the ${OTP_LENGTH}-digit code sent to ${phone}.`}
        </p>

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
          Protected by reCAPTCHA. Standard SMS charges may apply.
        </p>
      </div>
    </main>
  )
}
