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

import { decimal, isMissing } from '../format'

/**
 * Correlation in [-1, 1] -> a colour.
 *
 * Hue runs green (155) to red (2) and saturation rises with |r|, so a value
 * near zero is nearly white and the extremes are unmistakable. Lightness is
 * pinned high enough that dark text stays readable at projector contrast.
 */
function cellStyle(value) {
  if (isMissing(value)) return { background: 'var(--surface-sunken)', color: 'var(--ink-muted)' }

  const clamped = Math.max(-1, Math.min(1, value))
  const strength = Math.abs(clamped)
  const hue = clamped >= 0 ? 155 - clamped * 153 : 155
  const saturation = 20 + strength * 55
  const lightness = 96 - strength * 33

  return {
    background: `hsl(${hue} ${saturation}% ${lightness}%)`,
    // Below ~68% lightness the cell is dark enough that ink-on-colour loses.
    color: lightness < 68 ? '#ffffff' : 'var(--ink)',
  }
}

export default function CorrelationHeatmap({ matrix = {} }) {
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
                      style={isDiagonal ? undefined : cellStyle(value)}
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
