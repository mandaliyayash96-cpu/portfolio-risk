/**
 * "Connect a broker" - simulated multi-platform aggregation.
 *
 * WHAT THIS IS HONEST ABOUT
 * -------------------------
 * Nothing here connects to anything. Pressing a card does not open an OAuth
 * flow, does not ask for a credential, and does not put one anywhere. It calls
 * our own backend, which answers with a preset sample per broker - see
 * `backend/portfolio/brokers.py`. The note under the cards says so in the UI,
 * the response carries `simulated: true`, and the button labels say "Import
 * sample" rather than "Log in", because a demo that looks like a real login is
 * the one version of this feature that would be dishonest.
 *
 * The brand marks are a coloured circle with the broker's initial. Deliberately
 * not their logos: reproducing a trademark to suggest an integration that does
 * not exist is a legal problem on top of an accuracy one, and an initial reads
 * just as well at 40px.
 *
 * WHAT IS REAL
 * ------------
 * The import. It goes through `gatedWrite` like the manual form and the CSV
 * upload, so it hits the same ₹9 gate, opens the same payment modal on a 402,
 * and lands through the same upsert-on-ticker write path. Importing Zerodha and
 * then ICICI Direct - which both report HDFCBANK.NS in the samples - leaves one
 * consolidated position, and that is the whole point of the feature.
 *
 * THE TWO-PHASE BUSY STATE
 * ------------------------
 * "Connecting to Zerodha…" is a deliberate, honest-in-context pause before the
 * request goes out: it is what the connect step WOULD take, and without it the
 * whole interaction is one flicker. It is CONNECT_DELAY_MS long and does
 * nothing else. Once it ends the label changes to "Fetching holdings…" and a
 * real request is in flight, so from that moment the spinner is describing
 * something that is actually happening.
 */

import { useEffect, useRef, useState } from 'react'

import { importBroker } from '../api/client'
import { PAYMENT_CANCELLED, useUnlock } from '../payments/unlock-context'
import ImportReport from './ImportReport'

/**
 * How long the simulated connect step is shown before the import fires.
 *
 * Long enough to read the broker's name in the label, short enough that nobody
 * waits on a demo. The import itself follows immediately and takes as long as
 * it takes.
 */
const CONNECT_DELAY_MS = 900

/**
 * The brokers, as the UI shows them.
 *
 * `id` must match a slug in `backend/portfolio/brokers.py` - that is the only
 * part of this table the server knows about, and the server rejects anything
 * else with a 400. `initial` and `tint` are presentation and live only here.
 *
 * `tint` is a CSS custom property set per card, so the badge is coloured
 * without any of these hexes appearing in the stylesheet. They are picked to
 * be legible on both themes' surfaces and are NOT the brokers' brand colours -
 * see the note above about not impersonating them.
 */
const BROKERS = [
  { id: 'zerodha', label: 'Zerodha', initial: 'Z', tint: '#2563eb' },
  { id: 'groww', label: 'Groww', initial: 'G', tint: '#0f766e' },
  { id: 'upstox', label: 'Upstox', initial: 'U', tint: '#7c3aed' },
  { id: 'icici', label: 'ICICI Direct', initial: 'I', tint: '#c2410c' },
]

/** True when the user closed the payment sheet rather than the write failing. */
const isCancellation = (error) => error?.code === PAYMENT_CANCELLED

