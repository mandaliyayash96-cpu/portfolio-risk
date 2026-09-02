/**
 * The per-row outcome of one import: counts, then every row and its reason.
 *
 * Lifted out of ManageHoldings.jsx when the broker sync arrived, because both
 * importers answer with the same report and neither of them should own the
 * rendering of it. Nothing about it changed in the move.
 *
 * WHY THE TABLE IS THE PRIMARY RESULT AND THE COUNTS ARE THE SUMMARY
 * ------------------------------------------------------------------
 * An import is not pass/fail. The backend commits every valid row and reports
 * the rest, so a 50-row export with two bad lines loads 48 positions and hands
 * back two reasons. Rendering only "48 added" throws away the half of that
 * answer the user has to act on.
 *
 * `row` means a line number for a CSV and a position in the statement for a
 * broker import. The column is headed "Row" for both because in each case it
 * is "the nth thing in what you gave us", which is all the reader needs it for.
 */

const STATUS_CLASS = {
  added: 'pill pill--good',
  updated: 'pill pill--info',
  skipped: 'pill pill--bad',
}

export default function ImportReport({ report }) {
  const { added, updated, skipped, total_rows: totalRows, results = [] } = report
  const tone = skipped > 0 ? 'banner--warn' : 'banner--good'

  return (
    <div className="manage__report">
      <p className={`banner ${tone}`} role="status">
        <strong>
          {added} added · {updated} updated · {skipped} skipped
        </strong>{' '}
        out of {totalRows} row{totalRows === 1 ? '' : 's'}.
      </p>

      {results.length > 0 && (
        <div className="manage__report-scroll">
          <table className="table table--compact">
            <thead>
              <tr>
                <th scope="col" className="table__num">Row</th>
                <th scope="col">Ticker</th>
                <th scope="col">Result</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <tr key={`${row.row}-${row.ticker ?? 'blank'}`}>
                  <td className="table__num">{row.row}</td>
                  <th scope="row" className="table__ticker">{row.ticker ?? '--'}</th>
                  <td>
                    <span className={STATUS_CLASS[row.status] ?? 'pill'}>{row.status}</span>
                    {/* A saved row the risk report cannot use yet. Beside the
                        status rather than in the detail column, because it is
                        the row's standing, not an explanation of it. */}
                    {row.unverified && (
                      <span className="pill pill--bad" title="No price data yet">
                        Unverified
                      </span>
                    )}
                  </td>
                  {/* reason explains a skip, warning explains a row that saved
                      with something doubtful about it. Only one is ever set. */}
                  <td className="manage__reason">{row.reason ?? row.warning ?? '--'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
