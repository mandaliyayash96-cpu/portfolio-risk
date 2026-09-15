/**
 * Allocation by market value.
 *
 * Weights come from `portfolio.holdings`, which the backend already computed as
 * value weights (quantity x latest price / total). Recomputing them here from
 * prices would be a second source of truth for the same number.
 *
 * WHY THE NAMES ARE NOT ON THE CHART
 * ----------------------------------
 * This used to put "TICKER 12.3%" beside every slice, outside the ring. That
 * reads at four holdings and fails at twelve: the labels for the slices near
 * the top and bottom of the ring stack on top of one another (Recharts does no
 * collision avoidance) and the outermost ones run past the panel edge, where
 * `.panel { overflow: hidden }` cuts them off. More height or a smaller ring
 * moves the problem; it does not remove it, because twelve labels around a
 * circle need more circumference than any card has.
 *
 * So the ring carries only what fits INSIDE it - the percentage, on every slice
 * wide enough to hold one - and the names live in a list beside it, which is
 * the legend. A list has no geometry problem: fifteen rows are fifteen rows.
 * Both are sorted largest-first, so the ring reads clockwise in the same order
 * the list reads down, and the biggest slice takes the first hue of the ramp,
 * which is the brand emerald (the ramp in theme.js was designed on exactly that
 * assumption, and alphabetical order had been quietly breaking it).
 *
 * THE RAMP HAS EIGHT HUES AND A PORTFOLIO MAY HAVE MORE
 * ----------------------------------------------------
 * Past eight, hues repeat. Rather than widen the ramp - eight is as many as
 * stay distinguishable on a projector, and that is a property of the ramp
 * worth keeping - the second cycle is SHADED: darker in light mode, lighter in
 * dark. The shade is chosen so the on-slice text keeps its contrast in both
 * themes, and it is applied to the list's swatch too, so the row and the slice
 * still match. The list, not the colour, is what makes any row unambiguous.
 */

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'

import { useChartColors, useTheme } from '../theme-context'
import { money, percent } from '../format'

/** Height of the chart box. The ring scales to it (see the radii below). */
const CHART_HEIGHT = 300

/**
 * Below this share a slice is too narrow for its own label. At the radii used
 * here a 5% arc is about 33px long at mid-ring, and "5.0%" set in 11px is about
 * 28px - the smallest slice that gets a label is also the tightest fit. Under
 * it, the number is in the list and only in the list.
 */
const MIN_LABELLED_SHARE = 0.05

/**
 * Past this many rows the list flows into two columns. Six is where a single
 * column starts to look like a table with one very wide first cell, and
 * seven-plus in two columns keeps the list level with the 300px chart.
 */
const TWO_COLUMN_THRESHOLD = 6

/**
 * How far the second cycle of the ramp is pushed toward black (light theme)
 * or white (dark theme). Enough to tell a slice from its first-cycle twin
 * across the ring; not so far that white / near-black text stops reading.
 */
const REPEAT_SHADE = 0.32

const RADIAN = Math.PI / 180

/**
 * Mix a hex colour toward black (amount < 0) or white (amount > 0).
 *
 * Deliberately not `fillOpacity`: a translucent slice lets the panel through,
 * which in light mode washes it toward white and takes the white label's
 * contrast with it. Mixing the fill itself keeps the slice opaque.
 */
function shade(hex, amount) {
  const target = amount < 0 ? 0 : 255
  const weight = Math.abs(amount)
  const channel = (offset) => {
    const value = Number.parseInt(hex.slice(offset, offset + 2), 16)
    return Math.round(value + (target - value) * weight)
      .toString(16)
      .padStart(2, '0')
  }
  return `#${channel(1)}${channel(3)}${channel(5)}`
}

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

/**
 * The percentage, set at mid-ring on the slice's own colour.
 *
 * Recharts hands the sector's geometry in (cx, cy, midAngle, middleRadius are
 * all resolved pixels) and `value` is the weight. Rendered as our own <text>
 * so the fill can come from the stylesheet - `--on-brand` is white on the
 * light ramp and near-black on the dark one, which is exactly the pairing
 * these slices need and one that no single hex could give both themes.
 */
function SliceLabel({ cx, cy, midAngle, middleRadius, value }) {
  if (value < MIN_LABELLED_SHARE) return null
  const x = cx + middleRadius * Math.cos(-midAngle * RADIAN)
  const y = cy + middleRadius * Math.sin(-midAngle * RADIAN)
  return (
    <text
      x={x}
      y={y}
      textAnchor="middle"
      dominantBaseline="central"
      className="allocation__slice-label"
    >
      {percent(value, 1)}
    </text>
  )
}

export default function AllocationPie({ holdings = [] }) {
  const colors = useChartColors()
  const { isDark } = useTheme()
  const ramp = colors.categorical

  // Largest first. Presentation only - the holdings array is not mutated, and
  // nothing here changes a weight.
  const rows = [...holdings]
    .sort((a, b) => b.weight - a.weight)
    .map((holding, index) => {
      const base = ramp[index % ramp.length]
      const isRepeat = index >= ramp.length
      return {
        ...holding,
        // Recharts needs a number to size the arc; `weight` is already a fraction.
        value: holding.weight,
        fill: isRepeat ? shade(base, isDark ? REPEAT_SHADE : -REPEAT_SHADE) : base,
      }
    })

  const listClass = `allocation__list${
    rows.length > TWO_COLUMN_THRESHOLD ? ' allocation__list--dense' : ''
  }`

  return (
    <section className="panel allocation">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">Allocation</h2>
          <p className="panel__subtitle">Share of market value, by holding — largest first</p>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="panel__body panel__placeholder">
          <p className="status__detail">No priced holdings to allocate.</p>
        </div>
      ) : (
        <div className="panel__body allocation__body">
          <div className="allocation__chart">
            <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
              <PieChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
                <Pie
                  data={rows}
                  dataKey="value"
                  nameKey="ticker"
                  cx="50%"
                  cy="50%"
                  // Percentages of the chart box rather than pixels: the ring can
                  // never be larger than the container it is drawn in, whatever
                  // width the panel ends up at, so nothing is left to clip.
                  innerRadius="58%"
                  outerRadius="92%"
                  paddingAngle={1.5}
                  stroke={colors.surface}
                  strokeWidth={2}
                  isAnimationActive={false}
                  label={SliceLabel}
                  labelLine={false}
                >
                  {rows.map((row) => (
                    <Cell key={row.ticker} fill={row.fill} />
                  ))}
                </Pie>
                <Tooltip content={<SliceTooltip />} />
              </PieChart>
            </ResponsiveContainer>

            {/* The hole in the donut, put to use. Absolutely centred over the
                chart box; the ring is centred in the same box, so the two
                agree. pointer-events: none keeps the slices' tooltips live. */}
            <div className="allocation__center" aria-hidden="true">
              <span className="allocation__count">{rows.length}</span>
              <span className="allocation__count-label">
                {rows.length === 1 ? 'holding' : 'holdings'}
              </span>
            </div>
          </div>

          <ol className={listClass} aria-label="Allocation by holding, largest first">
            {rows.map((row) => (
              <li key={row.ticker} className="allocation__row">
                <span
                  className="allocation__swatch"
                  style={{ background: row.fill }}
                  aria-hidden="true"
                />
                <span className="allocation__ticker">{row.ticker}</span>
                <span className="allocation__weight">{percent(row.weight, 1)}</span>
                <span className="allocation__value">{money(row.market_value)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  )
}
