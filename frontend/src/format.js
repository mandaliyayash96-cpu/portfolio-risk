/**
 * Presentation formatting. Nothing here changes a number's meaning - it only
 * decides how many digits reach a projector at the back of a hall.
 *
 * Money arrives from the API as a STRING (the backend keeps Decimals exact in
 * transit), so it is parsed here and nowhere else.
 */

const INR = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  maximumFractionDigits: 0,
})

const INR_PRECISE = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** True for anything we cannot render as a number - the API sends null for
 *  undefined matrix cells and for beta when no benchmark was stored. */
export const isMissing = (value) => value === null || value === undefined || Number.isNaN(value)

/** 0.1815 -> "18.15%". Ratios only; never call this on a currency amount. */
export function percent(value, digits = 2) {
  if (isMissing(value)) return '--'
  return `${(value * 100).toFixed(digits)}%`
}

/** 0.879 -> "0.88". For unitless ratios: beta, Sharpe, HHI. */
export function decimal(value, digits = 2) {
  if (isMissing(value)) return '--'
  return value.toFixed(digits)
}

/** "24275.9995000000" -> "₹24,276". Accepts the API's string or a number. */
export function money(value) {
  if (isMissing(value)) return '--'
  const amount = typeof value === 'string' ? Number.parseFloat(value) : value
  return Number.isFinite(amount) ? INR.format(amount) : '--'
}

/** Same, keeping paise - for per-unit prices, where rounding hides the tick. */
export function moneyPrecise(value) {
  if (isMissing(value)) return '--'
  const amount = typeof value === 'string' ? Number.parseFloat(value) : value
  return Number.isFinite(amount) ? INR_PRECISE.format(amount) : '--'
}

/** "10.000000" -> "10" / "0.500000" -> "0.5". Trailing zeros on a share count
 *  are DecimalField padding, not precision anyone needs to read. */
export function quantity(value) {
  if (isMissing(value)) return '--'
  const amount = typeof value === 'string' ? Number.parseFloat(value) : value
  return Number.isFinite(amount) ? String(amount) : '--'
}

/**
 * "2026-08-27 00:00:00" -> "2026-08-27".
 *
 * The engine stringifies a pandas Timestamp, which always carries a midnight
 * time component for a daily series. Trimmed here rather than in the engine:
 * `risk/engine.py` is pure and its output is tested, so the cosmetic fix
 * belongs on the presentation side.
 */
export function isoDate(value) {
  if (typeof value !== 'string') return '--'
  return value.split(' ')[0]
}

/** A clock for the poll indicator - "14:32:05" in the viewer's locale. */
export function clockTime(date) {
  if (!date) return '--'
  return date.toLocaleTimeString([], { hour12: false })
}
