/**
 * Dashboard shell: owns the data lifecycle, delegates every pixel of the report
 * to a component.
 *
 * Refresh behaviour is built for a live demo. The first load shows a skeleton;
 * every poll after that keeps the last good report on screen and only marks it
 * "refreshing", so the page never blanks mid-sentence. A failed poll that
 * follows a successful one is shown as a banner above the still-valid numbers
 * rather than replacing them - stale data plus a warning beats an empty screen.
 *
 * <AlertsPanel> is deliberately wired with nothing but the portfolio id. It owns
 * its own WebSocket, its own rules and its own errors, and shares no state with
 * the report above it - so an alert feed that cannot connect, or a Redis that is
 * down, degrades to one panel showing a grey dot rather than taking the risk
 * dashboard with it. This component never awaits it and never reads from it.
 */

import { useEffect, useState } from 'react'

import { getRebalance, getRiskReport } from './api/client'
import AlertsPanel from './components/AlertsPanel'
import AllocationPie from './components/AllocationPie'
import CorrelationHeatmap from './components/CorrelationHeatmap'
import Header from './components/Header'
import HoldingsTable from './components/HoldingsTable'
import RebalanceCard from './components/RebalanceCard'
import RiskCards from './components/RiskCards'
import VarChart from './components/VarChart'

// TODO Phase 8: a portfolio picker, once /api/portfolios/ exists. Hardcoded
// while the API serves exactly one demo portfolio.
const PORTFOLIO_ID = 1

const POLL_INTERVAL_MS = 30_000

export default function App() {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState(null)
  // The rebalance suggestion is tracked separately on purpose: it is the
  // optional half of the page, so its failure must never take the risk report
  // down with it.
  const [rebalance, setRebalance] = useState(null)
  const [rebalanceError, setRebalanceError] = useState(null)
  // Bumped by Retry to re-run the effect below - the one honest way to say
  // "do that again" to an effect.
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    // Scoped to this effect run, so a response that lands after unmount (or
    // after StrictMode's development remount) is dropped instead of writing to
    // a stale component.
    let ignore = false

    async function load() {
      // allSettled, not all: one endpoint failing must not discard the other's
      // result. Both are fetched on the same tick so the two panels always
      // describe the same moment.
      const [riskOutcome, rebalanceOutcome] = await Promise.allSettled([
        getRiskReport(PORTFOLIO_ID),
        getRebalance(PORTFOLIO_ID),
      ])
      if (ignore) return

      if (riskOutcome.status === 'fulfilled') {
        setReport(riskOutcome.value)
        setError(null)
        setLastUpdated(new Date())
      } else {
        setError(riskOutcome.reason)
      }

      if (rebalanceOutcome.status === 'fulfilled') {
        setRebalance(rebalanceOutcome.value)
        setRebalanceError(null)
      } else {
        setRebalanceError(rebalanceOutcome.reason)
      }

      setIsLoading(false)
      setIsRefreshing(false)
    }

    load()

    // The poll marks itself as refreshing; `load` never touches state
    // synchronously, which keeps the effect from cascading renders on mount.
    const timer = setInterval(() => {
      setIsRefreshing(true)
      load()
    }, POLL_INTERVAL_MS)

    return () => {
      ignore = true
      clearInterval(timer)
    }
  }, [attempt])

  const retry = () => {
    setIsLoading(true)
    setError(null)
    setRebalanceError(null)
    setAttempt((count) => count + 1)
  }

  // First load, nothing to show yet.
  if (isLoading && !report) {
    return (
      <main className="page page--centered">
        <div className="status">
          <span className="spinner" aria-hidden="true" />
          <p className="status__title">Computing risk report…</p>
          <p className="status__detail">Portfolio {PORTFOLIO_ID}</p>
        </div>
      </main>
    )
  }

  // Failed before we ever had data: the error IS the page.
  if (error && !report) {
    return (
      <main className="page page--centered">
        <div className="status status--error" role="alert">
          <p className="status__badge">{error.code ?? 'error'}</p>
          <p className="status__title">Could not load the risk report</p>
          <p className="status__detail">{error.message}</p>
          {error.details?.tickers && (
            <p className="status__detail status__detail--muted">
              Affected: {error.details.tickers.join(', ')}
            </p>
          )}
          <button type="button" className="button" onClick={retry}>
            Retry
          </button>
        </div>
      </main>
    )
  }

  return (
    <main className="page">
      <Header report={report} lastUpdated={lastUpdated} isRefreshing={isRefreshing} />

      {error && (
        <p className="banner banner--error" role="alert">
          <strong>Refresh failed:</strong> {error.message} Showing the last successful report.
        </p>
      )}

      {report.warnings?.map((warning) => (
        <p key={warning} className="banner banner--warn">
          {warning}
        </p>
      ))}

      <RiskCards report={report} />

      <RebalanceCard data={rebalance} error={rebalanceError} isLoading={isLoading} />

      <AlertsPanel portfolioId={PORTFOLIO_ID} />

      <div className="grid grid--halves">
        <AllocationPie holdings={report.portfolio?.holdings} />
        <VarChart report={report} />
      </div>

      <div className="grid grid--halves">
        <HoldingsTable
          holdings={report.portfolio?.holdings}
          marketValue={report.portfolio?.market_value}
        />
        <CorrelationHeatmap matrix={report.correlation_matrix} />
      </div>

      <footer className="footer">
        <span>
          Prices from the last <code>manage.py fetch_prices</code> run · report recomputed on every
          request
        </span>
        <span>
          Auto-refreshing every {POLL_INTERVAL_MS / 1000}s · alerts stream live over WebSocket
        </span>
      </footer>
    </main>
  )
}
