/**
 * The Markowitz suggestion: same holdings, better weights.
 *
 * Leads with minimum-variance because it is the one point on the frontier that
 * needs no return forecast - it follows from the covariance matrix alone, so it
 * is the honest thing to put in front of someone. Maximum-Sharpe depends on
 * historical means, which are far noisier, so it appears as a second marker and
 * a footnote rather than as the headline.
 *
 * Weight arrows are deliberately NOT red/green. Buying more of something is not
 * inherently good or bad, and this dashboard reserves red and green for risk.
 *
 * Failures are contained here: this panel renders its own error state so a
 * rebalance outage never blanks the risk dashboard around it.
 */

import {
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useChartColors } from '../theme-context'
import { percent } from '../format'

// Marker and frontier colours come from src/theme.js per theme; see
// `frontier` there.

/** Percentage points, e.g. 0.1816 -> 0.1754 is "-0.62 pp". */
const points = (from, to) => (to - from) * 100

/**
 * A larger, outlined dot so a single marked portfolio reads over the curve.
 *
 * The outline is the PANEL colour, not white: it exists to punch the marker out
 * of whatever sits behind it, which in dark mode is a near-black card. Recharts
 * clones this element with the scatter's props, so `surface` is passed in by
 * the caller rather than read from context here.
 */
function Marker({ cx, cy, fill, surface }) {
  if (cx == null || cy == null) return null
  return <circle cx={cx} cy={cy} r={7} fill={fill} stroke={surface} strokeWidth={2} />
}

function FrontierTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const point = payload[0].payload
  return (
    <div className="tooltip">
      <p className="tooltip__title">{point.label ?? 'Efficient frontier'}</p>
      <p className="tooltip__row">Risk {percent(point.risk)}</p>
      <p className="tooltip__row tooltip__row--muted">Return {percent(point.return)}</p>
    </div>
  )
}

function WeightRow({ ticker, current, suggested }) {
  const delta = points(current, suggested)
  const negligible = Math.abs(delta) < 0.05 // half a basis point either way

  return (
    <tr>
      <th scope="row" className="table__ticker">{ticker}</th>
      <td className="table__num">{percent(current, 1)}</td>
      <td className="table__num">{percent(suggested, 1)}</td>
      <td className="table__num">
        {negligible ? (
          <span className="delta delta--flat">no change</span>
        ) : (
          <span className={`delta ${delta > 0 ? 'delta--up' : 'delta--down'}`}>
            {delta > 0 ? '▲' : '▼'} {Math.abs(delta).toFixed(1)} pp
          </span>
        )}
      </td>
    </tr>
  )
}

