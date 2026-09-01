/**
 * Holdings entry: type one in, or upload a CSV of them.
 *
 * Self-contained in the same way <AlertsPanel> is. It owns its two forms, its
 * busy states and its errors, and the only thing it hands upward is a single
 * `onChanged()` call after a write actually landed - which is App's cue to
 * re-fetch risk, rebalance and performance. If everything in here fails, the
 * dashboard above it is untouched: this panel shows the error and the numbers
 * stay on screen.
 *
 * WHY THE BUSY STATE HAS TWO WORDS FOR IT
 * ---------------------------------------
 * Adding a ticker the backend has never priced makes it call yfinance
 * synchronously before it answers - two upstream calls per new symbol, each
 * with a 20s ceiling. A plain "Saving…" on a button that then sits there for
 * fifteen seconds reads as a hang. So once a write passes SLOW_WRITE_MS the
 * label says what is actually taking the time: fetching prices. Nothing about
 * the request changes; only the honesty of the label does.
 *
 * WHAT THE CSV REPORT IS
 * ----------------------
 * An import is not pass/fail. The backend commits every valid row and reports
 * the rest, so a 50-row export with two bad lines loads 48 positions and hands
 * back two reasons. Rendering only a count would throw away the half of that
 * answer the user has to act on, so the per-row table is the primary result
 * here and the counts are the summary above it.
 *
 * EDITING COSTS ₹9, AND THE OPEN/CLOSE TOGGLE IS THE ROUND
 * --------------------------------------------------------
 * This panel had an Open/Close button before payments existed, and that button
 * turned out to BE the editing round - so the two were merged rather than
 * stacked. Open is now "pay ₹9 and start editing"; Close is "I am done", which
 * ends the round server-side and means the next one costs another ₹9. There is
 * no third state where the panel is open but unpaid.
 *
 * The lock is a SCREEN, not a permission. Every write endpoint refuses an
 * unpaid request with a 402 whatever this component renders - see
 * payments/gating.py. What the locked state buys is a user who is told the
 * price before they fill in a form, rather than after.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import {
  CSV_OPTIONAL_COLUMNS,
  CSV_REQUIRED_COLUMNS,
  CSV_TEMPLATE,
  addHolding,
  importHoldingsCsv,
} from '../api/client'
import { UNLOCK_PRICE_LABEL } from '../api/payments'
import { useUnlock } from '../payments/unlock-context'

/** How long a write may take before the label admits it is fetching prices. */
const SLOW_WRITE_MS = 600

/** Mirrors portfolio.models.AssetType. Keep the two in step. */
const ASSET_TYPES = [
  { value: 'EQUITY', label: 'Equity' },
  { value: 'ETF', label: 'ETF' },
  { value: 'MUTUAL_FUND', label: 'Mutual fund' },
  { value: 'BOND', label: 'Bond' },
  { value: 'CASH', label: 'Cash' },
  { value: 'CRYPTO', label: 'Crypto' },
  { value: 'OTHER', label: 'Other' },
]

const EMPTY_FORM = {
  ticker: '',
  quantity: '',
  avg_buy_price: '',
  buy_date: '',
  asset_type: 'EQUITY',
  sector: '',
}

const STATUS_CLASS = {
  added: 'pill pill--good',
  updated: 'pill pill--info',
  skipped: 'pill pill--bad',
}

/**
 * The same three rules the backend enforces, checked before the round trip.
 *
 * This is a courtesy, not the validation - `portfolio.services` re-checks every
 * one of these and is the only opinion that counts. Doing it here too just
 * means a blank ticker costs a keystroke instead of a network call.
 */
