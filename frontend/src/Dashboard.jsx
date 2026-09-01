/**
 * Dashboard shell: owns the data lifecycle, delegates every pixel of the report
 * to a component.
 *
 * WHOSE PORTFOLIO
 * ---------------
 * `portfolioId` is a prop, and it comes from the signed-in user's session (see
 * <App>). It used to be a hardcoded 1, which was fine while the API served one
 * demo portfolio and wrong the moment it served accounts. Nothing in this file
 * decides whose data it is - it is handed an id and renders it, which is what
 * lets the same component serve every user.
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
 * The delete buttons in <HoldingsTable> are ALWAYS offered now. They used to be
 * passed only while a round was paid for, which made sense when the panel above
 * them was a paywall; with the paywall gone, a hidden delete column would be a
 * feature no first-time user could discover. So `onDelete` is unconditional and
 * routes through `gatedWrite` like every other write - the click attempts the
 * delete, and the ₹9 modal appears only if the server refuses it.
 *
 * <AlertsPanel> is deliberately wired with nothing but the portfolio id. It owns
 * its own WebSocket, its own rules and its own errors, and shares no state with
 * the report above it - so an alert feed that cannot connect, or a Redis that is
 * down, degrades to one panel showing a grey dot rather than taking the risk
 * dashboard with it. This component never awaits it and never reads from it.
 *
 * ONE SECTION AT A TIME
 * ---------------------
 * The panels are grouped into tabs (<DashboardTabs>) and only one group is on
 * screen at once. The header, the status banners and the footer sit OUTSIDE
 * that - they describe the whole report, not a section of it.
 *
 * Nothing about fetching changed. Every panel still receives exactly what it
 * received before, on the same 30-second tick, whether or not its tab is the
 * one being shown: the poll is one request set for the whole page and always
 * was. Tabs decide what is PAINTED, not what is loaded.
 *
 * TWO WAYS TO HIDE A SECTION, AND WHY BOTH ARE HERE
 * -------------------------------------------------
 * `<Section keepMounted>` renders and hides with the `hidden` attribute;
 * without it, an inactive section is not rendered at all. Which one a panel
 * gets is decided by what it would LOSE:
 *
 *   keepMounted   <AlertsPanel>     owns a WebSocket. Unmounting it on every
 *                                   tab switch would close and reopen the
 *                                   socket, and a breach that fired while you
 *                                   were reading the Risk tab would arrive as
 *                                   a reconnect rather than an alert.
 *                 <ManageHoldings>  owns form state and the CSV import report.
 *                                   Switching tabs mid-edit must not throw
 *                                   away a half-typed position or the report
 *                                   saying which two rows failed.
 *
 *   unmounted     everything with a chart in it. Recharts measures its
 *                 container, and a container inside `display: none` measures
 *                 zero - so a chart first rendered while hidden can come back
 *                 blank. Mounting it fresh when its tab opens sidesteps that
 *                 entirely, and costs one render of data already in memory.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  deleteHolding,
  getPerformance,
  getRebalance,
  getRiskReport,
  listHoldings,
} from './api/client'
import { useUnlock } from './payments/unlock-context'
import AlertsPanel from './components/AlertsPanel'
import AllocationPie from './components/AllocationPie'
import CorrelationHeatmap from './components/CorrelationHeatmap'
import DashboardTabs from './components/DashboardTabs'
import { DEFAULT_TAB } from './components/dashboard-tabs'
import Header from './components/Header'
import HoldingsTable from './components/HoldingsTable'
import ManageHoldings from './components/ManageHoldings'
import PerformancePanel from './components/PerformancePanel'
import RebalanceCard from './components/RebalanceCard'
import RiskCards from './components/RiskCards'
import { DashboardSkeleton } from './components/Skeleton'
import VarChart from './components/VarChart'

// TODO Part 3: a portfolio picker, once an account can hold more than one and
// /api/portfolios/ exists. Until then every account has exactly one and the id
// arrives from the session.
const POLL_INTERVAL_MS = 30_000

export default function Dashboard({ portfolioId }) {
  // Deleting a holding is a gated write exactly like adding one, so it goes
  // through the same wrapper: try it, and let the modal handle a 402.
  const { gatedWrite } = useUnlock()
  // Which section is on screen. Plain state, no router: there is one page here
  // and these are not addresses - a tab is not worth a dependency, a history
  // entry, or a URL somebody could bookmark into a section that may not exist
  // in the next version.
  const [tab, setTab] = useState(DEFAULT_TAB)
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
          getRiskReport(portfolioId),
          getRebalance(portfolioId),
          getPerformance(portfolioId),
          listHoldings(portfolioId),
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
    // portfolioId belongs here even though it cannot change today: the moment a
    // portfolio picker exists, an effect that ignores it would keep polling the
    // previous account's report.
  }, [attempt, portfolioId])

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
    async (holdingId, ticker) => {
      await gatedWrite(
        {
          action: `Remove ${ticker}`,
          detail: 'Deletes this position from your portfolio. This cannot be undone.',
          noun: 'change',
        },
        () => deleteHolding(portfolioId, holdingId),
      )
      refreshAll()
    },
    [gatedWrite, refreshAll, portfolioId],
  )

  // First load, nothing to show yet. A skeleton of the real layout rather than
  // a spinner on an empty page - see components/Skeleton.jsx for why.
  if (isLoading && !report) {
    return <DashboardSkeleton />
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
  //
  // An empty portfolio is therefore rendered as an EMPTY STATE and not as a
  // failure: no red, no error code, no Retry - just what to do next, with the
  // form that does it directly underneath. A new user's first screen should not
  // be an error, and 'empty_portfolio' is the one code here that means "nothing
  // is wrong yet".
  if (error && !report) {
    const isEmpty = error.code === 'empty_portfolio'

    return (
      <main className="page page--centered">
        {isEmpty ? (
          <div className="status">
            <span className="empty__icon" aria-hidden="true">
              <PortfolioIcon />
            </span>
            <p className="status__title">Start your portfolio</p>
            <p className="status__detail">
              There is nothing to measure yet. Add a position below — or import a CSV
              of them — and the risk report, the value curve and the rebalance
              suggestion all build themselves from it.
            </p>
          </div>
        ) : (
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
        )}

        <div className="page__recovery">
          <ManageHoldings portfolioId={portfolioId} onChanged={retry} />
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
        portfolioId={portfolioId}
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

      <DashboardTabs active={tab} onChange={setTab} />

      <Section id="overview" active={tab === 'overview'}>
        <RiskCards report={report} />
        <AllocationPie holdings={report.portfolio?.holdings} />
      </Section>

      <Section id="performance" active={tab === 'performance'}>
        <PerformancePanel data={performance} error={performanceError} isLoading={isLoading} />
      </Section>

      {/*
        RiskCards appears here as well as on Overview, and that is deliberate:
        a tab called "Risk" that opened on two charts and no numbers would be
        the wrong answer to the click. Overview shows them as a summary; this
        shows them as the headline over the detail.
      */}
      <Section id="risk" active={tab === 'risk'}>
        <RiskCards report={report} />
        <div className="grid grid--halves">
          <VarChart report={report} />
          <CorrelationHeatmap matrix={report.correlation_matrix} />
        </div>
      </Section>

      <Section id="rebalance" active={tab === 'rebalance'}>
        <RebalanceCard data={rebalance} error={rebalanceError} isLoading={isLoading} />
      </Section>

      {/* keepMounted: the forms and the import report must survive a tab switch. */}
      <Section id="holdings" active={tab === 'holdings'} keepMounted>
        <ManageHoldings portfolioId={portfolioId} onChanged={refreshAll} />
        <HoldingsTable
          holdings={report.portfolio?.holdings}
          marketValue={report.portfolio?.market_value}
          holdingIds={holdingIds}
          onDelete={removeHolding}
        />
      </Section>

      {/* keepMounted: closing the socket on every tab switch would lose breaches. */}
      <Section id="alerts" active={tab === 'alerts'} keepMounted>
        <AlertsPanel portfolioId={portfolioId} />
      </Section>

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

