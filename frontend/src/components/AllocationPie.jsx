/**
 * Allocation by market value.
 *
 * Weights come from `portfolio.holdings`, which the backend already computed as
 * value weights (quantity x latest price / total). Recomputing them here from
 * prices would be a second source of truth for the same number.
 */

import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'

import { useChartColors } from '../theme-context'
import { money, percent } from '../format'

// The categorical ramp lives in src/theme.js, which holds a separate set of
// hues for dark mode - the light ramp's deeper blues and teals go muddy against
// a near-black panel.

function SliceTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const holding = payload[0].payload
  return (
    <div className="tooltip">
      <p className="tooltip__title">{holding.ticker}</p>
      <p className="tooltip__row">{percent(holding.weight)} of portfolio</p>
      <p className="tooltip__row tooltip__row--muted">{money(holding.market_value)}</p>
    </div>
  )
}

export default function AllocationPie({ holdings = [] }) {
  const colors = useChartColors()

  const data = holdings.map((holding) => ({
    ...holding,
    // Recharts needs a number to size the arc; `weight` is already a fraction.
    value: holding.weight,
  }))

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Allocation</h2>
        <p className="panel__subtitle">Share of market value, by holding</p>
      </div>
      <div className="panel__body panel__body--chart">
        <ResponsiveContainer width="100%" height={280}>
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              nameKey="ticker"
              cx="50%"
              cy="50%"
              innerRadius={58}
              outerRadius={104}
              paddingAngle={2}
              stroke={colors.surface}
              strokeWidth={2}
              isAnimationActive={false}
              label={({ ticker, weight }) => `${ticker} ${percent(weight, 1)}`}
              labelLine={false}
              // Recharts defaults slice labels to a near-black fill, which is
              // invisible on a dark panel. These sit OUTSIDE the arcs, on the
              // card, so they take the body ink rather than the slice colour.
              fill={colors.axis}
            >
              {data.map((holding, index) => (
                <Cell
                  key={holding.ticker}
                  fill={colors.categorical[index % colors.categorical.length]}
                />
              ))}
            </Pie>
            <Tooltip content={<SliceTooltip />} />
            <Legend verticalAlign="bottom" height={24} iconType="circle" />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </section>
  )
}
