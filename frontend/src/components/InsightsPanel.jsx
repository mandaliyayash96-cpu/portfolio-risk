/**
 * AI insights: the risk report, explained in plain English on demand.
 *
 * WHAT THIS PANEL IS HONEST ABOUT
 * -------------------------------
 * Every sentence it shows is a DESCRIPTION of figures already on this
 * dashboard - the same holdings, the same P/L, the same volatility and
 * drawdown - written by a language model the backend asked for observations
 * only. The backend also filters anything that reads as a directive or a
 * forecast before it gets here, and it is the backend, not the model, that
 * appends the disclaimer. This component prints what it is given; it does not
 * soften, reorder or editorialise.
 *
 * ON DEMAND, NOT ON A TIMER
 * -------------------------
 * Nothing here runs on the dashboard's 30-second poll. Each call spends a
 * rate-limited free-tier quota on a third-party model, so it is a button, and
 * the button is disabled while a call is in flight. The result stays on screen
 * until the user asks again; the Section that mounts this panel is
 * `keepMounted`, so switching tabs does not throw it away either.
 *
 * TWO KINDS OF "NOT NOW"
 * ----------------------
 * The backend answers 200 with `available: false` when the MODEL could not be
 * used - a rate limit, a timeout, no key on the server. That is rendered as a
 * calm amber notice with a retry, because nothing is wrong with the portfolio.
 * A thrown ApiError is the PORTFOLIO'S problem (empty, too little history) and
 * gets the red treatment the rest of the dashboard uses for the same codes.
 */

import { useEffect, useRef, useState } from 'react'

import { getAiInsights } from '../api/client'
import { clockTime } from '../format'
import { Skeleton } from './Skeleton'

/**
 * Shown before the first result and whenever the server's own sentence is not
 * available yet. The server's `disclaimer` is preferred once a response has
 * arrived, so the wording is owned in one place.
 */
const DISCLAIMER = 'Insights are informational only, not financial advice.'

/**
 * How each category is presented. `tone` picks the colour family from the
 * stylesheet: green for gains, red for losses, amber for risk, blue for
 * diversification, and plain ink for a general note. The label is printed on
 * the card so the category survives greyscale and a screen reader.
 */
const CATEGORIES = {
  gain: { label: 'Gain', tone: 'good', Icon: TrendUpIcon },
  loss: { label: 'Loss', tone: 'bad', Icon: TrendDownIcon },
  risk: { label: 'Risk', tone: 'warn', Icon: AlertIcon },
  diversification: { label: 'Diversification', tone: 'info', Icon: GridIcon },
  general: { label: 'Note', tone: 'neutral', Icon: InfoIcon },
}

/** Why the model was unavailable, in a sentence the reason code maps to. */
const REASON_DETAIL = {
  rate_limited: 'The free-tier request limit was hit. Wait a minute and try again.',
  unavailable: 'The model did not answer in time. Try again in a moment.',
  not_configured: 'The server has no Gemini API key configured.',
  bad_response: 'The model answered in a shape the server could not use. Try again.',
}

