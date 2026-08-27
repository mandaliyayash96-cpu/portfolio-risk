/**
 * The positions behind every number on this page.
 *
 * `price_source` is shown because it changes how much to trust the valuation:
 * "live" is the latest poll, "last close" means the live fetch had nothing and
 * the most recent stored close was used instead. A judge asking "how current is
 * this?" should be able to see the answer, not be told it.
 */

import { money, moneyPrecise, percent, quantity } from '../format'

const SOURCE_LABELS = {
  live: { text: 'Live', className: 'tag tag--live' },
  last_close: { text: 'Last close', className: 'tag tag--stale' },
}

export default function HoldingsTable({ holdings = [], marketValue }) {
  return (
    <section className="panel">
      <div className="panel__head">
        <h2 className="panel__title">Holdings</h2>
        <p className="panel__subtitle">{holdings.length} positions</p>
      </div>
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
            </tr>
          </thead>
          <tbody>
            {holdings.map((holding) => {
              const source = SOURCE_LABELS[holding.price_source] ?? {
                text: holding.price_source,
                className: 'tag',
              }
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
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  )
}