export default function RebalanceCard({ data, error, isLoading }) {
  const colors = useChartColors()

  if (isLoading && !data) {
    return (
      <section className="panel">
        <div className="panel__head">
          <h2 className="panel__title">Rebalance suggestion</h2>
        </div>
        <div className="panel__body panel__placeholder">
          <span className="spinner" aria-hidden="true" />
          <p>Optimising…</p>
        </div>
      </section>
    )
  }

  if (error && !data) {
    return (
      <section className="panel">
        <div className="panel__head">
          <h2 className="panel__title">Rebalance suggestion</h2>
          <p className="panel__subtitle">Unavailable — the risk report above is unaffected</p>
        </div>
        <div className="panel__body panel__placeholder">
          <p className="status__badge">{error.code ?? 'error'}</p>
          <p className="status__detail">{error.message}</p>
        </div>
      </section>
    )
  }

  if (!data) return null

  const { current, min_variance: minVariance, max_sharpe: maxSharpe } = data
  const volatilityDelta = points(current.volatility, minVariance.volatility)

  // One coordinate space for the curve and the three marked portfolios: the
  // backend annualises all of them identically before they leave the service.
  const frontier = data.efficient_frontier.map((point) => ({ ...point, label: 'Frontier' }))
  const currentPoint = [
    { risk: current.volatility, return: current.expected_return, label: 'Current weights' },
  ]
  const minVariancePoint = [
    {
      risk: minVariance.volatility,
      return: minVariance.expected_return,
      label: 'Minimum variance',
    },
  ]
  const maxSharpePoint = [
    { risk: maxSharpe.volatility, return: maxSharpe.expected_return, label: 'Maximum Sharpe' },
  ]

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Rebalance suggestion</h2>
        <p className="panel__subtitle">
          Minimum-variance rebalance — same holdings, weights tuned to cut risk.
        </p>
      </div>

      <div className="rebalance">
        <div className="rebalance__left">
          <div className="rebalance__headline">
            <span className="rebalance__from">{percent(current.volatility)}</span>
            <span className="rebalance__arrow" aria-hidden="true">→</span>
            <span className="rebalance__to">{percent(minVariance.volatility)}</span>
            <span
              className={`rebalance__badge ${
                volatilityDelta < 0 ? 'rebalance__badge--good' : 'rebalance__badge--flat'
              }`}
            >
              {volatilityDelta < 0 ? '↓' : ''} {Math.abs(volatilityDelta).toFixed(2)} pp
            </span>
          </div>
          <p className="rebalance__caption">
            Annualised volatility, current weights versus minimum-variance weights.
          </p>

          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col">Ticker</th>
                <th scope="col" className="table__num">Current</th>
                <th scope="col" className="table__num">Suggested</th>
                <th scope="col" className="table__num">Change</th>
              </tr>
            </thead>
            <tbody>
              {data.tickers.map((ticker) => (
                <WeightRow
                  key={ticker}
                  ticker={ticker}
                  current={current.weights[ticker]}
                  suggested={minVariance.weights[ticker]}
                />
              ))}
            </tbody>
          </table>

          <p className="rebalance__foot">
            Maximum-Sharpe alternative: {percent(maxSharpe.volatility)} volatility at a Sharpe of{' '}
            {maxSharpe.sharpe.toFixed(2)} (current {current.sharpe.toFixed(2)}). It chases
            historical average returns, which are far noisier than volatility — treat it as the
            aggressive option.
          </p>
        </div>

        <div className="rebalance__right">
          <ResponsiveContainer width="100%" height={300}>
            <ScatterChart margin={{ top: 12, right: 20, bottom: 16, left: 4 }}>
              <CartesianGrid stroke={colors.grid} />
              <XAxis
                type="number"
                dataKey="risk"
                name="Risk"
                domain={['dataMin - 0.004', 'dataMax + 0.004']}
                tickFormatter={(value) => `${(value * 100).toFixed(0)}%`}
                tick={{ fill: colors.axis, fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: colors.axisLine }}
                label={{
                  value: 'Risk (annualised volatility)',
                  position: 'insideBottom',
                  offset: -6,
                  fill: colors.axis,
                  fontSize: 12,
                }}
              />
              <YAxis
                type="number"
                dataKey="return"
                name="Return"
                domain={['dataMin - 0.01', 'dataMax + 0.01']}
                tickFormatter={(value) => `${(value * 100).toFixed(0)}%`}
                tick={{ fill: colors.axis, fontSize: 12 }}
                tickLine={false}
                axisLine={false}
                width={54}
              />
              <Tooltip
                content={<FrontierTooltip />}
                cursor={{ strokeDasharray: '3 3', stroke: colors.axisLine }}
              />
              {/* No colour needed here: Recharts resolves each legend label to
                  its own series colour (DefaultLegendContent falls back to
                  `entry.color`), and those are already theme-aware. */}
              <Legend verticalAlign="top" height={28} iconType="circle" />
              <Scatter
                name="Efficient frontier"
                data={frontier}
                fill={colors.frontier.curve}
                line={{ stroke: colors.frontier.curve, strokeWidth: 2 }}
                isAnimationActive={false}
              />
              <Scatter
                name="Current"
                data={currentPoint}
                fill={colors.frontier.current}
                shape={<Marker surface={colors.surface} />}
                isAnimationActive={false}
              />
              <Scatter
                name="Min variance"
                data={minVariancePoint}
                fill={colors.frontier.minVariance}
                shape={<Marker surface={colors.surface} />}
                isAnimationActive={false}
              />
              <Scatter
                name="Max Sharpe"
                data={maxSharpePoint}
                fill={colors.frontier.maxSharpe}
                shape={<Marker surface={colors.surface} />}
                isAnimationActive={false}
              />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      </div>

      {data.warnings?.length > 0 && (
        <p className="panel__foot">{data.warnings.join(' ')}</p>
      )}
    </section>
  )
}
