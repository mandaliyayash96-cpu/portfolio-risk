/**
 * The live alert feed, and the form for adding rules to it.
 *
 * Self-contained by design. It owns its socket, its rules, its errors and its
 * reconnect timer, and it is mounted as a leaf in App.jsx with no shared state -
 * so a WebSocket that never connects, a Redis that is down, or a rules endpoint
 * that 500s costs you this panel and nothing else on the dashboard.
 *
 * THE SOCKET IS THE SOURCE OF TRUTH FOR EVENTS
 * --------------------------------------------
 * On connect the server sends a `snapshot` of every open event, then streams
 * `alert` messages as they fire. So the feed is never fetched over REST: a
 * reconnect after a dropped connection re-syncs by replacing the list with the
 * fresh snapshot, which is correct whatever the tab missed while it was away.
 * Rules are the opposite - ordinary REST, loaded once and updated on create.
 *
 * RECONNECT
 * ---------
 * Exponential backoff from 1s to 30s, reset on a successful open. A demo laptop
 * sleeps, a daphne restarts, and neither should need a page refresh. Two things
 * stop the retry loop for good: an unmount, and a close code in the 4xxx range
 * that means the portfolio does not exist - retrying that forever would hammer
 * the server with a request that cannot ever succeed.
 *
 * The refs below are not premature optimisation. `socketRef` and `timerRef` have
 * to be reachable from the cleanup function, and `attemptRef` has to survive
 * re-renders without triggering one - a backoff counter in state would re-run
 * the effect that owns the socket, which would close the socket, which is the
 * classic way to build a reconnect loop that never stays connected.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  METRICS,
  OPERATORS,
  acknowledgeEvent,
  alertsSocketUrl,
  createRule,
  isFatalCloseCode,
  listRules,
  metricInfo,
} from '../api/alerts'

const INITIAL_BACKOFF_MS = 1_000
const MAX_BACKOFF_MS = 30_000

/** Connection states, in the order a socket moves through them. */
const CONNECTING = 'connecting'
const CONNECTED = 'connected'
const RECONNECTING = 'reconnecting'
const FAILED = 'failed'

const STATUS_LABEL = {
  [CONNECTING]: 'Connecting…',
  [CONNECTED]: 'Live',
  [RECONNECTING]: 'Reconnecting…',
  [FAILED]: 'Disconnected',
}

/** "2026-08-27T12:40:12.258731+00:00" -> "18:10:12" in the viewer's clock. */
function eventTime(iso) {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleTimeString([], { hour12: false })
}

/**
 * Render a stored value the way its metric reads.
 *
 * The backend already scaled it into rule units, so this only re-attaches the
 * unit - it never converts. Two places converting would be one place to
 * disagree with the alert message the server wrote.
 */
function formatValue(value, metric) {
  const amount = Number.parseFloat(value)
  if (!Number.isFinite(amount)) return '--'
  return `${amount.toFixed(2)}${metricInfo(metric).unit}`
}

function StatusDot({ status }) {
  return (
    <span className={`alerts__status alerts__status--${status}`}>
      <span className="alerts__dot" aria-hidden="true" />
      {STATUS_LABEL[status]}
    </span>
  )
}

function AlertRow({ event, onAcknowledge, isAcknowledging }) {
  const info = metricInfo(event.metric)
  return (
    <li className="alert">
      <div className="alert__body">
        <p className="alert__message">{event.message}</p>
        <p className="alert__meta">
          <span className="alert__metric">{event.metric_label ?? info.label}</span>
          <span className="alert__sep">·</span>
          <span>
            {formatValue(event.value, event.metric)} vs {formatValue(event.threshold, event.metric)}
          </span>
          <span className="alert__sep">·</span>
          <time dateTime={event.triggered_at}>{eventTime(event.triggered_at)}</time>
        </p>
      </div>
      <button
        type="button"
        className="button button--small"
        onClick={() => onAcknowledge(event.id)}
        disabled={isAcknowledging}
      >
        {isAcknowledging ? 'Acknowledging…' : 'Acknowledge'}
      </button>
    </li>
  )
}

