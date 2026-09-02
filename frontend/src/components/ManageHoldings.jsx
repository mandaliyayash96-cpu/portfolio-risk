/**
 * Holdings entry: connect a broker, type one in, or upload a CSV of them.
 *
 * Self-contained in the same way <AlertsPanel> is. It owns its two forms, its
 * busy states and its errors, and the only thing it hands upward is a single
 * `onChanged()` call after a write actually landed - which is the dashboard's
 * cue to re-fetch risk, rebalance and performance. If everything in here fails,
 * the dashboard above it is untouched: this panel shows the error and the
 * numbers stay on screen.
 *
 * NOTHING HERE IS LOCKED
 * ----------------------
 * This panel used to open with a paywall: a ₹9 button, and two forms nobody
 * could see until it was pressed. It no longer has one. Both forms are always
 * visible and always usable, and payment is asked for at SUBMIT - and only if
 * the server refuses the write with a 402.
 *
 * The mechanism is `gatedWrite`, and the reason both forms can use it without
 * knowing anything about payments is that it resolves with the write's real
 * result whichever path it took. A submit that needed ₹9 and a submit that did
 * not look identical from in here: one `await`, one result, one success banner.
 * The only payment-aware line in either form is the `payment_cancelled` check,
 * which exists so that changing your mind is not reported as an error.
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
 * An import is not pass/fail, and <ImportReport> is where that is rendered -
 * for the CSV upload and the broker sync alike, since the backend answers both
 * with the same report.
 *
 * THE THREE SECTIONS, AND WHY BROKERS COME FIRST
 * ----------------------------------------------
 * <BrokerConnect> is the fastest way to fill an empty portfolio - one click and
 * five positions - so it is above the two forms that ask you to type. It is
 * also the one that is SIMULATED, which it says on its own face; this component
 * treats it as an ordinary child and knows nothing about that.
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
import { PAYMENT_CANCELLED, useUnlock } from '../payments/unlock-context'
import { moneyPrecise } from '../format'
import BrokerConnect from './BrokerConnect'
import ImportReport from './ImportReport'

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

const ASSET_TYPE_LABELS = Object.fromEntries(
  ASSET_TYPES.map((entry) => [entry.value, entry.label]),
)

const EMPTY_FORM = {
  ticker: '',
  quantity: '',
  avg_buy_price: '',
  buy_date: '',
  asset_type: 'EQUITY',
  sector: '',
}

/** True when the user closed the payment sheet rather than the write failing. */
const isCancellation = (error) => error?.code === PAYMENT_CANCELLED

/**
 * The same three rules the backend enforces, checked before the round trip.
 *
 * This is a courtesy, not the validation - `portfolio.services` re-checks every
 * one of these and is the only opinion that counts. Doing it here too just
 * means a blank ticker costs a keystroke instead of a network call - and, now
 * that a submit can open a payment sheet, it means a form that was never going
 * to save cannot get as far as asking for money.
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

/**
 * A spinner and a line of text, used by both forms while a write is in flight.
 *
 * `isWaitingForPayment` outranks the slow-write label, because once the modal
 * is up "fetching prices" is not what is happening - the request has already
 * come back, refused, and is parked waiting for the user.
 */
