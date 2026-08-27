/**
 * Three ways of estimating the same thing, side by side.
 *
 * Historical replays what actually happened, parametric assumes a normal
 * distribution, and Monte Carlo simulates from the covariance matrix. When the
 * three agree the estimate is robust; when historical sits well above the other
 * two, the real return series has fatter tails than a normal curve admits -
 * which is the interesting case, and the reason all three are on screen.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useChartColors } from '../theme-context'
import { percent } from '../format'

// Colour is looked up per theme at render time (see src/theme.js), so only the
// key and the label are fixed here.
const METHODS = [
  { key: 'var_historical', name: 'Historical', tone: 'historical' },
  { key: 'var_parametric', name: 'Parametric', tone: 'parametric' },
  { key: 'var_montecarlo', name: 'Monte Carlo', tone: 'montecarlo' },
]

function VarTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const entry = payload[0].payload
  return (
    <div className="tooltip">
      <p className="tooltip__title">{entry.name} VaR</p>
      <p className="tooltip__row">{percent(entry.loss)} of portfolio value</p>
    </div>
  )
}

export default function VarChart({ report }) {
  const colors = useChartColors()

  const data = METHODS.map((method) => ({
    name: method.name,
    color: colors.var[method.tone],
    loss: report[method.key],
    // Recharts plots the axis in percentage points so the ticks read naturally.
    lossPct: report[method.key] * 100,
  }))

  const confidence = report.params?.confidence ?? 0.95

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Value at Risk</h2>
        <p className="panel__subtitle">
          1-day loss at {percent(confidence, 0)} confidence · three estimates
        </p>
      </div>
      <div className="panel__body panel__body--chart">
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
            <CartesianGrid stroke={colors.grid} vertical={false} />
            <XAxis
              dataKey="name"
              tick={{ fill: colors.axis, fontSize: 13 }}
              tickLine={false}
              axisLine={{ stroke: colors.axisLine }}
            />
            <YAxis
              tick={{ fill: colors.axis, fontSize: 13 }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => `${value.toFixed(1)}%`}
              width={52}
            />
            <Tooltip content={<VarTooltip />} cursor={{ fill: colors.cursor }} />
            <Bar dataKey="lossPct" radius={[6, 6, 0, 0]} isAnimationActive={false} maxBarSize={78}>
              {data.map((entry) => (
                <Cell key={entry.name} fill={entry.color} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="panel__foot">
        CVaR {percent(report.cvar)} — the average loss on days worse than VaR.
      </p>
    </section>
  )
}
