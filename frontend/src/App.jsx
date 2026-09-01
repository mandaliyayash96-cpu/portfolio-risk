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
 * <ManageHoldings> is the one child that can change what the report says, so
 * it is wired the other way round from the rest: it reports upward, through a
 * single `onChanged` callback that bumps `attempt` and re-runs the load below.
 * That is the whole integration - after an add, an import or a delete, risk,
 * rebalance, performance and the holdings list are all re-fetched on one tick,
 * so no panel is ever describing a portfolio that no longer exists. Its own
 * failures stay inside it; a rejected CSV cannot touch the numbers on screen.
 *
 * <AlertsPanel> is deliberately wired with nothing but the portfolio id. It owns
 * its own WebSocket, its own rules and its own errors, and shares no state with
 * the report above it - so an alert feed that cannot connect, or a Redis that is
 * down, degrades to one panel showing a grey dot rather than taking the risk
 * dashboard with it. This component never awaits it and never reads from it.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  deleteHolding,
  getPerformance,
  getRebalance,
  getRiskReport,
  listHoldings,
} from './api/client'
import AlertsPanel from './components/AlertsPanel'
import AllocationPie from './components/AllocationPie'
import CorrelationHeatmap from './components/CorrelationHeatmap'
import Header from './components/Header'
import HoldingsTable from './components/HoldingsTable'
import ManageHoldings from './components/ManageHoldings'
import PerformancePanel from './components/PerformancePanel'
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
  // Same arrangement for the performance curve, and for the same reason: the
  // value chart leads the page, but it must not be able to take the page down.
  const [performance, setPerformance] = useState(null)
  const [performanceError, setPerformanceError] = useState(null)
  // The holdings ROWS, as opposed to the report's valuation of them. Fetched
  // only for their ids, which the delete button needs and the report has no
  // business carrying. A failure here costs the delete buttons and nothing else.
  const [holdings, setHoldings] = useState([])
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
      const [riskOutcome, rebalanceOutcome, performanceOutcome, holdingsOutcome] =
        await Promise.allSettled([
          getRiskReport(PORTFOLIO_ID),
          getRebalance(PORTFOLIO_ID),
          getPerformance(PORTFOLIO_ID),
          listHoldings(PORTFOLIO_ID),
        ])
      if (ignore) return

      if (riskOutcome.status === 'fulfilled') {
        setReport(riskOutcome.value)
        setError(null)
        setLastUpdated(new Date())
      } else {
        setError(riskOutcome.reason)
        // "Keep the last good report" is the right policy for a TRANSIENT
        // failure - a timeout, a dropped connection - where the numbers on
        // screen are still the best available answer. `empty_portfolio` is not
        // that. It is the backend stating that the portfolio now holds nothing,
        // which after a delete is the user's own doing, and leaving the old
        // table up would show them a position they just removed. So this one
        // failure clears the report and falls through to the error page - which
        // carries the add form, so the fix is right there.
        if (riskOutcome.reason?.code === 'empty_portfolio') {
          setReport(null)
          setRebalance(null)
          setPerformance(null)
          setHoldings([])
        }
      }

      if (rebalanceOutcome.status === 'fulfilled') {
        setRebalance(rebalanceOutcome.value)
        setRebalanceError(null)
      } else {
        setRebalanceError(rebalanceOutcome.reason)
      }

      if (performanceOutcome.status === 'fulfilled') {
        setPerformance(performanceOutcome.value)
        setPerformanceError(null)
      } else {
        setPerformanceError(performanceOutcome.reason)
      }

      // No error state for this one on purpose. It contributes ids, not
      // information - the last good list stays, and the buttons keep working.
      if (holdingsOutcome.status === 'fulfilled') {
        setHoldings(holdingsOutcome.value)
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
    setPerformanceError(null)
    setAttempt((count) => count + 1)
  }

  /**
   * Re-read everything, without blanking anything.
   *
   * Deliberately NOT `retry`: this runs after a write the user just made, when
   * there is already a good report on screen. Setting isLoading would swap the
   * whole page for a skeleton and lose the import report they are still reading.
   * Bumping `attempt` re-runs the same effect and the panels update in place.
   */
  const refreshAll = useCallback(() => {
    setIsRefreshing(true)
    setAttempt((count) => count + 1)
  }, [])

  /**
   * ticker -> holding id, for the delete button in the table below.
   *
   * A map rather than a lookup per row: the table joins on every render, and
   * one pass here beats a find() per position.
   */
  const holdingIds = useMemo(
    () => Object.fromEntries(holdings.map((holding) => [holding.ticker, holding.id])),
    [holdings],
  )

  const removeHolding = useCallback(
    async (holdingId) => {
      await deleteHolding(PORTFOLIO_ID, holdingId)
      refreshAll()
    },
    [refreshAll],
  )

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
  //
  // With one exception, and it is the reason <ManageHoldings> is mounted here
  // too. An EMPTY portfolio is a first-class failure of the risk report - there
  // is nothing to measure - and it is also the exact state a new user starts
  // in. Showing them only "this portfolio has no holdings" with a Retry button
  // would be a dead end: the fix for that error is to add a holding, so the
  // form that adds one has to be reachable from the error page. The same is
  // true of `missing_price_data`, where the fix is often to delete the ticker
  // that has no prices.
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

        <div className="page__recovery">
          <ManageHoldings portfolioId={PORTFOLIO_ID} onChanged={retry} />
        </div>
      </main>
    )
  }

  return (
    <main className="page">
      <Header
        report={report}
        lastUpdated={lastUpdated}
        isRefreshing={isRefreshing}
        portfolioId={PORTFOLIO_ID}
      />

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

      <PerformancePanel data={performance} error={performanceError} isLoading={isLoading} />

      <RiskCards report={report} />

      <RebalanceCard data={rebalance} error={rebalanceError} isLoading={isLoading} />

      <ManageHoldings portfolioId={PORTFOLIO_ID} onChanged={refreshAll} />

      <AlertsPanel portfolioId={PORTFOLIO_ID} />

      <div className="grid grid--halves">
        <AllocationPie holdings={report.portfolio?.holdings} />
        <VarChart report={report} />
      </div>

      <div className="grid grid--halves">
        <HoldingsTable
          holdings={report.portfolio?.holdings}
          marketValue={report.portfolio?.market_value}
          holdingIds={holdingIds}
          onDelete={removeHolding}
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
