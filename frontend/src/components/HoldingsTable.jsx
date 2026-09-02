/**
 * The positions behind every number on this page.
 *
 * `price_source` is shown because it changes how much to trust the valuation:
 * "live" is the latest poll, "last close" means the live fetch had nothing and
 * the most recent stored close was used instead. A judge asking "how current is
 * this?" should be able to see the answer, not be told it.
 *
 * TWO KINDS OF ROW
 * ----------------
 * The report hands back two lists: the positions it measured, and the ones it
 * could not price. Both are rendered here, because the second kind is exactly
 * the row a user needs to see and act on - it is the delisted ticker, or the
 * typo. The measured rows carry a price and a weight; the excluded ones carry a
 * "No price" tag and dashes, and they keep their delete button.
 *
 * The totals row describes the measured half only, and says so when there is a
 * difference.
 *
 * WHERE THE DELETE BUTTON'S ID COMES FROM
 * ---------------------------------------
 * These rows come from the risk report, which describes a VALUATION - it has no
 * row ids, and it should not: it is a measurement, not a table of records. The
 * ids arrive separately from /api/portfolio/<id>/holdings/ and are joined here
 * by ticker, which is exactly the key the database already makes unique per
 * portfolio. A row whose id has not arrived yet simply renders no button rather
 * than a button that cannot work.
 *
 * DELETING COSTS ₹9, AND THIS COMPONENT DOES NOT KNOW THAT
 * --------------------------------------------------------
 * `onDelete` is a gated write - the dashboard wraps it so that a 402 opens the
 * payment modal and the delete is retried after paying. From in here it is one
 * async function that either resolves or throws, which is the whole point: the
 * table renders positions, and nothing about a payment belongs in it.
 *
 * The single exception is the `payment_cancelled` check below. A user who opens
 * the payment sheet and closes it again has not hit an error, and a red banner
 * over the table would tell them they have.
 */

import { useState } from 'react'

import { money, moneyPrecise, percent, quantity } from '../format'
import { PAYMENT_CANCELLED } from '../payments/unlock-context'

const SOURCE_LABELS = {
  live: { text: 'Live', className: 'tag tag--live' },
  last_close: { text: 'Last close', className: 'tag tag--stale' },
}

/** What the em dash stands in for in an excluded row: nothing we could compute. */
const NOT_APPLICABLE = '--'

/**
 * The delete cell, shared by both kinds of row.
 *
 * Extracted rather than written twice: an excluded position needs this button
 * MORE than a measured one does - deleting the dead ticker is the fix for the
 * warning above the dashboard - so the two must not be able to drift apart.
 *
 * Renders an empty cell when the row's id has not arrived from
 * /api/portfolio/<id>/holdings/ yet, which is better than a button that cannot
 * work. See the note at the top of this file on where the ids come from.
 */
function DeleteCell({ ticker, holdingId, deleting, onDelete }) {
  return (
    <td className="table__actions">
      {holdingId ? (
        <button
          type="button"
          className="button button--icon"
          onClick={() => onDelete(ticker)}
          disabled={deleting !== null}
          title={`Delete ${ticker}`}
        >
          {deleting === ticker ? '…' : '✕'}
          <span className="visually-hidden">Delete {ticker}</span>
        </button>
      ) : null}
    </td>
  )
}

export default function HoldingsTable({
  holdings = [],
  // Positions the risk report could not measure. They are NOT in `holdings` -
  // the backend keeps the measured subset separate so that everything with a
  // weight really has one - so they are rendered here as their own rows.
  //
  // Showing them is the point. Dropping an unpriceable position from this table
  // would leave the user with a portfolio that quietly lost a holding and no
  // way to act on it; showing it, marked, with its delete button intact, is
  // what makes "correct or remove the dead ticker" a thing they can actually do.
  excluded = [],
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
      // Kept in this panel. A failed delete must not blank the dashboard - and
      // a cancelled payment is not a failure at all, so it says nothing.
      if (apiError?.code !== PAYMENT_CANCELLED) setError(apiError)
    } finally {
      setDeleting(null)
    }
  }

  const total = holdings.length + excluded.length

  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Holdings</h2>
        <p className="panel__subtitle">
          {total} position{total === 1 ? '' : 's'}
          {excluded.length > 0 && (
            <>
              {' · '}
              <span className="holdings__excluded-count">
                {excluded.length} excluded from risk
              </span>
            </>
          )}
        </p>
      </div>
      {error && (
        <p className="banner banner--error panel__banner" role="alert">
          <strong>Could not delete:</strong> {error.message}
        </p>
      )}

      {total === 0 ? (
        <div className="empty">
          <span className="empty__icon" aria-hidden="true">
            <EmptyIcon />
          </span>
          <p className="empty__title">No positions yet</p>
          <p className="empty__body">
            Add one with the form above, or import a CSV. Everything else on this
            dashboard - the risk metrics, the charts, the rebalance suggestion - is
            computed from what lands here.
          </p>
        </div>
      ) : (
      <div className="panel__body panel__body--scroll">
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
                    <DeleteCell
                      ticker={holding.ticker}
                      holdingId={holdingId}
                      deleting={deleting}
                      onDelete={remove}
                    />
                  )}
                </tr>
              )
            })}

            {/* The unmeasurable positions, after the measured ones. Same
                columns, so the ticker and quantity still line up; every cell
                the report could not fill reads "--" rather than 0, because a
                zero here would be a number we do not have. */}
            {excluded.map((holding) => {
              const holdingId = holdingIds[holding.ticker]
              return (
                <tr key={holding.ticker} className="table__row--excluded">
                  <th scope="row" className="table__ticker">{holding.ticker}</th>
                  <td className="table__num">{quantity(holding.quantity)}</td>
                  <td className="table__num">{NOT_APPLICABLE}</td>
                  <td>
                    {/* The detail is the tooltip rather than a column: it is a
                        sentence, and it would wreck the row rhythm inline. */}
                    <span className="tag tag--dead" title={holding.detail}>
                      No price
                    </span>
                  </td>
                  <td className="table__num">{NOT_APPLICABLE}</td>
                  <td className="table__num">
                    <span className="table__weight-value">
                      {NOT_APPLICABLE}
                      <span className="visually-hidden">
                        {' '}excluded from the risk report
                      </span>
                    </span>
                  </td>
                  {canDelete && (
                    <DeleteCell
                      ticker={holding.ticker}
                      holdingId={holdingId}
                      deleting={deleting}
                      onDelete={remove}
                    />
                  )}
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            <tr>
              {/* Named honestly when it is a subtotal. The market value and the
                  100% both describe the MEASURED rows only, so calling it
                  "Total" beside two excluded positions would be a small lie in
                  the one place a reader checks their arithmetic. */}
              <th scope="row">{excluded.length > 0 ? 'Total (valued)' : 'Total'}</th>
              <td colSpan={3} />
              <td className="table__num">{money(marketValue)}</td>
              <td className="table__num">100.0%</td>
              {canDelete && <td />}
            </tr>
          </tfoot>
        </table>
      </div>
      )}
    </section>
  )
}

/** The empty-table mark: a list with nothing on it. */
function EmptyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M8 7h11M8 12h11M8 17h7M4.5 7h.01M4.5 12h.01M4.5 17h.01"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
    </svg>
  )
}