function BusyNote({ isSlow, isWaitingForPayment, idleLabel }) {
  let label = idleLabel
  if (isWaitingForPayment) label = 'Waiting for payment…'
  else if (isSlow) label = 'Fetching prices from the market feed…'

  return (
    <span className="manage__busy">
      <span className="spinner spinner--inline" aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}

/* ---------------------------------------------------------------------------
   Manual entry
   --------------------------------------------------------------------------- */
function ManualForm({ portfolioId, onChanged }) {
  const { gatedWrite, pending } = useUnlock()
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

    const ticker = form.ticker.trim().toUpperCase()
    const quantity = form.quantity.trim()
    const price = form.avg_buy_price.trim()

    setSavingRun(Date.now())
    setError(null)
    setResult(null)
    try {
      // Values go as the typed strings - see addHolding's note on why they are
      // not passed through Number() first.
      const saved = await gatedWrite(
        {
          action: `Add ${ticker} ×${quantity}`,
          detail: `${ASSET_TYPE_LABELS[form.asset_type] ?? form.asset_type} · average buy price ${moneyPrecise(price)}`,
          noun: 'holding',
        },
        () =>
          addHolding(portfolioId, {
            ticker,
            quantity,
            avg_buy_price: price,
            buy_date: form.buy_date || null,
            asset_type: form.asset_type,
            sector: form.sector.trim(),
          }),
      )
      setResult(saved)
      setForm({ ...EMPTY_FORM, asset_type: form.asset_type })
      onChanged()
    } catch (apiError) {
      // A cancelled payment is not a failure to apologise for, and the form is
      // still full of what they typed. Say nothing and leave it alone.
      if (!isCancellation(apiError)) setError(apiError)
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
        {isSaving && (
          <BusyNote
            isSlow={isSlow}
            isWaitingForPayment={pending !== null}
            idleLabel="Saving…"
          />
        )}
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

/**
 * How many data rows a picked CSV appears to hold.
 *
 * Read in the browser purely so the payment modal can say "Import 5 holdings"
 * instead of "Import a file" - a summary of something you cannot see is not a
 * summary. It is a COUNT, not a parse: the backend does the real reading, and
 * a file this miscounts still imports exactly the same rows.
 *
 * Returns null when the file cannot be read, and every caller treats that as
 * "say nothing about the count" rather than as an error - failing to preview a
 * number must never stop an import.
 */
async function countCsvRows(file) {
  try {
    const text = await file.text()
    const lines = text.split(/\r?\n/).filter((line) => line.trim() !== '')
    // Minus the header. A file of nothing but a header counts as zero, not -1.
    return Math.max(lines.length - 1, 0)
  } catch {
    return null
  }
}

function ImportForm({ portfolioId, onChanged }) {
  const { gatedWrite, pending } = useUnlock()
  const [file, setFile] = useState(null)
  const [rowCount, setRowCount] = useState(null)
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

  async function pickFile(event) {
    const picked = event.target.files?.[0] ?? null
    setFile(picked)
    setRowCount(null)
    setError(null)
    if (picked) setRowCount(await countCsvRows(picked))
  }

  async function submit(event) {
    event.preventDefault()
    if (isImporting || !file) return

    setImportingRun(Date.now())
    setError(null)
    setReport(null)
    try {
      const summary = await gatedWrite(
        {
          action:
            rowCount === null
              ? `Import holdings from ${file.name}`
              : `Import ${rowCount} holding${rowCount === 1 ? '' : 's'} from ${file.name}`,
          detail: 'Valid rows are saved; anything rejected comes back with a reason',
          noun: 'holdings',
        },
        () => importHoldingsCsv(portfolioId, file),
      )
      setReport(summary)
      setFile(null)
      setRowCount(null)
      if (inputRef.current) inputRef.current.value = ''
      // Even an import where every row failed is worth refreshing on: the
      // cheap call is the one that tells the truth about what is stored now.
      onChanged()
    } catch (apiError) {
      // Cancelled: keep the chosen file so the button is still armed.
      if (!isCancellation(apiError)) setError(apiError)
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
          onChange={pickFile}
        />
        <button type="submit" className="button button--small" disabled={isImporting || !file}>
          {isImporting ? 'Importing…' : 'Import'}
        </button>
        {isImporting && (
          <BusyNote
            isSlow={isSlow}
            isWaitingForPayment={pending !== null}
            idleLabel="Reading rows…"
          />
        )}
      </div>

      {file && rowCount !== null && !isImporting && (
        <p className="manage__hint" role="status">
          <strong>{file.name}</strong> — {rowCount} data row{rowCount === 1 ? '' : 's'} found.
        </p>
      )}

      {error && (
        <p className="banner banner--error" role="alert">
          <strong>{error.code ?? 'error'}:</strong> {error.message}
        </p>
      )}

      {report && <ImportReport report={report} />}
    </form>
  )
}

/* ---------------------------------------------------------------------------
   Shell
   --------------------------------------------------------------------------- */
export default function ManageHoldings({ portfolioId, onChanged }) {
  // Read for one purpose only: to say so when a round is already paid for, and
  // to offer the way out of it. Nothing in this panel is hidden or disabled by
  // it - the forms below do not consult it at all.
  const { isUnlocked, lock } = useUnlock()

  return (
    <section className="panel manage">
      <div className="panel__head">
        <div>
          <h2 className="panel__title">Manage holdings</h2>
          <p className="panel__subtitle">
            Connect a broker, add a position by hand, or import a CSV. Editing costs{' '}
            {UNLOCK_PRICE_LABEL} per round — you are asked when you save, not before.
          </p>
        </div>
      </div>

      {/*
        Only shown once a round is open. It is the answer to "have I already
        paid?", which is a question a pay-at-submit flow invites and a paywall
        never did - and it carries the control that ends the round, so the
        server-side lifecycle is unchanged from when a Close button owned it.
      */}
      {isUnlocked && (
        <p className="manage__round" role="status">
          <span className="manage__round-text">
            <strong>Editing round is open.</strong> Add, import and delete as much as you
            like — you will not be asked to pay again until you end it or reload.
          </span>
          <button type="button" className="button button--small button--ghost" onClick={lock}>
            End round
          </button>
        </p>
      )}

      <div className="panel__body manage__body">
        <div className="manage__section">
          <h3 className="manage__heading">
            Connect a broker
            {/* Labelled on the heading as well as in the note below it: the
                note explains the demo, this makes it unmissable at a glance. */}
            <span className="pill pill--info">Simulated</span>
          </h3>
          <BrokerConnect portfolioId={portfolioId} onChanged={onChanged} />
        </div>

        <div className="manage__section">
          <h3 className="manage__heading">Add one</h3>
          <ManualForm portfolioId={portfolioId} onChanged={onChanged} />
        </div>

        <div className="manage__section">
          <h3 className="manage__heading">Import a CSV</h3>
          <ImportForm portfolioId={portfolioId} onChanged={onChanged} />
        </div>
      </div>
    </section>
  )
}
