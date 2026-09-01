/**
 * The only place that knows the API's address and its envelope shape.
 *
 * Every backend response - success or failure - is
 * {success, data, error:{code, message, details}}. Components should never see
 * that wrapper: this module unwraps `data` on success and raises an ApiError
 * carrying the backend's own message on failure, so the UI has exactly one
 * thing to render when something goes wrong.
 *
 * It is also the only place that knows requests are AUTHENTICATED. The two
 * interceptors below attach the signed-in user's Firebase ID token and handle
 * it expiring, so not one caller in the app - and not one component - has to
 * think about tokens. A signed-out visitor sends no header at all, and the
 * backend answers exactly as it did before phone auth existed.
 */

import axios from 'axios'

import { auth } from '../firebase'

// TODO Phase 8 (prod): read from import.meta.env.VITE_API_BASE_URL so the
// deployed build can point at the real host instead of a dev port.
export const API_BASE_URL = 'http://127.0.0.1:8000'

/**
 * The WebSocket address for a path on the same backend.
 *
 * Derived from API_BASE_URL rather than written out again: the alert feed and
 * the REST API are served by one daphne process, so a second hardcoded host is
 * a second thing to forget when the port moves. http -> ws, https -> wss.
 */
export function websocketUrl(path) {
  const base = API_BASE_URL.replace(/^http/, 'ws').replace(/\/$/, '')
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000, // the Monte Carlo leg of the report is not instant
  headers: { Accept: 'application/json' },
})

/* ---------------------------------------------------------------------------
   Authentication

   Two interceptors, and between them the rest of this file - and every
   component above it - never mentions a token.
   --------------------------------------------------------------------------- */

/**
 * Attach the signed-in user's ID token to every request.
 *
 * `getIdToken()` is the important call here, rather than a token held in a
 * variable somewhere: Firebase caches the current token and silently mints a
 * fresh one when it is within five minutes of expiring, so a tab left open
 * over lunch keeps working with no refresh logic of our own.
 *
 * No user means no header. That is what keeps the dashboard's anonymous
 * behaviour identical to before auth existed, and it is why this cannot be
 * written as "throw if not signed in".
 */
api.interceptors.request.use(async (config) => {
  const user = auth.currentUser
  if (!user) return config

  try {
    config.headers.Authorization = `Bearer ${await user.getIdToken()}`
  } catch {
    // The refresh failed - revoked account, or offline. Sending the request
    // unauthenticated gets a clean 401 the UI already knows how to show;
    // throwing here would surface as an unexplained network error instead.
  }
  return config
})

/**
 * One retry with a force-refreshed token when the backend says 401.
 *
 * The request interceptor above handles ordinary expiry, so reaching here means
 * the two sides disagree: a token Firebase still considered valid was rejected
 * server-side (clock skew, or a token revoked mid-session). `getIdToken(true)`
 * bypasses the cache and asks Google for a new one.
 *
 * The `_retriedWithFreshToken` flag is not optional. Without it a genuinely
 * unauthorised call - a revoked user, a disabled account - retries forever,
 * each attempt minting a token and each being refused.
 *
 * Status only, never the envelope's error code: this instance also fetches the
 * PDF with `responseType: 'blob'`, and on that path `error.response.data` is a
 * Blob whose `.error.code` is undefined. A 401 is a 401 in both shapes.
 */
api.interceptors.response.use(undefined, async (error) => {
  const original = error.config
  if (error.response?.status !== 401 || !original || original._retriedWithFreshToken) {
    throw error
  }
  const user = auth.currentUser
  if (!user) throw error

  original._retriedWithFreshToken = true
  try {
    original.headers.Authorization = `Bearer ${await user.getIdToken(true)}`
  } catch {
    throw error
  }
  return api.request(original)
})