/** The empty-portfolio mark: a bar chart with nothing in it yet. */
function PortfolioIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M4 20h16M7 20v-5m5 5V9m5 11v-8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
    </svg>
  )
}

/**
 * One tab's worth of dashboard.
 *
 * `keepMounted` is the whole of the difference between the two hiding
 * strategies described at the top of this file: with it, the section is always
 * in the DOM and the `hidden` attribute takes it off screen, so anything alive
 * inside it - a socket, a half-filled form - stays alive. Without it, an
 * inactive section is not rendered, which is what a chart wants: Recharts
 * measures its container, and a container that has never been visible measures
 * zero.
 *
 * `role="tabpanel"` and the id pairing are what tie this back to the button in
 * <DashboardTabs> that controls it.
 *
 * THE `hidden` HALF OF THIS LIVES IN CSS
 * --------------------------------------
 * `hidden` is only as strong as `[hidden] { display: none }` in the browser's
 * default stylesheet, which any class-based `display` in our own stylesheet
 * outranks. `.tab-panel` sets `display: flex`, so for a while it did exactly
 * that, and every tab rendered the holdings forms and the alerts feed on top
 * of its own content - the attribute below was correct and had no effect.
 * index.css now restates the rule at author level (globally in Reset, and
 * again on `.tab-panel[hidden]`). If sections ever leak across tabs again,
 * that is the first place to look, not this file.
 */
function Section({ id, active, keepMounted = false, children }) {
  if (!active && !keepMounted) return null

  return (
    <section
      id={`panel-${id}`}
      role="tabpanel"
      aria-labelledby={`tab-${id}`}
      className="tab-panel"
      hidden={!active}
      // Without a tabindex the panel itself is not focusable, so a keyboard
      // user pressing Tab out of the tablist lands on the first control INSIDE
      // it - fine for Holdings, but it skips a panel that is only charts.
      tabIndex={active ? 0 : -1}
    >
      {children}
    </section>
  )
}
