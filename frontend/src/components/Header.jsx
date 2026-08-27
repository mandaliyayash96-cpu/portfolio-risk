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
 */

import { isoDate, clockTime, money } from '../format'
import ThemeToggle from './ThemeToggle'

export default function Header({ report, lastUpdated, isRefreshing }) {
  const portfolio = report?.portfolio
  const benchmark = report?.benchmark

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
        <ThemeToggle />

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
      </div>
    </header>
  )
}
