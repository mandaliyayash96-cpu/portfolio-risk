/**
 * Correlation matrix as a heatmap.
 *
 * Read it as a diversification check: green pairs move independently (the
 * portfolio spreads risk), red pairs move together (a bad day hits both at
 * once). The diagonal is 1.0 by definition and is drawn muted so the eye skips
 * it and lands on the off-diagonal cells, which are the only informative ones.
 *
 * The API sends `null` for a cell it could not compute (a constant series has
 * no correlation with anything); those render as a dash rather than a
 * fabricated 0.
 */

import { useTheme } from '../theme-context'
import { decimal, isMissing } from '../format'

/* The two candidate text colours for a coloured cell. Every cell gets whichever
   of these contrasts better against it - see `pickInk`.

   They mirror --ink (light) and --page (light) from index.css, but they are NOT
   theme-dependent and must not become so: a cell's background is an hsl() the
   component computed itself, so the readable ink is a property of that cell,
   not of the page it sits on. */
const DARK_INK = '#0f1d18'
const LIGHT_INK = '#f5f9f7'

/** One sRGB channel, 0-255 -> its linear-light value (WCAG 2.1). */
function linearise(channel) {
  const c = channel / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

/** Relative luminance of an #rrggbb colour. */
function hexLuminance(hex) {
  const int = Number.parseInt(hex.slice(1), 16)
  const r = linearise((int >> 16) & 255)
  const g = linearise((int >> 8) & 255)
  const b = linearise(int & 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Relative luminance straight from HSL, without a round trip through a string. */
function hslLuminance(hue, saturation, lightness) {
  const s = saturation / 100
  const l = lightness / 100
  const chroma = (1 - Math.abs(2 * l - 1)) * s
  const second = chroma * (1 - Math.abs(((hue / 60) % 2) - 1))
  const offset = l - chroma / 2

  const sector = Math.floor(hue / 60) % 6
  const [r, g, b] = [
    [chroma, second, 0],
    [second, chroma, 0],
    [0, chroma, second],
    [0, second, chroma],
    [second, 0, chroma],
    [chroma, 0, second],
  ][sector]

  return (
    0.2126 * linearise((r + offset) * 255) +
    0.7152 * linearise((g + offset) * 255) +
    0.0722 * linearise((b + offset) * 255)
  )
}

const DARK_INK_LUMINANCE = hexLuminance(DARK_INK)
const LIGHT_INK_LUMINANCE = hexLuminance(LIGHT_INK)

const contrast = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)

/**
 * The more readable of the two inks on a given cell.
 *
 * This replaces a lightness threshold, which cannot work: HSL lightness is not
 * perceived brightness, and the gap is widest exactly where this matrix puts
 * its most important cells. `hsl(155 75% 63%)` and `hsl(2 75% 63%)` share a
 * lightness, but the green is roughly four times as luminous as the red - so a
 * single cutoff was picking white text on a bright green at 1.56:1, effectively
 * erasing the strongest positive correlations on the board. Measuring the cell
 * and comparing both candidates gets every cell in both themes above 4.6:1.
 */
function pickInk(hue, saturation, lightness) {
  const cell = hslLuminance(hue, saturation, lightness)
  return contrast(cell, DARK_INK_LUMINANCE) >= contrast(cell, LIGHT_INK_LUMINANCE)
    ? DARK_INK
    : LIGHT_INK
}

/**
 * Correlation in [-1, 1] -> a colour.
 *
 * Hue runs green (155) to red (2) and saturation rises with |r|, so a pair that
 * barely correlates nearly vanishes into the panel and the extremes are
 * unmistakable. HUE AND SATURATION ARE THE SAME IN BOTH THEMES - only the
 * lightness band moves, and it has to.
 *
 * The light ramp runs 63-96%: pale cells. Reused as-is in dark mode that
 * becomes the single brightest object on the page - a glowing white grid on a
 * near-black dashboard. The dark ramp inverts it to 10-28%, so "weak
 * correlation" reads as "melts into the background" in both, which is the
 * property that actually matters when you scan the matrix.
 *
 * The band comes from HEATMAP_RAMP in src/theme.js; the text colour is measured
 * per cell rather than themed.
 */
function cellStyle(value, ramp) {
  if (isMissing(value)) return { background: 'var(--surface-sunken)', color: 'var(--ink-muted)' }

  const clamped = Math.max(-1, Math.min(1, value))
  const strength = Math.abs(clamped)
  const hue = clamped >= 0 ? 155 - clamped * 153 : 155
  const saturation = 20 + strength * 55
  // Light: 96 - strength*33 (falls as it saturates).
  // Dark:  10 + strength*18 (rises). The direction follows from `base`.
  const lightness =
    ramp.base > 50 ? ramp.base - strength * ramp.span : ramp.base + strength * ramp.span

  return {
    background: `hsl(${hue} ${saturation}% ${lightness}%)`,
    color: pickInk(hue, saturation, lightness),
  }
}

export default function CorrelationHeatmap({ matrix = {} }) {
  const { heatmapRamp } = useTheme()
  const tickers = Object.keys(matrix)

  if (tickers.length === 0) {
    return null
  }

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Correlation</h2>
        <p className="panel__subtitle">
          Green moves independently · red moves together
        </p>
      </div>
      <div className="panel__body panel__body--scroll">
        <table className="heatmap">
          <thead>
            <tr>
              <th scope="col" className="heatmap__corner">
                <span className="visually-hidden">Ticker</span>
              </th>
              {tickers.map((ticker) => (
                <th key={ticker} scope="col" className="heatmap__head">
                  {ticker}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tickers.map((row) => (
              <tr key={row}>
                <th scope="row" className="heatmap__head heatmap__head--row">
                  {row}
                </th>
                {tickers.map((column) => {
                  const value = matrix[row]?.[column]
                  const isDiagonal = row === column
                  return (
                    <td
                      key={column}
                      className={`heatmap__cell ${isDiagonal ? 'heatmap__cell--diagonal' : ''}`}
                      style={isDiagonal ? undefined : cellStyle(value, heatmapRamp)}
                      title={`${row} vs ${column}: ${decimal(value, 3)}`}
                    >
                      {decimal(value, 2)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
