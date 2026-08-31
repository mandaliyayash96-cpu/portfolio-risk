/**
 * The positions behind every number on this page.
 *
 * `price_source` is shown because it changes how much to trust the valuation:
 * "live" is the latest poll, "last close" means the live fetch had nothing and
 * the most recent stored close was used instead. A judge asking "how current is
 * this?" should be able to see the answer, not be told it.
 *
 * WHERE THE DELETE BUTTON'S ID COMES FROM
 * ---------------------------------------
 * These rows come from the risk report, which describes a VALUATION - it has no
 * row ids, and it should not: it is a measurement, not a table of records. The
 * ids arrive separately from /api/portfolio/<id>/holdings/ and are joined here
 * by ticker, which is exactly the key the database already makes unique per
 * portfolio. A row whose id has not arrived yet simply renders no button rather
 * than a button that cannot work.
 */

import { useState } from 'react'

import { money, moneyPrecise, percent, quantity } from '../format'

const SOURCE_LABELS = {
  live: { text: 'Live', className: 'tag tag--live' },
  last_close: { text: 'Last close', className: 'tag tag--stale' },
}

export default function HoldingsTable({
  holdings = [],
  marketValue,
  // Both optional: with neither, this renders exactly the read-only table it
  // was before, which is what keeps the delete column from being load-bearing.
  holdingIds = {},
  onDelete,
}) {
  const [deleting, setDeleting] = useState(null)
  const [error, setError] = useState(null)
  const canDelete = typeof onDelete === 'function'

  async function remove(ticker) {
    const holdingId = holdingIds[ticker]
    if (!holdingId || deleting) return

    // A native confirm, deliberately. This is destructive and irreversible from
    // the UI's side, and a bespoke modal would be more code for a worse
    // guarantee - the browser's own dialog cannot be missed or mis-focused.
    if (!window.confirm(`Delete ${ticker} from this portfolio? This cannot be undone.`)) {
      return
    }

    setDeleting(ticker)
    setError(null)
    try {
      await onDelete(holdingId, ticker)
    } catch (apiError) {
      // Kept in this panel. A failed delete must not blank the dashboard.
      setError(apiError)
    } finally {
      setDeleting(null)
    }
  }

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Holdings</h2>
        <p className="panel__subtitle">{holdings.length} positions</p>
      </div>
      <div className="panel__body panel__body--scroll">
        {error && (
          <p className="banner banner--error" role="alert">
            <strong>Could not delete:</strong> {error.message}
          </p>
        )}
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Ticker</th>
              <th scope="col" className="table__num">Quantity</th>
              <th scope="col" className="table__num">Price</th>
              <th scope="col">Source</th>
              <th scope="col" className="table__num">Market value</th>
              <th scope="col" className="table__num">Weight</th>
              {canDelete && (
                <th scope="col" className="table__actions">
                  <span className="visually-hidden">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {holdings.map((holding) => {
              const source = SOURCE_LABELS[holding.price_source] ?? {
                text: holding.price_source,
                className: 'tag',
              }
              const holdingId = holdingIds[holding.ticker]
              return (
                <tr key={holding.ticker}>
                  <th scope="row" className="table__ticker">{holding.ticker}</th>
                  <td className="table__num">{quantity(holding.quantity)}</td>
                  <td className="table__num">{moneyPrecise(holding.price)}</td>
                  <td>
                    <span className={source.className}>{source.text}</span>
                  </td>
                  <td className="table__num">{money(holding.market_value)}</td>
                  <td className="table__num">
                    <div className="table__weight">
                      <span className="table__weight-value">{percent(holding.weight, 1)}</span>
                      <span className="table__weight-bar" aria-hidden="true">
                        <span
                          className="table__weight-fill"
                          style={{ width: `${Math.min(holding.weight * 100, 100)}%` }}
                        />
                      </span>
                    </div>
                  </td>
                  {canDelete && (
                    <td className="table__actions">
                      {holdingId ? (
                        <button
                          type="button"
                          className="button button--icon"
                          onClick={() => remove(holding.ticker)}
                          disabled={deleting !== null}
                          title={`Delete ${holding.ticker}`}
                        >
                          {deleting === holding.ticker ? '…' : '✕'}
                          <span className="visually-hidden">Delete {holding.ticker}</span>
                        </button>
                      ) : null}
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              <td colSpan={3} />
              <td className="table__num">{money(marketValue)}</td>
              <td className="table__num">100.0%</td>
              {canDelete && <td />}
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  )
}
