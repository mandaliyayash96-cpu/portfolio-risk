/**
 * Allocation by market value.
 *
 * Weights come from `portfolio.holdings`, which the backend already computed as
 * value weights (quantity x latest price / total). Recomputing them here from
 * prices would be a second source of truth for the same number.
 */

import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'

import { money, percent } from '../format'

/**
 * A categorical ramp that stays distinguishable on a washed-out projector and
 * in greyscale print. Deliberately not the red/green used for risk tone -
 * allocation slices carry no good/bad meaning.
 */
const SLICE_COLORS = [
  '#2563eb',
  '#0891b2',
  '#7c3aed',
  '#c2410c',
  '#0f766e',
  '#a21caf',
  '#4d7c0f',
  '#b45309',
]

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
              stroke="#ffffff"
              strokeWidth={2}
              isAnimationActive={false}
              label={({ ticker, weight }) => `${ticker} ${percent(weight, 1)}`}
              labelLine={false}
            >
              {data.map((holding, index) => (
                <Cell
                  key={holding.ticker}
                  fill={SLICE_COLORS[index % SLICE_COLORS.length]}
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
