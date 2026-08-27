/**
 * The only place that knows the API's address and its envelope shape.
 *
 * Every backend response - success or failure - is
 * {success, data, error:{code, message, details}}. Components should never see
 * that wrapper: this module unwraps `data` on success and raises an ApiError
 * carrying the backend's own message on failure, so the UI has exactly one
 * thing to render when something goes wrong.
 */

import axios from 'axios'

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
async function request(method, path, body) {
  let response
  try {
    response = await api.request({ method, url: path, data: body })
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
export function postEnveloped(path, body = {}) {
  return request('post', path, body)
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