function validate(form) {
  const errors = {}
  if (!form.ticker.trim()) {
    errors.ticker = 'Required - the yfinance symbol, e.g. RELIANCE.NS'
  }
  for (const [field, label] of [
    ['quantity', 'Quantity'],
    ['avg_buy_price', 'Average buy price'],
  ]) {
    const raw = form[field].trim()
    if (!raw) {
      errors[field] = 'Required'
    } else if (!Number.isFinite(Number(raw))) {
      errors[field] = `${label} must be a number`
    } else if (Number(raw) <= 0) {
      errors[field] = `${label} must be greater than zero`
    }
  }
  return errors
}

/**
 * True once the write identified by `run` has been in flight longer than `delay`.
 *
 * The delay is the whole point: a write against a ticker that already has
 * prices comes back in milliseconds, and flashing "fetching prices…" for one
 * frame on every fast save would be noise that means nothing.
 *
 * `run` is a TOKEN, not a boolean - null while idle, and a fresh value for each
 * write. That is what lets the timer be the only thing that ever calls
 * setState: the flag is read as "the slow timer fired for THIS run", so a
 * second write cannot inherit the first one's expired timer, and there is
 * nothing to reset when the write finishes. An `active` boolean would need the
 * effect to clear the flag itself, which is a setState in an effect body -
 * cascading renders, and the lint rule that names them.
 */
function useSlowWrite(run, delay = SLOW_WRITE_MS) {
  const [slowRun, setSlowRun] = useState(null)

  useEffect(() => {
    if (run === null) return undefined
    const timer = setTimeout(() => setSlowRun(run), delay)
    return () => clearTimeout(timer)
  }, [run, delay])

  return run !== null && slowRun === run
}

/** A spinner and a line of text, used by both forms while a write is in flight. */
function BusyNote({ isSlow, idleLabel }) {
  return (
    <span className="manage__busy">
      <span className="spinner spinner--inline" aria-hidden="true" />
      <span>{isSlow ? 'Fetching prices from the market feed…' : idleLabel}</span>
    </span>
  )
}

/* ---------------------------------------------------------------------------
   Manual entry
   --------------------------------------------------------------------------- */
