/**
 * Page header: what this is, whose money it is, and how current the numbers are.
 *
 * TWO ROWS, SPLIT BY SUBJECT
 * --------------------------
 * The top bar is about the APP and the person using it - the mark, the product
 * name, the signed-in number, the theme switch, the way out. The card below is
 * about the MONEY - the portfolio's name, the window it was measured over, and
 * the one figure that answers "what is it worth".
 *
 * They used to share a row, which put the Log out button an inch from the
 * market value and made it read as a control for the amount. Separating them
 * costs a line of vertical space and removes that reading entirely.
 *
 * "As of" is deliberately two different times. `end` is the last trading date
 * in the measured window - the age of the DATA. `lastUpdated` is when the
 * browser last refreshed - the age of the SCREEN. On a projector those diverge
 * (nobody re-fetches prices mid-demo) and conflating them would be a lie.
 *
 * The PDF download sits under the market value, because the document it
 * produces is a snapshot of exactly what this header describes - the same
 * portfolio, the same window, the same numbers. Its failures stay local: a
 * report that cannot be generated shows a line of red text under the button and
 * leaves every panel on the page untouched.
 *
 * The signed-in number and the way out are read from context rather than passed
 * down. Identity is not the dashboard's business - it does not own it, cannot
 * change it, and would only be forwarding two props it never reads. The header
 * is the one place that renders them, so it is the one place that asks.
 */

import { useState } from 'react'

import { ApiError, downloadRiskReportPdf, saveBlob } from '../api/client'
import { useAuth } from '../auth/auth-context'
import { isoDate, clockTime, money } from '../format'
import BrandMark from './BrandMark'
import ThemeToggle from './ThemeToggle'

export default function Header({ report, lastUpdated, isRefreshing, portfolioId }) {
  const portfolio = report?.portfolio
  const benchmark = report?.benchmark
  const { phone, logout } = useAuth()

  const [isDownloading, setIsDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)

  // The id is taken from the report when the caller did not pass one, so this
  // component keeps working if it is ever mounted without the prop.
  const reportId = portfolioId ?? portfolio?.id

  async function downloadReport() {
    if (isDownloading || reportId == null) return

    setIsDownloading(true)
    setDownloadError(null)
    try {
      const { blob, filename } = await downloadRiskReportPdf(reportId)
      saveBlob(blob, filename)
    } catch (error) {
      // Shown, never thrown: the dashboard must survive a failed download.
      setDownloadError(
        error instanceof ApiError ? error : new ApiError('Could not generate the report.'),
      )
    } finally {
      setIsDownloading(false)
    }
  }

  return (
    <header className="header">
      <div className="header__bar">
        <div className="header__brand">
          <BrandMark />
          <div className="header__wordmark">
            <p className="header__eyebrow">Clarisk</p>
            <p className="header__product">See your risk clearly.</p>
          </div>
        </div>

        <div className="header__account">
          {phone && (
            <span className="header__phone" title="Signed in">
              {phone}
            </span>
          )}
          <ThemeToggle />
          {phone && (
            <button type="button" className="button button--small button--ghost" onClick={logout}>
              Log out
            </button>
          )}
        </div>
      </div>

      <div className="header__summary">
        <div className="header__headline">
          <h1 className="header__title">{portfolio?.name ?? 'Risk Dashboard'}</h1>
          <div className="header__meta">
            {report ? (
              <>
                <span className="chip">
                  {report.observations} trading days
                </span>
                <span className="chip chip--muted">
                  {isoDate(report.start)} → {isoDate(report.end)}
                </span>
                {benchmark?.ticker && (
                  <span className="chip chip--muted">
                    Benchmark {benchmark.ticker}
                    {benchmark.included ? '' : ' (unavailable)'}
                  </span>
                )}
              </>
            ) : (
              <span className="chip chip--muted">Connecting to the risk API…</span>
            )}
          </div>
        </div>

        <div className="header__value">
          <p className="header__value-label">Market value</p>
          <p className="header__value-amount">{money(portfolio?.market_value)}</p>
          <p className="header__value-foot">
            <span
              className={`header__pulse ${isRefreshing ? 'header__pulse--live' : ''}`}
              aria-hidden="true"
            />
            Updated {clockTime(lastUpdated)}
            {isRefreshing && <span className="header__refreshing"> · refreshing</span>}
          </p>
        </div>

        <div className="header__actions">
          <button
            type="button"
            className="button button--small button--ghost"
            onClick={downloadReport}
            disabled={isDownloading || reportId == null}
          >
            {isDownloading && <span className="spinner spinner--inline" aria-hidden="true" />}
            {isDownloading ? 'Preparing…' : 'Download report (PDF)'}
          </button>

          {downloadError && (
            <p className="header__download-error" role="alert">
              {downloadError.message}
            </p>
          )}
        </div>
      </div>
    </header>
  )
}
