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
    `Cannot reach the API at ${API_BASE_URL}. Start it with \`python manage.py runserver\` in the backend folder, then retry.`,
    { code: 'network_error' },
  )
}

/**
 * GET one enveloped endpoint and hand back its `data`.
 *
 * @throws {ApiError} with the backend's message for a business failure
 *   (not_found / empty_portfolio / missing_price_data / insufficient_history /
 *   optimization_failed), or a fix-it message when the API was unreachable.
 */
async function getEnveloped(path) {
  let response
  try {
    response = await api.get(path)
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
