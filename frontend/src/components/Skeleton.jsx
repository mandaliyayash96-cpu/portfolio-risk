/**
 * Loading placeholders, shaped like the thing that is coming.
 *
 * WHY NOT A SPINNER
 * -----------------
 * A spinner in the middle of an empty page says "wait" and nothing else. On a
 * first load this dashboard is waiting on four endpoints, one of which runs a
 * Monte Carlo simulation, so "wait" can last several seconds - long enough for
 * a blank page to read as a broken one. A skeleton says "wait, and here is what
 * is arriving", and because these shapes match the real layout the page does
 * not jump when it does.
 *
 * The spinner has not gone away; it moved to where it belongs. Inside a button,
 * or beside a line of text that names what is happening, a spinner is exactly
 * right - it is a small thing that is busy, not a whole region that is empty.
 *
 * ACCESSIBILITY
 * -------------
 * The shapes are decorative: they carry `aria-hidden`, and every skeleton
 * region announces itself once through a `role="status"` label instead. A
 * screen reader hears "Loading the risk report" rather than sixteen empty divs.
 */

/** One shimmering block. `variant` picks a height from the type scale. */
export function Skeleton({ variant = 'text', width, style, className = '' }) {
  return (
    <span
      className={`skeleton skeleton--${variant} ${className}`.trim()}
      style={{ width, display: 'block', ...style }}
      aria-hidden="true"
    />
  )
}

/**
 * The whole dashboard, before the first report lands.
 *
 * Mirrors the real page: the summary card, the tab strip, then the metric grid.
 * Eight cards because eight is what <RiskCards> renders - a skeleton that shows
 * four and then paints eight is a layout jump with extra steps.
 */
export function DashboardSkeleton() {
  return (
    <main className="page" role="status" aria-live="polite">
      <span className="visually-hidden">Computing your risk report…</span>

      <div className="skeleton-header" aria-hidden="true">
        <Skeleton variant="text" width="14rem" />
        <Skeleton variant="title" width="20rem" />
        <Skeleton variant="value" width="12rem" />
      </div>

      <Skeleton variant="title" style={{ height: '3rem', borderRadius: 'var(--radius-md)' }} />

      <div className="cards">
        {Array.from({ length: 8 }, (_, index) => (
          <div className="skeleton-card" key={index} aria-hidden="true">
            <Skeleton variant="text" width="7rem" />
            <Skeleton variant="value" width="60%" />
            <Skeleton variant="text" />
            <Skeleton variant="text" width="80%" />
          </div>
        ))}
      </div>
    </main>
  )
}

/**
 * One panel's worth, for a chart that has not arrived.
 *
 * Used where a panel is still loading while the rest of the page is already
 * live - the performance curve and the rebalance suggestion, both of which are
 * fetched alongside the risk report but can be slower than it.
 */
export function PanelSkeleton({ title, subtitle }) {
  return (
    <section className="panel" role="status" aria-live="polite">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">{title}</h2>
          {subtitle && <p className="panel__subtitle">{subtitle}</p>}
        </div>
      </div>
      <div className="panel__body">
        <span className="visually-hidden">Loading {title}…</span>
        <Skeleton variant="chart" />
      </div>
    </section>
  )
}
