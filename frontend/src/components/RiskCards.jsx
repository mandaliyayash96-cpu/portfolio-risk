/**
 * The eight headline metrics, each as a big number with a plain-English line
 * underneath.
 *
 * ---------------------------------------------------------------------------
 * About the colours
 * ---------------------------------------------------------------------------
 * The API returns measurements, not verdicts. Every threshold below is a
 * PRESENTATION heuristic chosen for this dashboard - conventional rules of
 * thumb (Sharpe > 1 is good; a drawdown past 20% hurts; HHI > 0.5 is a
 * two-stock portfolio), not something the risk engine asserts. They live
 * together in this one table so they are easy to argue with and easy to change,
 * and so nobody mistakes a red card for an engine output.
 *
 * Tone vocabulary: 'good' (green), 'warn' (amber), 'bad' (red), 'flat' (ink).
 */

import { decimal, isMissing, percent } from '../format'

/** Pick the first band whose test passes; bands are ordered worst-first. */
const band = (value, bands) => bands.find(([test]) => test(value))?.[1] ?? 'flat'

const METRICS = [
  {
    key: 'annualized_volatility',
    label: 'Ann. Volatility',
    format: percent,
    explain: 'How much the portfolio swings in a year — bigger means a rougher ride.',
    tone: (v) =>
      band(v, [
        [(x) => x >= 0.3, 'bad'],
        [(x) => x >= 0.18, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'beta',
    label: 'Beta',
    format: (v) => decimal(v, 2),
    explain: 'Move per 1% move in the benchmark. Above 1 amplifies the market.',
    tone: (v) =>
      band(v, [
        [(x) => x >= 1.2, 'bad'],
        [(x) => x >= 1.0, 'warn'],
        [() => true, 'good'],
      ]),
    missingNote: 'Needs benchmark prices',
  },
  {
    key: 'sharpe',
    label: 'Sharpe Ratio',
    format: (v) => decimal(v, 2),
    explain: 'Return earned per unit of risk taken. Above 1 is considered good.',
    tone: (v) =>
      band(v, [
        [(x) => x < 0, 'bad'],
        [(x) => x < 1, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'sortino',
    label: 'Sortino Ratio',
    format: (v) => decimal(v, 2),
    explain: 'Like Sharpe, but only counts downside moves as risk.',
    tone: (v) =>
      band(v, [
        [(x) => x < 0, 'bad'],
        [(x) => x < 1, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'max_drawdown',
    label: 'Max Drawdown',
    format: (v) => percent(v, 1),
    explain: 'Worst peak-to-trough fall over the window.',
    tone: (v) =>
      band(v, [
        [(x) => x <= -0.2, 'bad'],
        [(x) => x <= -0.1, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'var_historical',
    label: 'VaR (95%)',
    format: (v) => percent(v, 2),
    explain: 'Max expected 1-day loss, 95% confidence — exceeded 1 day in 20.',
    tone: (v) =>
      band(v, [
        [(x) => x >= 0.03, 'bad'],
        [(x) => x >= 0.02, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'cvar',
    label: 'CVaR (95%)',
    format: (v) => percent(v, 2),
    explain: 'Average loss on the days that are worse than VaR.',
    tone: (v) =>
      band(v, [
        [(x) => x >= 0.04, 'bad'],
        [(x) => x >= 0.025, 'warn'],
        [() => true, 'good'],
      ]),
  },
  {
    key: 'hhi',
    label: 'Concentration (HHI)',
    format: (v) => decimal(v, 3),
    explain: 'How lopsided the holdings are. 1.0 is everything in one stock.',
    tone: (v) =>
      band(v, [
        [(x) => x >= 0.5, 'bad'],
        [(x) => x >= 0.25, 'warn'],
        [() => true, 'good'],
      ]),
    footnote: (report) =>
      isMissing(report.effective_holdings)
        ? null
        : `≈ ${decimal(report.effective_holdings, 1)} equally-weighted holdings`,
  },
]

function MetricCard({ metric, report }) {
  const value = report[metric.key]
  const missing = isMissing(value)
  const tone = missing ? 'flat' : metric.tone(value)
  const footnote = missing ? metric.missingNote : metric.footnote?.(report)

  return (
    <article className={`card card--${tone}`}>
      <h3 className="card__label">{metric.label}</h3>
      <p className="card__value">{metric.format(value)}</p>
      <p className="card__explain">{metric.explain}</p>
      {footnote && <p className="card__footnote">{footnote}</p>}
    </article>
  )
}

export default function RiskCards({ report }) {
  return (
    <section className="section" aria-label="Headline risk metrics">
      <div className="cards">
        {METRICS.map((metric) => (
          <MetricCard key={metric.key} metric={metric} report={report} />
        ))}
      </div>
      <p className="section__note">
        Annualised return over the window: <strong>{percent(report.annualized_return)}</strong> ·
        risk-free rate {percent(report.params?.rf_per_period * report.params?.trading_days, 2)} ·
        {' '}{report.params?.trading_days} trading days/year
      </p>
    </section>
  )
}
