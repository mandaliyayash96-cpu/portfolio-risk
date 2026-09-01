/**
 * Page header: what this is, whose money it is, and how current the numbers are.
 *
 * "As of" is deliberately two different times. `end` is the last trading date
 * in the measured window - the age of the DATA. `lastUpdated` is when the
 * browser last refreshed - the age of the SCREEN. On a projector those diverge
 * (nobody re-fetches prices mid-demo) and conflating them would be a lie.
 *
 * The theme toggle lives here because the header is the only element that spans
 * the full width at the top of the page. It sits inside `.header__aside` with
 * the market value rather than being absolutely positioned over the corner, so
 * it can never overlap the amount at a narrow width.
 *
 * The PDF download sits with them, under the market value, because the document
 * it produces is a snapshot of exactly what this header describes - the same
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
      <div className="header__identity">
        <p className="header__eyebrow">Investor Portfolio Monitoring &amp; Risk Management</p>
        <h1 className="header__title">{portfolio?.name ?? 'Risk Dashboard'}</h1>
        <p className="header__meta">
          {report ? (
            <>
              <span>
                {report.observations} trading days &middot; {isoDate(report.start)} to{' '}
                {isoDate(report.end)}
              </span>
              {benchmark?.ticker && (
                <>
                  <span className="header__dot" aria-hidden="true" />
                  <span>
                    Benchmark {benchmark.ticker}
                    {benchmark.included ? '' : ' (unavailable)'}
                  </span>
                </>
              )}
            </>
          ) : (
            <span>Connecting to the risk API…</span>
          )}
        </p>
      </div>

      <div className="header__aside">
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

          <button
            type="button"
            className="button button--small button--ghost header__download"
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