function ManualForm({ portfolioId, onChanged }) {
  const [form, setForm] = useState(EMPTY_FORM)
  const [fieldErrors, setFieldErrors] = useState({})
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  // null when idle; the moment the write started when busy. One state variable
  // doing both jobs - see useSlowWrite for why it is a token and not a flag.
  const [savingRun, setSavingRun] = useState(null)
  const isSaving = savingRun !== null
  const isSlow = useSlowWrite(savingRun)

  const update = (field) => (event) => {
    setForm((current) => ({ ...current, [field]: event.target.value }))
    setFieldErrors((current) => ({ ...current, [field]: undefined }))
  }

  async function submit(event) {
    event.preventDefault()
    if (isSaving) return

    const errors = validate(form)
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    setSavingRun(Date.now())
    setError(null)
    setResult(null)
    try {
      // Values go as the typed strings - see addHolding's note on why they are
      // not passed through Number() first.
      const saved = await addHolding(portfolioId, {
        ticker: form.ticker.trim(),
        quantity: form.quantity.trim(),
        avg_buy_price: form.avg_buy_price.trim(),
        buy_date: form.buy_date || null,
        asset_type: form.asset_type,
        sector: form.sector.trim(),
      })
      setResult(saved)
      setForm({ ...EMPTY_FORM, asset_type: form.asset_type })
      onChanged()
    } catch (apiError) {
      setError(apiError)
    } finally {
      setSavingRun(null)
    }
  }

  return (
    <form className="manage__form" onSubmit={submit} noValidate>
      <div className="manage__grid">
        <label className="manage__field">
          <span className="manage__label">Ticker</span>
          <input
            type="text"
            value={form.ticker}
            onChange={update('ticker')}
            placeholder="RELIANCE.NS"
            autoComplete="off"
            spellCheck="false"
            aria-invalid={Boolean(fieldErrors.ticker)}
          />
          {fieldErrors.ticker && <span className="manage__error">{fieldErrors.ticker}</span>}
        </label>

        <label className="manage__field">
          <span className="manage__label">Quantity</span>
          <input
            type="text"
            inputMode="decimal"
            value={form.quantity}
            onChange={update('quantity')}
            placeholder="10"
            aria-invalid={Boolean(fieldErrors.quantity)}
          />
          {fieldErrors.quantity && <span className="manage__error">{fieldErrors.quantity}</span>}
        </label>

        <label className="manage__field">
          <span className="manage__label">Avg buy price</span>
          <input
            type="text"
            inputMode="decimal"
            value={form.avg_buy_price}
            onChange={update('avg_buy_price')}
            placeholder="1400.50"
            aria-invalid={Boolean(fieldErrors.avg_buy_price)}
          />
          {fieldErrors.avg_buy_price && (
            <span className="manage__error">{fieldErrors.avg_buy_price}</span>
          )}
        </label>

        <label className="manage__field">
          <span className="manage__label">
            Buy date <span className="manage__optional">optional</span>
          </span>
          <input type="date" value={form.buy_date} onChange={update('buy_date')} />
        </label>

        <label className="manage__field">
          <span className="manage__label">
            Asset type <span className="manage__optional">optional</span>
          </span>
          <select value={form.asset_type} onChange={update('asset_type')}>
            {ASSET_TYPES.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>

        <label className="manage__field">
          <span className="manage__label">
            Sector <span className="manage__optional">optional</span>
          </span>
          <input
            type="text"
            value={form.sector}
            onChange={update('sector')}
            placeholder="Energy"
            autoComplete="off"
          />
        </label>
      </div>

      <div className="manage__actions">
        <button type="submit" className="button button--small" disabled={isSaving}>
          {isSaving ? 'Adding…' : 'Add holding'}
        </button>
        {isSaving && <BusyNote isSlow={isSlow} idleLabel="Saving…" />}
      </div>

      {error && (
        <p className="banner banner--error" role="alert">
          <strong>{error.code ?? 'error'}:</strong> {error.message}
        </p>
      )}

      {result && (
        <p
          className={`banner ${result.warning ? 'banner--warn' : 'banner--good'}`}
          role="status"
        >
          <strong>
            {result.ticker} {result.created ? 'added' : 'updated'}
          </strong>{' '}
          — {result.quantity} units at {result.avg_buy_price}.
          {result.warning ? ` ${result.warning}` : ''}
        </p>
      )}
    </form>
  )
}

/* ---------------------------------------------------------------------------
   CSV import
   --------------------------------------------------------------------------- */
function ImportForm({ portfolioId, onChanged }) {
  const [file, setFile] = useState(null)
  const [error, setError] = useState(null)
  const [report, setReport] = useState(null)
  const [importingRun, setImportingRun] = useState(null)
  const isImporting = importingRun !== null
  const isSlow = useSlowWrite(importingRun)
  const inputRef = useRef(null)

  /**
   * The sample file, built in the browser rather than served.
   *
   * A blob URL because the alternative - a data: URI - is capped in some
   * browsers and shows the whole file in the status bar on hover. Revoked on
   * unmount so the tab does not leak it.
   */
  const templateUrl = useMemo(
    () => URL.createObjectURL(new Blob([CSV_TEMPLATE], { type: 'text/csv' })),
    [],
  )
  useEffect(() => () => URL.revokeObjectURL(templateUrl), [templateUrl])

  async function submit(event) {
    event.preventDefault()
    if (isImporting || !file) return

    setImportingRun(Date.now())
    setError(null)
    setReport(null)
    try {
      const summary = await importHoldingsCsv(portfolioId, file)
      setReport(summary)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      // Even an import where every row failed is worth refreshing on: the
      // cheap call is the one that tells the truth about what is stored now.
      onChanged()
    } catch (apiError) {
      setError(apiError)
    } finally {
      setImportingRun(null)
    }
  }

  return (
    <form className="manage__form" onSubmit={submit}>
      <p className="manage__hint">
        Required columns: {CSV_REQUIRED_COLUMNS.map((column) => (
          <code key={column}>{column}</code>
        ))}
        . Optional: {CSV_OPTIONAL_COLUMNS.map((column) => (
          <code key={column}>{column}</code>
        ))}
        . Up to 500 rows, 1 MB.{' '}
        <a href={templateUrl} download="sample.csv" className="manage__link">
          Download sample.csv
        </a>
      </p>

      <div className="manage__actions">
        <input
          ref={inputRef}
          type="file"
          accept=".csv,text/csv"
          className="manage__file"
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null)
            setError(null)
          }}
        />
        <button type="submit" className="button button--small" disabled={isImporting || !file}>
          {isImporting ? 'Importing…' : 'Import'}
        </button>
        {isImporting && <BusyNote isSlow={isSlow} idleLabel="Reading rows…" />}
      </div>

      {error && (
        <p className="banner banner--error" role="alert">
          <strong>{error.code ?? 'error'}:</strong> {error.message}
        </p>
      )}

      {report && <ImportReport report={report} />}
    </form>
  )
}

