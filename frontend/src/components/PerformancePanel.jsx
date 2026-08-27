/**
 * What the portfolio actually did, over the same window everything else on this
 * page measures.
 *
 * Two charts, stacked and sharing one x domain, because they answer two halves
 * of one question. The value curve says how the portfolio grew; the underwater
 * chart says what that growth cost on the way - and a drawdown is invisible in
 * a rising curve until you plot it against its own running peak.
 *
 * Design notes worth keeping:
 *
 *   * The value axis is NOT floored at zero. The curve is a rebased index, not
 *     a rupee amount, so a zero baseline would compress every real move into a
 *     flat line near the top of the plot. The solid reference line at 100 is
 *     what keeps a truncated axis honest: it marks "no change", so a reader can
 *     see at a glance which side of the start the curve is on.
 *   * The underwater chart IS floored - at 0, its true maximum - and fills
 *     downward from there. That baseline is meaningful, so it stays.
 *   * Only the bottom chart carries x-axis labels. Both use the same y-axis
 *     width and margins, so their plot areas line up and one date ruler reads
 *     for both.
 *   * Red is used deliberately here. This dashboard reserves red and green for
 *     risk, and a drawdown is risk - unlike the rebalance weights, which are
 *     neither good nor bad and are therefore not tinted.
 *
 * Like <RebalanceCard>, this panel renders its own loading and error states. A
 * performance outage costs you this card and nothing else on the page.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useChartColors } from '../theme-context'
import { decimal, percent } from '../format'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * "2026-03-09" -> "9 Mar".
 *
 * Split rather than `new Date(iso)`: the string form is parsed as UTC midnight
 * and then rendered in local time, which silently shows the previous day for
 * anyone west of Greenwich. The API already sent the date it means.
 */
function shortDate(iso) {
  const [, month, day] = iso.split('-')
  return `${Number(day)} ${MONTHS[Number(month) - 1]}`
}