/** A promise that resolves after `ms`, for the simulated connect step. */
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export default function BrokerConnect({ portfolioId, onChanged }) {
  const { gatedWrite, pending } = useUnlock()
  // The broker currently importing, or null. One at a time: two imports of
  // overlapping brokers racing each other would make the final position depend
  // on which request happened to land second.
  const [busy, setBusy] = useState(null)
  // 'connecting' | 'importing'. Only meaningful while `busy` is set.
  const [phase, setPhase] = useState('connecting')
  const [error, setError] = useState(null)
  const [report, setReport] = useState(null)

  // The simulated delay is a timer and the import that follows it is a request,
  // so this component can be torn down (a sign-out unmounts the dashboard) with
  // an await still outstanding. The guard stops the resolution from calling
  // onChanged() into a dashboard that is no longer there.
  //
  // The flag is set to true INSIDE the effect, not just at useRef(true). Under
  // StrictMode the effect mounts, cleans up and mounts again, so an effect that
  // only ever set it false would leave it false for the life of the component
  // in dev - and every import would land, refresh nothing, and show no report.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  async function connect(broker) {
    if (busy) return

    setBusy(broker.id)
    setPhase('connecting')
    setError(null)
    setReport(null)
    try {
      // MOCK: stands in for the OAuth handshake a real integration would do
      // here. Nothing is happening during this await.
      await wait(CONNECT_DELAY_MS)
      if (!mounted.current) return
      setPhase('importing')

      // From here it is an ordinary gated write - the same call shape the
      // manual form and the CSV import use, refused with the same 402 and
      // resolved by the same payment modal.
      const summary = await gatedWrite(
        {
          action: `Import holdings from ${broker.label}`,
          detail: 'Sample data — positions are added to your portfolio, matching tickers are replaced',
          noun: 'holdings',
        },
        () => importBroker(portfolioId, broker.id),
      )
      if (!mounted.current) return
      setReport(summary)
      onChanged()
    } catch (apiError) {
      // Cancelling the payment is a decision, not a failure. Say nothing.
      if (mounted.current && !isCancellation(apiError)) setError(apiError)
    } finally {
      if (mounted.current) setBusy(null)
    }
  }

  const busyBroker = BROKERS.find((broker) => broker.id === busy) ?? null

  let busyLabel = null
  if (busyBroker) {
    if (pending !== null) busyLabel = 'Waiting for payment…'
    else if (phase === 'connecting') busyLabel = `Connecting to ${busyBroker.label}… fetching holdings`
    else busyLabel = `Importing ${busyBroker.label} holdings…`
  }

  return (
    <div className="broker">
      <div className="broker__grid">
        {BROKERS.map((broker) => {
          const isThisOne = busy === broker.id
          return (
            <button
              key={broker.id}
              type="button"
              className="broker__card"
              style={{ '--broker-tint': broker.tint }}
              onClick={() => connect(broker)}
              // Every card is disabled while any import runs, so a second
              // broker cannot be started on top of the first.
              disabled={busy !== null}
              aria-busy={isThisOne}
            >
              <span className="broker__badge" aria-hidden="true">
                {broker.initial}
              </span>
              <span className="broker__name">{broker.label}</span>
              <span className="broker__action">
                {isThisOne ? (
                  <>
                    <span className="spinner spinner--inline" aria-hidden="true" />
                    <span>{phase === 'connecting' ? 'Connecting…' : 'Importing…'}</span>
                  </>
                ) : (
                  'Import sample'
                )}
              </span>
            </button>
          )
        })}
      </div>

      {/* One live region for the whole grid rather than per-card text, so a
          screen reader hears the phase change once and in full. */}
      {busyLabel && (
        <p className="manage__busy" role="status">
          <span className="spinner spinner--inline" aria-hidden="true" />
          <span>{busyLabel}</span>
        </p>
      )}

      <p className="broker__note">
        <strong>Demo:</strong> broker sync is simulated with sample data. Live API
        integration (e.g. Zerodha Kite Connect) is on our roadmap.
      </p>

      {error && (
        <p className="banner banner--error" role="alert">
          <strong>{error.code ?? 'error'}:</strong> {error.message}
        </p>
      )}

      {report && (
        <>
          <p className="manage__hint" role="status">
            <strong>{report.broker_label}</strong> reported {report.holdings?.length ?? 0}{' '}
            position{report.holdings?.length === 1 ? '' : 's'}. Tickers you already held
            were replaced, so brokers that overlap leave one consolidated position.
          </p>

          {/* The statement as it arrived, before the import did anything with
              it. The table below says what became of each row; this says what
              was pulled - which is the half a demo of "aggregation" has to
              show. */}
          {report.holdings?.length > 0 && (
            <ul className="broker__statement">
              {report.holdings.map((row) => (
                <li key={row.ticker} className="broker__position">
                  <span className="broker__position-ticker">{row.ticker}</span>
                  <span className="broker__position-qty">×{row.quantity}</span>
                </li>
              ))}
            </ul>
          )}

          <ImportReport report={report} />
        </>
      )}
    </div>
  )
}