/** The per-row outcome of one import: counts, then every row and its reason. */
function ImportReport({ report }) {
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
                  </td>
                  {/* reason explains a skip, warning explains a row that saved
                      without prices. Only one is ever set. */}
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

/* ---------------------------------------------------------------------------
   Shell
   --------------------------------------------------------------------------- */
export default function ManageHoldings({ portfolioId, onChanged }) {
  // The open/closed state of this panel IS the editing round - it is not
  // tracked separately here, because two booleans that must agree are one
  // booleans' worth of truth and twice the ways to disagree.
  const { isUnlocked, isPaying, error, unlock, lock } = useUnlock()

  return (
    <section className="panel manage">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">Manage holdings</h2>
          <p className="panel__subtitle">
            {isUnlocked
              ? 'Editing is open — every panel above recomputes as you change things'
              : `Add a position by hand or import a CSV — ${UNLOCK_PRICE_LABEL} per editing session`}
          </p>
        </div>

        {isUnlocked ? (
          <button type="button" className="button button--small button--ghost" onClick={lock}>
            Close
          </button>
        ) : (
          <button
            type="button"
            className="button button--small manage__unlock"
            onClick={unlock}
            disabled={isPaying}
          >
            {isPaying && <span className="spinner spinner--inline" aria-hidden="true" />}
            {isPaying ? 'Opening checkout…' : `Unlock editing ${UNLOCK_PRICE_LABEL}`}
          </button>
        )}
      </div>

      {isUnlocked ? (
        <div className="panel__body manage__body">
          <div className="manage__section">
            <h3 className="manage__heading">Add one</h3>
            <ManualForm portfolioId={portfolioId} onChanged={onChanged} />
          </div>

          <div className="manage__section">
            <h3 className="manage__heading">Import a CSV</h3>
            <ImportForm portfolioId={portfolioId} onChanged={onChanged} />
          </div>

          <p className="manage__round" role="status">
            This round is paid for. Add and remove as much as you like — closing
            this panel ends it, and the next round is another {UNLOCK_PRICE_LABEL}.
          </p>
        </div>
      ) : (
        <div className="panel__body manage__locked">
          <p className="manage__locked-title">
            <span aria-hidden="true">🔒</span> Editing is locked
          </p>
          <p className="manage__locked-body">
            Viewing your dashboard is free. One payment of {UNLOCK_PRICE_LABEL} opens a
            single editing session: add positions, import a CSV, and delete rows from the
            holdings table below, as many as you need. Closing the panel — or reloading the
            page — ends the session.
          </p>
          {/*
            The failure lives here rather than in a toast: the button that
            caused it is six pixels away, and a cancelled payment needs no
            apology, only a way to try again.
          */}
          {error && (
            <p className="banner banner--error manage__locked-error" role="alert">
              {error.message}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
