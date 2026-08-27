/**
 * The alerts half of the API surface.
 *
 * Split from client.js because alerts are the one feature that talks over two
 * protocols: rules and acknowledgements are ordinary REST, but the event FEED
 * is a WebSocket. Both addresses are derived from the single API_BASE_URL in
 * client.js, so moving the backend to another host or port is still a one-line
 * change.
 *
 * There is deliberately no `listEvents`. The socket sends every open event as
 * its connect snapshot, so a REST list would be a second source of truth for
 * the same data - and the one that goes stale.
 */

import { getEnveloped, postEnveloped, websocketUrl } from './client'

/**
 * The metrics a rule can watch, mirroring alerts.evaluator.METRICS.
 *
 * `unit` drives the form's suffix and the feed's formatting. The backend is the
 * authority on both - it sends `metric_label` with every rule and event, which
 * is what actually gets rendered - but the dropdown has to be populated before
 * any rule exists, so the choices are listed here too.
 *
 * `hint` is the part worth getting right in the UI: thresholds are in whole
 * percent for the percent metrics, and the sign convention on drawdown catches
 * everybody once.
 */
export const METRICS = [
  {
    value: 'var_historical',
    label: 'VaR (historical)',
    unit: '%',
    hint: 'Positive: 2 means a 2% one-day loss at 95% confidence.',
  },
  {
    value: 'max_drawdown',
    label: 'Max drawdown',
    unit: '%',
    hint: 'Negative: -15 means a 15% fall from the peak. Use "<" to catch worse.',
  },
  {
    value: 'annualized_volatility',
    label: 'Volatility (annualised)',
    unit: '%',
    hint: 'Positive: 25 means 25% annualised.',
  },
  {
    value: 'concentration',
    label: 'Largest holding weight',
    unit: '%',
    hint: 'Positive: 40 means one position is 40% of the portfolio.',
  },
  {
    value: 'hhi',
    label: 'Concentration (HHI)',
    unit: '',
    hint: 'Unitless, 0 to 1. Equal weights across n holdings gives 1/n.',
  },
  {
    value: 'beta',
    label: 'Beta vs benchmark',
    unit: '',
    hint: 'Unitless. 1.0 moves with the benchmark. Needs benchmark history stored.',
  },
]

/** The comparisons, mirroring alerts.models.AlertOperator. */
export const OPERATORS = [
  { value: 'gt', label: '>' },
  { value: 'gte', label: '>=' },
  { value: 'lt', label: '<' },
  { value: 'lte', label: '<=' },
]

/** Look up a metric's display info; falls back gracefully for an unknown one. */
export function metricInfo(metric) {
  return METRICS.find((entry) => entry.value === metric) ?? { value: metric, label: metric, unit: '' }
}

/** Every rule configured on a portfolio, active or not. */
export function listRules(portfolioId) {
  return getEnveloped(`/api/alerts/rules/${portfolioId}/`)
}

/**
 * Configure a new rule.
 *
 * @param {number} portfolioId
 * @param {{metric: string, operator: string, threshold: string}} rule
 *   `threshold` is sent as a string: it goes into a Decimal column, and a
 *   JavaScript number would round-trip through float on the way.
 */
export function createRule(portfolioId, { metric, operator, threshold }) {
  return postEnveloped(`/api/alerts/rules/${portfolioId}/`, { metric, operator, threshold })
}

/**
 * Mark one event read.
 *
 * Also re-arms the rule server-side: the scan suppresses a breach only while an
 * unacknowledged event stands against it, so acknowledging is how you ask to be
 * told again if the breach is still true.
 */
export function acknowledgeEvent(eventId) {
  return postEnveloped(`/api/alerts/events/${eventId}/ack/`)
}

/** The live feed's address: ws://<host>/ws/alerts/<id>/ */
export function alertsSocketUrl(portfolioId) {
  return websocketUrl(`/ws/alerts/${portfolioId}/`)
}

/**
 * Close codes the consumer uses to say "reconnecting will not help".
 *
 * The server accepts the socket before closing with these precisely so they
 * survive to the client - a pre-accept rejection arrives as 1006, which is
 * indistinguishable from the backend being down.
 */
export const CLOSE_BAD_PORTFOLIO_ID = 4400
export const CLOSE_NO_SUCH_PORTFOLIO = 4404

/** True when a close code means the panel should stop retrying. */
export function isFatalCloseCode(code) {
  return code === CLOSE_BAD_PORTFOLIO_ID || code === CLOSE_NO_SUCH_PORTFOLIO
}
