/**
 * The Firebase app, and the `auth` handle every other module shares.
 *
 * WHY THE CONFIG BELOW IS NOT A SECRET
 * ------------------------------------
 * `apiKey` reads like a credential and is not one. It identifies the project to
 * Google's servers; it authorises nothing on its own. Firebase ships it in the
 * client bundle by design, and it is safe there for exactly two reasons, both
 * of which this app relies on:
 *
 *   1. Phone sign-in still requires a real OTP delivered to a real handset, and
 *      the domain making the request has to be on the project's authorised
 *      domains list.
 *   2. Nothing on our backend trusts the browser's word about who it is. The
 *      ID token minted here is verified server-side against Google's public
 *      certificates (backend/accounts/firebase.py) before a single row is read.
 *
 * The thing that WOULD be a secret is the service-account JSON, and that lives
 * only in backend/.env. Nothing in this folder can reach it.
 *
 * INITIALISED ONCE, AT IMPORT
 * ---------------------------
 * Module scope, not a hook or a provider: `initializeApp` is idempotent per
 * page load and Firebase throws on a duplicate default app. Importing this
 * module from anywhere gets the same instance.
 */

import { initializeApp } from 'firebase/app'
import { browserLocalPersistence, getAuth, setPersistence } from 'firebase/auth'

const firebaseConfig = {
  apiKey: 'AIzaSyCIi-3189pksRjImiHKuSf36wygmlnGtgI',
  authDomain: 'fixseva-cf2e9.firebaseapp.com',
  projectId: 'fixseva-cf2e9',
  storageBucket: 'fixseva-cf2e9.firebasestorage.app',
  messagingSenderId: '1056660944954',
  appId: '1:1056660944954:web:46e52e497265bad4b7dd03',
  measurementId: 'G-ESR0QM51VB',
}

export const app = initializeApp(firebaseConfig)

/** The one auth instance. Import this, never call getAuth() again elsewhere. */
export const auth = getAuth(app)

/**
 * DEVELOPMENT ONLY: turn Firebase's app verification off, so a TEST phone
 * number signs in with no reCAPTCHA at all.
 *
 * WHAT THIS FIXES
 * ---------------
 * "Invisible" reCAPTCHA does not mean "no reCAPTCHA". It means Google decides,
 * per attempt, whether to escalate - and a developer hammering the same login
 * form from the same browser is exactly the traffic shape its risk model was
 * built to distrust. So the invisible widget starts serving the photo grid
 * ("select all the buses"), several rounds of it, on a form whose only job in
 * development is to get past itself. Nothing is misconfigured when that
 * happens; the widget is working as designed and the design is wrong for a
 * dev loop.
 *
 * `appVerificationDisabledForTesting` is Firebase's own answer to that. With
 * it set, the SDK swaps its reCAPTCHA loader for a mock: no script from
 * Google, no widget, no challenge, and a fake token that the backend accepts
 * for TEST NUMBERS ONLY - the ones registered under Authentication →
 * Sign-in method → Phone → "Phone numbers for testing", which sign in against
 * their fixed six-digit code and never send an SMS.
 *
 * IT HAS TO BE SET HERE, NOT IN THE LOGIN SCREEN
 * ----------------------------------------------
 * `RecaptchaVerifier` reads this flag in its CONSTRUCTOR to choose between the
 * real loader and the mock one. Set it after a verifier exists and that
 * verifier is already the real thing - it will keep loading Google's script
 * and keep showing challenges. Module scope is what guarantees the ordering:
 * every module that can build a verifier imports `auth` from this file, so
 * this has already run by the time one can.
 *
 * WHY IT CANNOT REACH PRODUCTION
 * ------------------------------
 * Three independent guards, because a real user hitting this flag cannot sign
 * in at all - their real number would be sent with a mock token and rejected:
 *
 *   1. `import.meta.env.DEV`, which Vite REPLACES with the literal `false` in
 *      a production build. The condition then folds to `false` at build time
 *      and the assignment below is dropped from the bundle entirely. This is
 *      the guard that actually matters; the other two are for the dev machine.
 *   2. The host has to be a loopback name, so `npm run dev` exposed on a LAN
 *      address does not quietly get it.
 *   3. `VITE_FIREBASE_TEST_MODE`, which overrides both ways - set it to
 *      `false` in frontend/.env.local to test a REAL number and a real SMS on
 *      localhost, or to `true` to force test mode on some other dev host.
 */
const TEST_MODE_FLAG = import.meta.env.VITE_FIREBASE_TEST_MODE

/**
 * `localhost` only, and deliberately not `127.0.0.1`: Firebase authorises the
 * name, not the address, so sign-in fails there with `auth/unauthorized-domain`
 * long before app verification is reached. See RUN.md.
 */
const LOOPBACK_HOSTS = new Set(['localhost', '::1'])

export const IS_PHONE_TEST_MODE =
  import.meta.env.DEV &&
  TEST_MODE_FLAG !== 'false' &&
  (TEST_MODE_FLAG === 'true' || LOOPBACK_HOSTS.has(window.location.hostname))

if (IS_PHONE_TEST_MODE) {
  auth.settings.appVerificationDisabledForTesting = true
  // Loud on purpose. The one thing worse than a reCAPTCHA challenge in
  // development is wondering for ten minutes why a real number will not sign
  // in, because the mock token is silently being rejected.
  console.info(
    '[auth] Development: app verification is DISABLED. reCAPTCHA is mocked, so ' +
      'only Firebase TEST phone numbers can sign in (console → Authentication → ' +
      'Sign-in method → Phone → Phone numbers for testing). Set ' +
      'VITE_FIREBASE_TEST_MODE=false in frontend/.env.local to use a real number.',
  )
}

/**
 * Survive a refresh.
 *
 * `browserLocalPersistence` (localStorage) is already the web default, but it
 * is set explicitly because it is the single line that decides whether a user
 * who reloads the page lands on the dashboard or on the login screen - and a
 * default that is never named is a default nobody knows they depend on.
 *
 * Fired and not awaited. The call resolves before any sign-in can complete (a
 * user has to type a phone number first), and awaiting it at module scope would
 * mean a top-level await in the critical path of every page load. The rejection
 * path is real though: Safari in private mode, or a browser with site data
 * blocked, refuses storage - in which case the session simply lasts until the
 * tab closes, which is a worse experience but a working one.
 */
setPersistence(auth, browserLocalPersistence).catch((error) => {
  console.warn(
    'Firebase could not use local persistence; sign-in will not survive a refresh.',
    error?.code ?? error,
  )
})