/** Same parse, with the year - for tooltips, where there is room. */
function longDate(iso) {
  const [year, month, day] = iso.split('-')
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`
}

function ValueTooltip({ active, payload, baseline }) {
  if (!active || !payload?.length) return null
  const point = payload[0].payload
  const change = point.value / baseline - 1

  return (
    <div className="tooltip">
      <p className="tooltip__title">{longDate(point.date)}</p>
      <p className="tooltip__row">{decimal(point.value)} rebased</p>
      <p className="tooltip__row tooltip__row--muted">
        {change >= 0 ? '+' : ''}
        {percent(change)} since the start of the window
      </p>
    </div>
  )
}

function DrawdownTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const point = payload[0].payload

  return (
    <div className="tooltip">
      <p className="tooltip__title">{longDate(point.date)}</p>
      <p className="tooltip__row">{decimal(point.drawdown)}% below the peak</p>
      {point.drawdown === 0 && <p className="tooltip__row tooltip__row--muted">At a new high</p>}
      {point.drawdown !== 0 && point.peak !== null && (
        <p className="tooltip__row tooltip__row--muted">Peak was {decimal(point.peak)}</p>
      )}
    </div>
  )
}

/**
 * The peak a given date's drawdown is measured against, recovered from the two
 * series the API already sends rather than by a second scan for running maxima.
 *
 *     dd = value / peak - 1   ->   peak = value / (1 + dd)
 *
 * A total loss (dd = -100%) would divide by zero; there is no peak to name in
 * that case, so the tooltip says nothing rather than "Infinity".
 */
function peakBehind(value, drawdownPercent) {
  const ratio = 1 + drawdownPercent / 100
  return ratio > 0 ? value / ratio : null
}

/**
 * The one direct label on either chart: a chip callout on the deepest trough.
 *
 * Drawn as SVG rather than a Recharts <Label> string so it can sit on a filled
 * surface chip - the trough is the densest part of the red wash, and plain text
 * there would be unreadable in either theme. The text itself wears ink, not the
 * series colour; the 1px red edge is what ties the chip to its point.
 *
 * `align` shifts the chip left or right of the dot instead of centring it, so a
 * trough at either end of the window cannot push the label out of the plot -
 * the label viewBox carries the dot's position but not the plot's width, so
 * there is nothing to clamp against.
 *
 * Recharts hands a ReferenceDot's label the DOT'S BOUNDING BOX, not its centre:
 * `x`/`y` are its top-left corner. Hence the half-width below.
 */
function DeepestLabel({ viewBox, text, align, colors }) {
  if (!viewBox) return null

  const width = text.length * 7.4 + 18
  const height = 22
  const centre = viewBox.x + (viewBox.width ?? 0) / 2
  const offset = { start: -8, middle: -width / 2, end: 8 - width }[align]
  const x = centre + offset
  const y = Math.max(2, viewBox.y - height - 10)

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={6}
        fill={colors.surface}
        stroke={colors.performance.drawdown}
        strokeWidth={1}
      />
      <text
        x={x + width / 2}
        y={y + height / 2 + 0.5}
        textAnchor="middle"
        dominantBaseline="middle"
        fill={colors.performance.label}
        fontSize={12}
        fontWeight={600}
      >
        {text}
      </text>
    </g>
  )
}

function Shell({ children, subtitle }) {
  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Performance</h2>
        {subtitle && <p className="panel__subtitle">{subtitle}</p>}
      </div>
      <div className="panel__body panel__placeholder">{children}</div>
    </section>
  )
}

export default function PerformancePanel({ data, error, isLoading }) {
  const colors = useChartColors()

  if (isLoading && !data) {
    return (
      <Shell>
        <span className="spinner" aria-hidden="true" />
        <p>Building the value curve…</p>
      </Shell>
    )
  }

  if (error && !data) {
    return (
      <Shell subtitle="Unavailable — the rest of the dashboard is unaffected">
        <p className="status__badge">{error.code ?? 'error'}</p>
        <p className="status__detail">{error.message}</p>
      </Shell>
    )
  }

  if (!data?.dates?.length) return null

  const baseline = data.start_value ?? 100
  const series = data.dates.map((date, index) => ({
    date,
    value: data.equity_curve[index],
    drawdown: data.drawdown_series[index],
    // Carried along so the drawdown tooltip can name the peak the fall is
    // measured from, without a second pass over the data at render time.
    peak: peakBehind(data.equity_curve[index], data.drawdown_series[index]),
  }))

  // Padded so the curve never touches the frame, and always wide enough to
  // include the 100 line even for a window that spent all of it underwater.
  const low = Math.min(baseline, ...data.equity_curve)
  const high = Math.max(baseline, ...data.equity_curve)
  const pad = Math.max((high - low) * 0.08, 0.5)

  const deepest = Math.min(0, ...data.drawdown_series)
  const deepestIndex = data.drawdown_series.indexOf(deepest)
  // A portfolio that only ever rose has no trough to point at; the flat zero
  // line is the whole story and a dot on it would invent an event.
  const hasTrough = deepest < 0
  const troughPosition = deepestIndex / Math.max(data.dates.length - 1, 1)
  const troughAlign = troughPosition > 0.8 ? 'end' : troughPosition < 0.2 ? 'start' : 'middle'

  const axis = {
    tick: { fill: colors.axis, fontSize: 12 },
    tickLine: false,
  }

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Performance</h2>
        <p className="panel__subtitle">
          {shortDate(data.start)} – {shortDate(data.end)} · {data.observations} trading days
        </p>
        <dl className="performance__stats">
          <div className="performance__stat">
            <dt>Current</dt>
            <dd>{decimal(data.current_value)}</dd>
          </div>
          <div className="performance__stat">
            <dt>Peak</dt>
            <dd>{decimal(data.peak_value)}</dd>
          </div>
          <div className="performance__stat performance__stat--bad">
            <dt>Max drawdown</dt>
            <dd>{percent(data.max_drawdown)}</dd>
          </div>
        </dl>
      </div>

      <div className="panel__body panel__body--chart">
        <p className="performance__caption">Portfolio value (rebased to 100)</p>
        <ResponsiveContainer width="100%" height={250}>
          <AreaChart data={series} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="performance-value" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.performance.value} stopOpacity={0.22} />
                <stop offset="100%" stopColor={colors.performance.value} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={colors.grid} vertical={false} />
            <XAxis dataKey="date" hide />
            <YAxis
              {...axis}
              width={56}
              axisLine={false}
              domain={[low - pad, high + pad]}
              tickFormatter={(value) => value.toFixed(0)}
            />
            {/* Where the window started. A truncated value axis is only honest
                while this line is on it. Drawn in axis ink rather than grid
                ink so it reads as a statement and not as one more gridline,
                and left unlabelled: the caption above already says "rebased to
                100", and a label pinned to the right edge would be the first
                thing clipped on a narrow screen. */}
            <ReferenceLine y={baseline} stroke={colors.axis} strokeWidth={1} />
            <Tooltip
              content={<ValueTooltip baseline={baseline} />}
              cursor={{ stroke: colors.axisLine, strokeWidth: 1 }}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={colors.performance.value}
              strokeWidth={2}
              fill="url(#performance-value)"
              isAnimationActive={false}
              activeDot={{ r: 4, stroke: colors.surface, strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>

        <p className="performance__caption">Drawdown from the running peak — underwater</p>
        <ResponsiveContainer width="100%" height={210}>
          <AreaChart data={series} margin={{ top: 26, right: 16, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="performance-drawdown" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.performance.drawdown} stopOpacity={0.06} />
                <stop offset="100%" stopColor={colors.performance.drawdown} stopOpacity={0.3} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={colors.grid} vertical={false} />
            <XAxis
              {...axis}
              dataKey="date"
              axisLine={{ stroke: colors.axisLine }}
              tickFormatter={shortDate}
              minTickGap={44}
            />
            <YAxis
              {...axis}
              width={56}
              axisLine={false}
              domain={[hasTrough ? deepest * 1.18 : -1, 0]}
              tickFormatter={(value) => `${value.toFixed(0)}%`}
            />
            {/* Zero is the real maximum of this series, not a chosen crop. */}
            <ReferenceLine y={0} stroke={colors.axisLine} strokeWidth={1} />
            <Tooltip
              content={<DrawdownTooltip />}
              cursor={{ stroke: colors.axisLine, strokeWidth: 1 }}
            />
            <Area
              type="monotone"
              dataKey="drawdown"
              baseValue={0}
              stroke={colors.performance.drawdown}
              strokeWidth={2}
              fill="url(#performance-drawdown)"
              isAnimationActive={false}
              activeDot={{ r: 4, stroke: colors.surface, strokeWidth: 2 }}
            />
            {hasTrough && (
              <ReferenceDot
                x={data.dates[deepestIndex]}
                y={deepest}
                r={5}
                fill={colors.performance.drawdown}
                stroke={colors.surface}
                strokeWidth={2}
                isFront
                label={
                  <DeepestLabel
                    text={`${percent(data.max_drawdown)} on ${shortDate(data.dates[deepestIndex])}`}
                    align={troughAlign}
                    colors={colors}
                  />
                }
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <p className="panel__foot">
        Both charts are built from the same aligned returns as the risk report, so the max drawdown
        here is the figure that report shows. The curve opens one trading day into the window — a
        return series has no row for the day it is measured from.
      </p>
    </section>
  )
}