/**
 * The ceiling for the two calls that WRITE holdings.
 *
 * Adding a ticker the backend has never seen makes it fetch that symbol from
 * yfinance before replying - up to two upstream calls at a 20s timeout each,
 * and a CSV of new symbols multiplies that. Thirty seconds is right for a
 * report that is only ever computed from stored data; it would abort a
 * perfectly healthy import halfway through. The request keeps running on the
 * server either way, so a timeout here is a lie about what happened, not a
 * cancellation - hence the higher number rather than a nicer error.
 */
const WRITE_TIMEOUT_MS = 120_000

/** An error we can show the user verbatim, with the backend's code attached. */
export class ApiError extends Error {
  constructor(message, { code = 'error', details = null, status = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.details = details
    this.status = status
  }
}

/**
 * Axios rejected before any envelope existed: the server is down, the request
 * timed out, or the browser blocked the response for CORS. These are the
 * failures a demo actually hits, so each gets its own fix-it sentence.
 */
function transportError(error) {
  if (error.code === 'ECONNABORTED') {
    return new ApiError(
      `The API at ${API_BASE_URL} took too long to respond. It may still be computing the report - try again in a moment.`,
      { code: 'timeout' },
    )
  }
  return new ApiError(
    `Cannot reach the API at ${API_BASE_URL}. Start it with \`daphne config.asgi:application\` in the backend folder, then retry.`,
    { code: 'network_error' },
  )
}

/**
 * Call one enveloped endpoint and hand back its `data`.
 *
 * Method-aware rather than GET-only because Phase 6 writes: creating an alert
 * rule and acknowledging an event are POSTs through the same envelope, and the
 * unwrapping and error translation below should not exist twice.
 *
 * @throws {ApiError} with the backend's message for a business failure
 *   (not_found / invalid_input / empty_portfolio / missing_price_data /
 *   insufficient_history / optimization_failed), or a fix-it message when the
 *   API was unreachable.
 */
async function request(method, path, body, config = {}) {
  let response
  try {
    response = await api.request({ method, url: path, data: body, ...config })
  } catch (error) {
    // A 4xx/5xx still carries a perfectly good envelope - prefer its message.
    const envelope = error.response?.data
    if (envelope?.error?.message) {
      throw new ApiError(envelope.error.message, {
        code: envelope.error.code,
        details: envelope.error.details,
        status: error.response.status,
      })
    }
    throw transportError(error)
  }

  const envelope = response.data
  // A 200 that says success:false should never happen, but rendering `undefined`
  // across eight cards is a worse way to find out than saying so.
  if (!envelope?.success) {
    throw new ApiError(envelope?.error?.message ?? 'The API returned an unrecognised response.', {
      code: envelope?.error?.code,
      details: envelope?.error?.details,
      status: response.status,
    })
  }
  return envelope.data
}

/** GET an enveloped endpoint. */
export function getEnveloped(path) {
  return request('get', path)
}

/** POST to an enveloped endpoint. Body defaults to {} - DRF wants valid JSON. */
export function postEnveloped(path, body = {}, config) {
  return request('post', path, body, config)
}

/** DELETE an enveloped endpoint. */
export function deleteEnveloped(path, config) {
  return request('delete', path, undefined, config)
}

/* ---------------------------------------------------------------------------
   Identity

   The phone number is never sent. Both calls are authenticated by the Bearer
   token alone, and the backend reads the number out of the VERIFIED token -
   which is the whole security property of the sign-in flow. Sending a phone
   number here would be sending something the server is right to ignore.
   --------------------------------------------------------------------------- */

/**
 * Establish (or re-establish) the backend session for the signed-in user.
 *
 * Called the moment Firebase confirms the OTP, and again on every boot with a
 * persisted user. Creates the account and its portfolio on a first login;
 * returns the same ids on every call after that.
 *
 * @returns {Promise<{user_id: number, phone: string, portfolio_id: number,
 *   portfolio_name: string, base_currency: string, first_login: boolean}>}
 */
export function startSession() {
  return postEnveloped('/api/auth/session/')
}

/** Who the current token belongs to, and which portfolio is theirs. A pure read. */
export function getMe() {
  return getEnveloped('/api/auth/me/')
}

/** The full risk report for a portfolio. */
export function getRiskReport(portfolioId) {
  return getEnveloped(`/api/risk/${portfolioId}/`)
}

/**
 * The Markowitz rebalance suggestion for the same portfolio: current weights
 * and volatility, the minimum-variance and maximum-Sharpe allocations, and the
 * efficient frontier they sit on.
 */
export function getRebalance(portfolioId) {
  return getEnveloped(`/api/rebalance/${portfolioId}/`)
}

/**
 * The same window as the risk report, with its time axis kept: a value curve
 * rebased to 100, the drawdown at every date on it, and the peak, current
 * value and max drawdown for reference.
 *
 * `dates`, `equity_curve` and `drawdown_series` are parallel arrays of equal
 * length - zip them by index. `drawdown_series` is in PERCENT (-12.34) while
 * `max_drawdown` is the fraction (-0.1234) the risk report uses, so the axis
 * and the headline figure each get the units they want.
 */
export function getPerformance(portfolioId) {
  return getEnveloped(`/api/performance/${portfolioId}/`)
}

/* ---------------------------------------------------------------------------
   Holdings entry
   ---------------------------------------------------------------------------
   The write half of the API, and the only calls on this page that can change
   what the risk report says. All three are slower than they look: adding a
   ticker the backend has no prices for makes it fetch that symbol from
   yfinance before it answers, which is why they carry WRITE_TIMEOUT_MS and why
   the UI shows a "fetching prices" state rather than a plain spinner.
   --------------------------------------------------------------------------- */

/** The rows behind the Holdings table, each with the `id` a delete needs. */
export function listHoldings(portfolioId) {
  return getEnveloped(`/api/portfolio/${portfolioId}/holdings/`)
}

/**
 * Add one position, or replace the one already held in that ticker.
 *
 * `body` is {ticker, quantity, avg_buy_price} plus optional
 * {buy_date, asset_type, sector}. Numbers are sent as the STRINGS the user
 * typed: the backend stores them as exact Decimals, and routing them through
 * JavaScript's Number on the way would put a float in the middle of a value
 * that is about to be stored to four decimal places.
 *
 * Resolves with the saved holding plus `created` (false means it replaced one)
 * and `warning` - non-null when the row saved but its prices could not be
 * fetched, which is what a typo'd symbol looks like.
 *
 * @throws {ApiError} code `invalid_input` with a message naming the bad field.
 */
export function addHolding(portfolioId, body) {
  return postEnveloped(`/api/portfolio/${portfolioId}/holdings/`, body, {
    timeout: WRITE_TIMEOUT_MS,
  })
}

/**
 * Bulk-load holdings from a CSV file.
 *
 * Resolves with a REPORT, not a yes/no: {total_rows, added, updated, skipped,
 * results[], price_fetch}. A file where some rows failed still resolves - those
 * rows come back as `skipped` with a reason, and the caller is expected to show
 * them. Only a file no row survives (wrong headers, too big, not a CSV) rejects.
 *
 * The Content-Type header is left unset deliberately: the browser has to write
 * it itself so it can append the multipart boundary, and setting it by hand is
 * the classic way to get an upload the server cannot parse.
 */
export function importHoldingsCsv(portfolioId, file) {
  const form = new FormData()
  form.append('file', file)
  return postEnveloped(`/api/portfolio/${portfolioId}/holdings/import/`, form, {
    timeout: WRITE_TIMEOUT_MS,
  })
}

/** Remove one position. Scoped to the portfolio, so a stale id 404s. */
export function deleteHolding(portfolioId, holdingId) {
  return deleteEnveloped(`/api/portfolio/${portfolioId}/holdings/${holdingId}/`)
}

/**
 * The columns a CSV must have, and one filled-in example row.
 *
 * Kept beside the calls above rather than inside the component because it is a
 * fact about the API's contract, not about the form's layout - if the backend's
 * REQUIRED_COLUMNS ever change, this is the line that has to change with them.
 */
export const CSV_REQUIRED_COLUMNS = ['ticker', 'quantity', 'avg_buy_price']
export const CSV_OPTIONAL_COLUMNS = ['buy_date', 'asset_type', 'sector']
export const CSV_TEMPLATE = [
  [...CSV_REQUIRED_COLUMNS, ...CSV_OPTIONAL_COLUMNS].join(','),
  'RELIANCE.NS,10,1400.50,2026-01-05,EQUITY,Energy',
  'TCS.NS,5,3200.00,2026-01-06,EQUITY,IT',
  '',
].join('\n')

/* ---------------------------------------------------------------------------
   The risk report as a document
   --------------------------------------------------------------------------- */

/**
 * Fetch the PDF of a portfolio's risk report.
 *
 * Resolves with `{blob, filename}` — the file, and the name the server asked
 * for it to be saved under.
 *
 * WHY THIS DOES NOT GO THROUGH `request()`
 * ----------------------------------------
 * Every other call in this module unwraps an envelope. This one must not: on
 * success the body is a binary PDF with no envelope anywhere in it, so the
 * shared helper's `envelope.data` would be meaningless.
 *
 * THE ERROR CASE IS THE INTERESTING HALF
 * --------------------------------------
 * With `responseType: 'blob'`, axios gives back a Blob for EVERY response —
 * including a 404 whose body is a perfectly good JSON envelope. Reading
 * `error.response.data.error.message` on that yields undefined, and the classic
 * bug from here is to save the failure to disk as a 200-byte `report.pdf`. So a
 * failed response is read back as text and parsed before its message is used.
 *
 * @throws {ApiError} carrying the backend's own message and code.
 */
export async function downloadRiskReportPdf(portfolioId) {
  let response
  try {
    response = await api.request({
      method: 'get',
      url: `/api/risk/${portfolioId}/report.pdf`,
      responseType: 'blob',
      // The report is recomputed per request and the Monte Carlo leg is not
      // instant, so this gets the write-path budget rather than the 30s one.
      timeout: WRITE_TIMEOUT_MS,
    })
  } catch (error) {
    throw await blobError(error)
  }

  // A 200 whose body is JSON should not happen — but rendering a JSON envelope
  // as a PDF in a viewer is a uniquely confusing way to find out that it did.
  const type = response.headers['content-type'] ?? ''
  if (!type.includes('application/pdf')) {
    const envelope = await readEnvelope(response.data)
    throw new ApiError(
      envelope?.error?.message ?? 'The server did not return a PDF.',
      { code: envelope?.error?.code ?? 'unexpected_response', status: response.status },
    )
  }

  return {
    blob: response.data,
    filename: filenameFrom(response.headers['content-disposition'], portfolioId),
  }
}

/** Read a Blob body back as a parsed envelope, or null if it was not JSON. */
async function readEnvelope(blob) {
  try {
    return JSON.parse(await blob.text())
  } catch {
    return null
  }
}

/** Translate a failed blob request into an ApiError carrying the real message. */
async function blobError(error) {
  const body = error.response?.data
  if (body instanceof Blob) {
    const envelope = await readEnvelope(body)
    if (envelope?.error?.message) {
      return new ApiError(envelope.error.message, {
        code: envelope.error.code,
        details: envelope.error.details,
        status: error.response.status,
      })
    }
  }
  return transportError(error)
}

/**
 * Pull the filename out of `Content-Disposition`, falling back to something
 * sensible. The server already slugifies it, so this only has to unwrap the
 * quotes rather than sanitise anything.
 */
function filenameFrom(disposition, portfolioId) {
  const match = /filename="?([^";]+)"?/i.exec(disposition ?? '')
  return match?.[1] ?? `risk-report-${portfolioId}.pdf`
}

/**
 * Hand a fetched blob to the browser as a download.
 *
 * The anchor is created, clicked and removed synchronously; the object URL is
 * revoked on the next tick rather than immediately, because revoking it in the
 * same frame as the click cancels the download in some browsers.
 */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