function RuleForm({ portfolioId, onCreated }) {
  const [metric, setMetric] = useState(METRICS[0].value)
  const [operator, setOperator] = useState('gt')
  const [threshold, setThreshold] = useState('')
  const [error, setError] = useState(null)
  const [isSaving, setIsSaving] = useState(false)

  const selected = metricInfo(metric)

  async function submit(submitEvent) {
    submitEvent.preventDefault()
    if (isSaving) return

    setIsSaving(true)
    setError(null)
    try {
      // Sent as the typed string - the backend parses it into a Decimal, and
      // routing it through Number first would put a float in the middle of a
      // value that is about to be stored exactly.
      const rule = await createRule(portfolioId, { metric, operator, threshold: threshold.trim() })
      onCreated(rule)
      setThreshold('')
    } catch (apiError) {
      setError(apiError)
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <form className="rule-form" onSubmit={submit}>
      <div className="rule-form__row">
        <label className="rule-form__field">
          <span className="rule-form__label">Metric</span>
          <select value={metric} onChange={(changed) => setMetric(changed.target.value)}>
            {METRICS.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>

        <label className="rule-form__field rule-form__field--narrow">
          <span className="rule-form__label">Is</span>
          <select value={operator} onChange={(changed) => setOperator(changed.target.value)}>
            {OPERATORS.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>

        <label className="rule-form__field rule-form__field--narrow">
          <span className="rule-form__label">Threshold</span>
          <div className="rule-form__input-wrap">
            <input
              type="text"
              inputMode="decimal"
              value={threshold}
              onChange={(changed) => setThreshold(changed.target.value)}
              placeholder={selected.unit === '%' ? '2' : '0.3'}
              required
            />
            {selected.unit && <span className="rule-form__suffix">{selected.unit}</span>}
          </div>
        </label>

        <button type="submit" className="button" disabled={isSaving}>
          {isSaving ? 'Adding…' : 'Add rule'}
        </button>
      </div>

      <p className="rule-form__hint">{selected.hint}</p>

      {error && (
        <p className="banner banner--error" role="alert">
          {error.message}
        </p>
      )}
    </form>
  )
}

export default function AlertsPanel({ portfolioId }) {
  const [events, setEvents] = useState([])
  const [rules, setRules] = useState([])
  const [status, setStatus] = useState(CONNECTING)
  const [fatalError, setFatalError] = useState(null)
  const [rulesError, setRulesError] = useState(null)
  const [acknowledging, setAcknowledging] = useState(() => new Set())

  const socketRef = useRef(null)
  const timerRef = useRef(null)
  const attemptRef = useRef(0)
  // Guards every state write in the socket callbacks. A message can land
  // between the unmount and the socket actually closing, and React warns loudly
  // about setting state on a component that is gone.
  const aliveRef = useRef(true)

  // -- rules: plain REST, loaded once ---------------------------------------
  useEffect(() => {
    let ignore = false
    listRules(portfolioId)
      .then((loaded) => {
        if (!ignore) {
          setRules(loaded)
          setRulesError(null)
        }
      })
      .catch((error) => {
        if (!ignore) setRulesError(error)
      })
    return () => {
      ignore = true
    }
  }, [portfolioId])

  // -- the socket ------------------------------------------------------------
  useEffect(() => {
    aliveRef.current = true
    attemptRef.current = 0

    function scheduleReconnect() {
      if (!aliveRef.current) return
      // 1s, 2s, 4s ... capped at 30s. Capped rather than unbounded so a laptop
      // that slept for an hour reconnects within half a minute of waking.
      const delay = Math.min(INITIAL_BACKOFF_MS * 2 ** attemptRef.current, MAX_BACKOFF_MS)
      attemptRef.current += 1
      setStatus(RECONNECTING)
      timerRef.current = setTimeout(connect, delay)
    }

    function connect() {
      if (!aliveRef.current) return

      let socket
      try {
        socket = new WebSocket(alertsSocketUrl(portfolioId))
      } catch {
        // A malformed URL throws synchronously. Nothing to retry against, but
        // the dashboard around this panel must not care.
        scheduleReconnect()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        if (!aliveRef.current) return
        attemptRef.current = 0
        setStatus(CONNECTED)
        setFatalError(null)
      }

      socket.onmessage = (message) => {
        if (!aliveRef.current) return
        let payload
        try {
          payload = JSON.parse(message.data)
        } catch {
          return // Not ours to render; ignoring beats blanking the feed.
        }

        if (payload.type === 'snapshot') {
          // Replace, never merge. The snapshot is the server's current truth,
          // and a reconnect after a missed breach should converge on it.
          setEvents(payload.events ?? [])
        } else if (payload.type === 'alert' && payload.event) {
          setEvents((current) => {
            // A redelivery after a flaky reconnect must not double the row.
            if (current.some((existing) => existing.id === payload.event.id)) return current
            return [payload.event, ...current]
          })
        }
      }

      socket.onclose = (closeEvent) => {
        if (!aliveRef.current) return
        socketRef.current = null

        if (isFatalCloseCode(closeEvent.code)) {
          // The server is telling us this will never work. Stop retrying and
          // say why, rather than blinking "reconnecting" forever.
          setStatus(FAILED)
          setFatalError(
            `Portfolio ${portfolioId} does not exist on the server, so there is no alert feed to open.`,
          )
          return
        }
        scheduleReconnect()
      }

      // onerror always precedes onclose for a failed connection, so the
      // reconnect is scheduled there. Swallowed here to keep the browser from
      // logging an unhandled error on every retry.
      socket.onerror = () => {}
    }

    connect()

    return () => {
      aliveRef.current = false
      clearTimeout(timerRef.current)
      const socket = socketRef.current
      socketRef.current = null
      if (socket) {
        // Detach before closing: close() fires onclose, which would otherwise
        // schedule a reconnect for a component that is being torn down.
        socket.onopen = null
        socket.onmessage = null
        socket.onclose = null
        socket.onerror = null
        socket.close()
      }
    }
  }, [portfolioId])

  // -- acknowledge -----------------------------------------------------------
  const acknowledge = useCallback(async (eventId) => {
    setAcknowledging((current) => new Set(current).add(eventId))
    try {
      await acknowledgeEvent(eventId)
      // Dropped locally rather than waiting for the server to say so: the feed
      // shows OPEN alerts, and an acknowledged one is no longer open. A
      // reconnect re-derives the same list from the snapshot anyway.
      setEvents((current) => current.filter((event) => event.id !== eventId))
    } catch {
      // Left in place on failure - silently vanishing an alert that is still
      // open would be the one unforgivable bug in an alerting panel.
    } finally {
      setAcknowledging((current) => {
        const next = new Set(current)
        next.delete(eventId)
        return next
      })
    }
  }, [])

  const onRuleCreated = useCallback((rule) => {
    setRules((current) => [rule, ...current])
    setRulesError(null)
  }, [])

  return (
    <section className="panel alerts">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">Alerts</h2>
          <p className="panel__subtitle">
            {rules.length} rule{rules.length === 1 ? '' : 's'} configured · live over WebSocket
          </p>
        </div>
        <StatusDot status={status} />
      </div>

      <div className="panel__body">
        {fatalError && (
          <p className="banner banner--error" role="alert">
            {fatalError}
          </p>
        )}

        {status === RECONNECTING && !fatalError && (
          <p className="banner banner--warn">
            Lost the alert feed, retrying. Existing alerts are still shown; new ones will appear
            once the connection is back.
          </p>
        )}

        {rulesError && (
          <p className="banner banner--error" role="alert">
            Could not load alert rules: {rulesError.message}
          </p>
        )}

        <RuleForm portfolioId={portfolioId} onCreated={onRuleCreated} />

        {events.length === 0 ? (
          <p className="panel__placeholder">
            {rules.length === 0
              ? 'No rules yet. Add one above, then run `python manage.py scan_alerts` in the backend to check it.'
              : 'No open alerts. Run `python manage.py scan_alerts` in the backend to check the rules now.'}
          </p>
        ) : (
          <ul className="alerts__feed">
            {events.map((event) => (
              <AlertRow
                key={event.id}
                event={event}
                onAcknowledge={acknowledge}
                isAcknowledging={acknowledging.has(event.id)}
              />
            ))}
          </ul>
        )}
      </div>

      <p className="panel__foot">
        Acknowledging clears an alert and re-arms its rule, so the next scan can raise it again if
        the breach still stands.
      </p>
    </section>
  )
}