export default function InsightsPanel({ portfolioId }) {
  // 'idle' | 'loading' | 'done' | 'error'. One word rather than three
  // booleans, because the four states are exclusive and the render below
  // branches on exactly one of them.
  const [status, setStatus] = useState('idle')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  // Same guard <BrokerConnect> carries, for the same reason: a sign-out
  // unmounts the dashboard with this request still in flight, and the
  // resolution must not write to a component that is gone. Set inside the
  // effect so StrictMode's mount/unmount/mount leaves it true.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  async function generate() {
    if (status === 'loading') return
    setStatus('loading')
    setError(null)
    try {
      const data = await getAiInsights(portfolioId)
      if (!mounted.current) return
      setResult(data)
      setStatus('done')
    } catch (apiError) {
      if (!mounted.current) return
      setError(apiError)
      setStatus('error')
    }
  }

  const isLoading = status === 'loading'
  const disclaimer = result?.disclaimer ?? DISCLAIMER
  const hasResult = status === 'done' && result

  let buttonLabel = 'Get AI insights'
  if (isLoading) buttonLabel = 'Reading your portfolio…'
  else if (result || error) buttonLabel = 'Get fresh insights'

  return (
    <section className="panel insights" aria-busy={isLoading}>
      <div className="panel__head">
        <div>
          <h2 className="panel__title">AI insights</h2>
          <p className="panel__subtitle">
            What the numbers on this dashboard show, in plain English. Observations only — it
            will not tell you what to buy or sell.
          </p>
        </div>
        <button type="button" className="button" onClick={generate} disabled={isLoading}>
          {isLoading && <span className="spinner spinner--inline" aria-hidden="true" />}
          <span>{buttonLabel}</span>
        </button>
      </div>

      <div className="insights__body">
        {status === 'idle' && <IdleState />}

        {isLoading && <LoadingState />}

        {status === 'error' && error && (
          <div className="banner banner--error" role="alert">
            <strong>{error.code ?? 'error'}:</strong> {error.message}
          </div>
        )}

        {hasResult && !result.available && (
          <div className="banner banner--warn insights__notice" role="status">
            <strong>{result.message}</strong>
            {REASON_DETAIL[result.reason] && <span> {REASON_DETAIL[result.reason]}</span>}
          </div>
        )}

        {hasResult && result.available && result.insights.length === 0 && (
          <p className="panel__placeholder insights__empty" role="status">
            The model answered, but nothing it wrote passed the observations-only check. Try
            again — each run is a fresh read of the same figures.
          </p>
        )}

        {hasResult && result.available && result.insights.length > 0 && (
          <ul className="insights__grid" aria-label="AI insights">
            {result.insights.map((insight, index) => (
              <InsightCard key={`${insight.category}-${index}`} insight={insight} />
            ))}
          </ul>
        )}

        {hasResult && result.excluded?.length > 0 && (
          <p className="insights__excluded">
            Not part of this analysis (no usable price data):{' '}
            {result.excluded.map((entry) => entry.ticker).join(', ')}. They are listed under
            Holdings and were neither valued nor described.
          </p>
        )}
      </div>

      <div className="panel__foot insights__foot">
        <span className="insights__disclaimer">
          <InfoIcon /> {disclaimer}
        </span>
        {hasResult && result.available && (
          <span className="insights__meta">
            {result.model} · generated {clockTime(new Date(result.generated_at))}
          </span>
        )}
      </div>
    </section>
  )
}

/** One insight. The tone is on the rail, the icon and the label - never alone. */
function InsightCard({ insight }) {
  const { label, tone, Icon } = CATEGORIES[insight.category] ?? CATEGORIES.general
  return (
    <li className={`insight insight--${tone}`}>
      <span className="insight__icon" aria-hidden="true">
        <Icon />
      </span>
      <div className="insight__content">
        <span className="insight__label">{label}</span>
        <p className="insight__text">{insight.text}</p>
      </div>
    </li>
  )
}

/**
 * Before the first click: what a run does, and what it will never do. The
 * second half is the more important one - a user who has seen "AI" beside a
 * portfolio is entitled to expect stock tips, and this is where they learn
 * they will not get any.
 */
function IdleState() {
  return (
    <div className="insights__idle">
      <p className="insights__idle-lead">
        Press <strong>Get AI insights</strong> and the model reads the same holdings and risk
        figures shown on this dashboard, then describes what stands out — the biggest gains and
        losses, how concentrated the portfolio is, and how rough the ride has been.
      </p>
      <ul className="insights__rules">
        <li>Describes what the data shows. Does not recommend buying or selling anything.</li>
        <li>Does not predict prices or returns.</li>
        <li>Uses only the figures already on this page — nothing is fetched from the news.</li>
      </ul>
    </div>
  )
}

/** Three card-shaped placeholders, so the grid does not jump when they land. */
function LoadingState() {
  return (
    <div className="insights__grid" role="status" aria-live="polite">
      <span className="visually-hidden">Generating insights…</span>
      {Array.from({ length: 3 }, (_, index) => (
        <div className="insight insight--skeleton" key={index} aria-hidden="true">
          <Skeleton variant="text" width="5rem" />
          <Skeleton variant="text" />
          <Skeleton variant="text" width="80%" />
        </div>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------------------
   Icons. Inline, 1.9 stroke, currentColor - the same conventions as the
   empty-state mark in Dashboard.jsx.
   --------------------------------------------------------------------------- */
function TrendUpIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M4 17l6-6 4 4 6-7M14 8h6v6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function TrendDownIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M4 7l6 6 4-4 6 7M14 16h6v-6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function AlertIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M12 4l9 16H3l9-16zM12 10v4M12 17.5v.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function GridIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function InfoIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.9" />
      <path
        d="M12 11v5M12 8v.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
    </svg>
  )
}
