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
