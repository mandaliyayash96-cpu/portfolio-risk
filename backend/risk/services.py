"""
Risk services: the one place where Django, the ORM and the pure engine meet.

Architecture rule 2 keeps `risk/engine.py` pure, so every impure concern lives
here:

  * reading holdings and prices through selectors (rule 1 - no ORM in views),
  * reading settings (risk-free rate, trading calendar, benchmark symbol) and
    passing them into the engine as ordinary arguments,
  * crossing the Decimal -> float64 boundary for the weights vector
    (common/MONEY.md),
  * translating "that cannot be computed" into DomainErrors, which the
    exception handler renders as clean {success: false} envelopes rather than
    letting a ValueError out of NumPy surface as a 500.

The engine never learns any of this exists: it receives a DataFrame, a weights
list and some floats.

Pipeline
--------
    holdings ---> value each position (Decimal) ---> weights (float64, once)

    close series per ticker ---> engine.align_returns  (inner join on date)
                            ---> engine.daily_returns  (differenced AFTER the
                                                        join, so every return
                                                        spans the same interval)
                            ---> split off the benchmark column
                            ---> engine.build_report     (compute_risk)
                            ---> risk.optimizer          (compute_rebalance)

All three endpoints share `_prepare()`, so they cannot disagree about the
window, the valuation or the join.

`_prepare` also owns GRACEFUL DEGRADATION: a holding with no usable price data
is excluded from the maths and reported in `warnings` and `excluded`, rather
than failing the request. One dead ticker costs the user that ticker, not the
whole dashboard. See `_Excluded` and `_prepare` for the rules and the two cases
that are still fatal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd
from django.conf import settings

from common.exceptions import (
    EmptyPortfolioError,
    InsufficientHistoryError,
    InvalidInputError,
    OptimizationError,
)
from marketdata.selectors import (
    CLOSE_COLUMN,
    DEFAULT_HISTORY_DAYS,
    get_history_df,
    get_latest_closes,
    get_latest_prices,
)
from portfolio.models import Holding, Portfolio
from portfolio.selectors import get_holdings, get_portfolio
from risk import engine, optimizer

ZERO = Decimal("0")

#: Floor on the number of OVERLAPPING return observations. Sample statistics
#: with ddof=1 are defined from 2 observations, but a "risk report" built on a
#: handful of days is noise dressed as measurement, so the floor is a trading
#: month. `manage.py fetch_prices` pulls 252 days by default, comfortably above
#: this; callers can lower it (the tests do) with compute_risk(min_observations=).
MIN_OBSERVATIONS = 20

#: Where the performance curve starts. 100 is the convention for a rebased
#: index: the chart is then about SHAPE - growth and drawdown - and no point on
#: it can be mistaken for a rupee valuation of the portfolio.
REBASE_VALUE = 100.0

#: What to tell the user when data is missing. One string, one fix.
_FETCH_HINT = "Run `python manage.py fetch_prices` first."


@dataclass(frozen=True)
class _Position:
    """One valued holding. Money stays Decimal here - see common/MONEY.md."""

    ticker: str
    quantity: Decimal
    price: Decimal
    price_source: str  # "live" (PriceSnapshot) or "last_close" (PriceHistory)

    @property
    def market_value(self) -> Decimal:
        """quantity * price. Exact: no float has touched this yet."""
        return self.quantity * self.price


#: Why a holding could not be measured. Two genuinely different gaps, kept
#: apart because they have different fixes and the user deserves to be told
#: which one they have.
NO_PRICE = "no_price"  # nothing to value the position AT
NO_HISTORY = "no_history"  # valued, but no series to compute returns FROM


@dataclass(frozen=True)
class _Excluded:
    """
    One holding the report had to leave out, and why.

    This exists so that a dead ticker is a FOOTNOTE rather than a failure. It
    used to be neither: a single unpriceable symbol raised MissingPriceDataError
    out of `_prepare` and the entire dashboard - risk, performance, rebalance,
    every chart - rendered as one error page. A portfolio of twenty positions
    was unmeasurable because one of them had been delisted.

    So the pipeline now measures what it can and reports what it could not. The
    excluded holding keeps its row in the holdings table (marked, not hidden)
    and is named in `warnings`; it is simply absent from the weights, the market
    value and the returns matrix, because there is no honest number to put there.
    """

    ticker: str
    quantity: Decimal
    reason: str  # NO_PRICE or NO_HISTORY

    @property
    def detail(self) -> str:
        """One sentence, for a client that wants to render this row on its own."""
        if self.reason == NO_PRICE:
            return "No stored price, so this position could not be valued."
        return "No stored price history, so no return series could be built."

    def as_dict(self) -> dict:
        """Money-free, so no Decimal/str convention question arises."""
        return {
            "ticker": self.ticker,
            "quantity": str(self.quantity),
            "reason": self.reason,
            "detail": self.detail,
        }


def compute_risk(
    portfolio_id: int,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    conf: float = engine.DEFAULT_CONFIDENCE,
    min_observations: int = MIN_OBSERVATIONS,
) -> dict:
    """
    Full risk report for one portfolio: holdings + stored prices + engine.

    Args:
        portfolio_id: primary key from the URL.
        days: trading days of stored history to consider per ticker. The
            aligned window is shorter whenever the symbols' histories differ.
        conf: VaR/CVaR confidence, e.g. 0.95.
        min_observations: floor on overlapping return rows (see MIN_OBSERVATIONS).

    Returns:
        The engine's RiskReport dict (every key documented in
        `engine.build_report`) plus three provenance keys added here, because
        the engine has no idea which portfolio it just measured:

            "portfolio"  {id, name, base_currency, market_value, holdings[],
                          excluded[]}
            "benchmark"  {ticker, included}
            "warnings"   list[str] - degradations the caller should see

        `portfolio.holdings` is the MEASURED subset: every row has a price and
        a weight, and the weights sum to 1. `portfolio.excluded` is what could
        not be measured, each with a ticker, a quantity and a reason - and every
        entry there is also described in `warnings`, so a client that renders
        warnings and nothing else still tells the user what happened.

        Every value is JSON-safe: the engine guarantees no NaN/inf, and money
        is emitted as strings (common/MONEY.md).

    Raises:
        NotFoundError (404):            no such portfolio.
        EmptyPortfolioError (400):      the portfolio holds nothing, or nothing
                                        in it could be priced (`details.tickers`
                                        distinguishes the second from the first).
        InsufficientHistoryError (422): too little overlapping history.
        InvalidInputError (400):        the portfolio values at zero.

    Does NOT raise for a holding with no price data. That used to fail the whole
    report; it is now an exclusion - see `_prepare`.
    """
    prepared = _prepare(portfolio_id, days=days, min_observations=min_observations)

    report = engine.build_report(
        prepared.holding_returns,
        prepared.weights,
        benchmark_returns=prepared.benchmark_returns,
        rf=prepared.rf_per_period,
        trading_days=prepared.trading_days,
        conf=conf,
    )

    report["portfolio"] = _portfolio_block(
        prepared.portfolio, prepared.positions, prepared.weights, prepared.excluded
    )
    report["benchmark"] = {
        "ticker": prepared.benchmark_ticker or None,
        "included": prepared.benchmark_included,
    }
    report["warnings"] = list(prepared.warnings)
    return report


def compute_rebalance(
    portfolio_id: int,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    min_observations: int = MIN_OBSERVATIONS,
    n_points: int = optimizer.DEFAULT_FRONTIER_POINTS,
) -> dict:
    """
    What the same holdings would look like at better weights.

    Runs on the SAME prepared inputs as `compute_risk` - same valuation, same
    inner join, same returns matrix - so the "current volatility" here is the
    identical number the risk report shows and the comparison against the
    suggestion is honest. Nothing is re-fetched or re-aligned.

    Only the WEIGHTS change. No ticker is added or dropped: a suggestion the
    investor cannot act on with what they already own is not a suggestion.

    Args:
        portfolio_id: primary key from the URL.
        days, min_observations: as `compute_risk`; keep them in step if you
            override either, or the two endpoints describe different windows.
        n_points: how many efficient-frontier samples to return.

    Returns:
        dict with keys:
            portfolio, tickers, observations, start, end
            current       {weights, volatility, expected_return, sharpe}
            min_variance  {weights, volatility, expected_return, sharpe}
            max_sharpe    {weights, volatility, expected_return, sharpe}
            efficient_frontier  [{risk, return}, ...]
            params, warnings

        Every risk and return figure is ANNUALISED (see `_annualise_return`),
        matching the risk report, so the two pages never disagree about units.

    Raises:
        The same as `compute_risk` (not found / empty-or-unpriceable /
        insufficient history), plus OptimizationError (422) if a solve fails.

    Holdings with no usable price data are excluded rather than fatal, exactly
    as in the risk report, and appear in `excluded` and `warnings`. The
    suggestion then covers the tickers it could actually measure.
    """
    prepared = _prepare(portfolio_id, days=days, min_observations=min_observations)

    returns = prepared.holding_returns
    tickers = [str(column) for column in returns.columns]
    trading_days = prepared.trading_days
    rf = prepared.rf_per_period

    # Per-period moments, exactly as the engine produces them. Annualisation
    # happens once, on the way out - never in storage (engine convention).
    covariance = engine.covariance_matrix(returns)
    means = returns.mean().to_numpy(dtype="float64")

    try:
        min_variance = optimizer.min_variance_weights(covariance)
        max_sharpe = optimizer.max_sharpe_weights(means, covariance, rf=rf)
        frontier = optimizer.efficient_frontier(means, covariance, n_points=n_points)
    except ValueError as exc:
        raise OptimizationError(
            f"Could not solve for an optimal allocation of {', '.join(tickers)}: {exc}",
            details={"tickers": tickers},
        ) from exc

    def describe(weights) -> dict:
        """One allocation, measured with the same functions the report uses."""
        vector = np.asarray(weights, dtype="float64")
        return {
            "weights": {ticker: float(weight) for ticker, weight in zip(tickers, vector)},
            "volatility": engine.portfolio_volatility(vector, covariance, trading_days),
            "expected_return": _annualise_return(float(vector @ means), trading_days),
            "sharpe": engine.sharpe(
                engine.portfolio_return_series(returns, vector),
                rf=rf,
                trading_days=trading_days,
            ),
        }

    warnings = list(prepared.warnings)
    if len(tickers) < 2:
        warnings.append(
            "This portfolio holds a single ticker, so there are no weights to "
            "optimise - diversification needs at least two holdings."
        )

    return {
        "portfolio": {
            "id": prepared.portfolio.pk,
            "name": prepared.portfolio.name,
            "base_currency": prepared.portfolio.base_currency,
        },
        "tickers": tickers,
        "observations": int(len(returns)),
        "start": str(returns.index[0]),
        "end": str(returns.index[-1]),
        "current": describe(prepared.weights),
        "min_variance": describe(min_variance),
        "max_sharpe": describe(max_sharpe),
        "efficient_frontier": [
            {
                "risk": point["risk"] * float(np.sqrt(trading_days)),
                "return": _annualise_return(point["return"], trading_days),
            }
            for point in frontier
        ],
        "params": {
            "rf_per_period": rf,
            "rf_annual": float(settings.RISK_FREE_RATE),
            "trading_days": trading_days,
            "n_points": int(n_points),
        },
        # Holdings this suggestion does not cover, for the same reason the risk
        # report lists them: an allocation over 4 of your 5 positions should say
        # which one it left out.
        "excluded": [entry.as_dict() for entry in prepared.excluded],
        "warnings": warnings,
    }


def compute_performance(
    portfolio_id: int,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    min_observations: int = MIN_OBSERVATIONS,
    start_value: float = REBASE_VALUE,
) -> dict:
    """
    The portfolio's value and drawdown at every date in the window.

    Where `compute_risk` reduces the window to a handful of scalars, this
    endpoint keeps the time axis: the same returns, compounded into a curve,
    plus how far below its own running peak that curve sat on each date.

    Runs on the SAME `_prepare` as the other two endpoints - same valuation,
    same inner join, same window - so the last point of this curve describes
    the same day the risk report describes, and `max_drawdown` below is
    literally the figure that report shows rather than a second computation of
    it. Nothing is re-fetched and nothing is re-aligned.

    Args:
        portfolio_id: primary key from the URL.
        days, min_observations: as `compute_risk`; keep them in step if you
            override either, or the pages describe different windows.
        start_value: where the rebased curve begins (see REBASE_VALUE).

    Returns:
        dict with keys:
            portfolio     {id, name, base_currency}
            dates         ["YYYY-MM-DD", ...] - one per observation
            equity_curve  [float, ...] rebased so the window opens near
                          `start_value` (see `engine.portfolio_equity_curve`
                          for why "near" and not "exactly")
            drawdown_series  [float, ...] PERCENT, e.g. -12.34, never positive
            peak_value, current_value  points on `equity_curve`, for the
                          summary line above the chart
            max_drawdown  FRACTION and negative (-0.25 = a 25% fall), matching
                          the risk report's key of the same name; it equals
                          min(drawdown_series) / 100 by construction
            start_value, observations, start, end, excluded, warnings

        The three lists are parallel and equal-length, which is what lets the
        frontend zip them into one chart without an index lookup.

        Every value is JSON-safe: no NaN, no inf.

    Raises:
        The same as `compute_risk` - not found (404), empty or wholly
        unpriceable portfolio (400), insufficient history (422) - because they
        all originate in `_prepare`, which this shares. A holding with no price
        data is excluded from the curve, not fatal to it.
    """
    prepared = _prepare(portfolio_id, days=days, min_observations=min_observations)

    portfolio_returns = engine.portfolio_return_series(
        prepared.holding_returns, prepared.weights
    )
    curve = engine.portfolio_equity_curve(portfolio_returns, start_value=start_value)
    drawdowns = engine.drawdown_series(curve)

    values = [_json_float(value) for value in curve.to_numpy(dtype="float64")]
    # Percentage points, converted once here rather than in every consumer: the
    # chart's y-axis is labelled in %, and a frontend that has to remember which
    # of two series is a fraction will eventually forget.
    underwater = [
        _json_float(value * 100.0) for value in drawdowns.to_numpy(dtype="float64")
    ]
    dates = _iso_dates(curve.index)

    return {
        "portfolio": {
            "id": prepared.portfolio.pk,
            "name": prepared.portfolio.name,
            "base_currency": prepared.portfolio.base_currency,
        },
        "dates": dates,
        "equity_curve": values,
        "drawdown_series": underwater,
        "peak_value": max(values),
        "current_value": values[-1],
        # From the same curve the series above came from, so the deepest point
        # of the underwater chart and this number cannot drift apart.
        "max_drawdown": engine.max_drawdown(curve),
        "start_value": float(start_value),
        "observations": len(values),
        "start": dates[0],
        "end": dates[-1],
        "excluded": [entry.as_dict() for entry in prepared.excluded],
        "warnings": list(prepared.warnings),
    }


# ---------------------------------------------------------------------------
# Shared preparation - the reason both endpoints agree
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Prepared:
    """
    Everything either endpoint needs, built exactly once and identically.

    Both `compute_risk` and `compute_rebalance` start here, so the rebalance
    page cannot drift into a different window, a different valuation or a
    different inner join from the risk page. Sharing this object is what makes
    "current volatility" mean the same thing on both.
    """

    portfolio: Portfolio
    positions: list[_Position]
    #: Holdings left out of every number in this object. Never empty-by-design:
    #: an empty list is the normal, healthy case.
    excluded: list[_Excluded]
    weights: list[float]
    holding_returns: pd.DataFrame  # columns ordered to match `weights`
    benchmark_ticker: str
    benchmark_returns: pd.Series | None
    benchmark_included: bool
    warnings: list[str]
    trading_days: int
    rf_per_period: float


def _prepare(portfolio_id: int, *, days: int, min_observations: int) -> _Prepared:
    """
    Portfolio -> valued positions -> weights -> aligned returns matrix.

    Every DomainError the three endpoints raise about data originates here,
    which is why they report identical messages for identical problems.

    GRACEFUL DEGRADATION
    --------------------
    Holdings that cannot be priced, or have no history to build returns from,
    are EXCLUDED here rather than raised on. What survives is measured; what did
    not is carried in `excluded` and named in `warnings`. One delisted symbol
    therefore costs the user that symbol, not their dashboard.

    The order matters. Positions are valued first, then filtered again by
    whether a return series exists, and `_weights` runs on the SURVIVORS - so
    the weights sum to 1 over what was actually measured, and the market value
    in the report is the value of the measured subset. Weighting an excluded
    position at zero instead would be arithmetically similar and a lie: it would
    claim we valued something we could not price.

    STILL FATAL
    -----------
    Two things. A portfolio with no holdings at all (`_value_positions`), and a
    portfolio where NOTHING can be measured - handled below with the same
    `empty_portfolio` code, because from the report's point of view those are
    the same situation: there is no exposure it can describe. The message and
    `details.tickers` tell the two apart.

    NOT handled here, deliberately: a ticker with a very SHORT history still
    shrinks the inner join for everyone and can trip `insufficient_history`
    below. Excluding by "usable history" is a clean rule; choosing which subset
    of tickers to drop to maximise an overlap window is a combinatorial guess
    at what the user meant, and a wrong guess would silently change which
    portfolio is being measured. That error names the tickers and stays honest.
    """
    portfolio = get_portfolio(portfolio_id)
    positions, unpriced = _value_positions(portfolio)

    benchmark = (settings.DEFAULT_BENCHMARK_TICKER or "").strip().upper()
    closes, warnings, unhistoried = _close_series(positions, benchmark, days)

    # Only the positions that have BOTH a price and a series survive. Rebuilt by
    # membership in `closes` rather than by removing entries, so `positions` and
    # the returns matrix cannot fall out of step - that pairing is what the
    # weights vector's column order depends on.
    positions = [position for position in positions if position.ticker in closes]
    excluded = unpriced + unhistoried

    if not positions:
        symbols = sorted({entry.ticker for entry in excluded})
        raise EmptyPortfolioError(
            f"None of this portfolio's holdings could be priced "
            f"({', '.join(symbols)}), so there is nothing to measure. "
            f"{_FETCH_HINT}",
            details={"tickers": symbols},
        )

    warnings = _exclusion_warnings(excluded) + warnings
    weights = _weights(positions)
    returns = _returns_matrix(closes, min_observations)

    benchmark_included = bool(benchmark) and benchmark in returns.columns
    # Column order must match the weights vector, and the benchmark is not a
    # holding - reindexing by position ticker enforces both at once. (If the
    # benchmark is also held, it legitimately appears on both sides.)
    holding_returns = returns[[position.ticker for position in positions]]
    benchmark_returns = returns[benchmark] if benchmark_included else None

    trading_days = int(settings.TRADING_DAYS_PER_YEAR)
    return _Prepared(
        portfolio=portfolio,
        positions=positions,
        excluded=excluded,
        weights=weights,
        holding_returns=holding_returns,
        benchmark_ticker=benchmark,
        benchmark_returns=benchmark_returns,
        benchmark_included=benchmark_included,
        warnings=warnings,
        trading_days=trading_days,
        # The engine wants a PER-PERIOD rate; the setting is annualised.
        rf_per_period=float(settings.RISK_FREE_RATE) / trading_days,
    )


def _exclusion_warnings(excluded: list[_Excluded]) -> list[str]:
    """
    The excluded holdings, as sentences a user can act on.

    Grouped by REASON rather than one line per ticker: a portfolio re-imported
    before `fetch_prices` has run can exclude a dozen symbols at once, and
    twelve near-identical banners is not twelve times the information. Within a
    group the tickers are sorted, so the same gap produces the same string on
    every request and the frontend's `key={warning}` stays stable.

    Both messages name the consequence ("excluded from this report") before the
    cause, because the consequence is the part the reader did not already know.
    """
    grouped: dict[str, list[str]] = {}
    for entry in excluded:
        grouped.setdefault(entry.reason, []).append(entry.ticker)

    messages = {
        NO_PRICE: (
            "no stored price, so {they} could not be valued and {are} left out "
            "of the market value and the weights"
        ),
        NO_HISTORY: (
            "no stored price history, so no return series could be built and "
            "{they} {are} left out of every risk figure"
        ),
    }

    warnings: list[str] = []
    for reason, tickers in grouped.items():
        symbols = sorted(set(tickers))
        plural = len(symbols) > 1
        clause = messages[reason].format(
            they="they" if plural else "it",
            are="are" if plural else "is",
        )
        warnings.append(
            f"{', '.join(symbols)} excluded from this report - {clause}. "
            f"{_FETCH_HINT} If the symbol is delisted or wrong, delete or "
            f"correct the holding."
        )
    return warnings


def _json_float(value) -> float:
    """
    NaN/inf -> 0.0, so a response cannot carry a non-JSON constant.

    The engine already guarantees this for its scalars (`_finite`), but the
    performance curve is emitted as raw arrays rather than through
    `build_report`, so the same floor is applied here on the way out.
    """
    number = float(value)
    return number if np.isfinite(number) else 0.0


def _iso_dates(index: pd.Index) -> list[str]:
    """
    A DatetimeIndex -> ["2026-01-05", ...].

    `build_report` emits `str(timestamp)`, which carries a "00:00:00" a daily
    series has no use for and the frontend trims again (see format.js:isoDate).
    A chart axis wants the date and only the date, so it is formatted properly
    once, here.
    """
    return [pd.Timestamp(stamp).strftime("%Y-%m-%d") for stamp in index]


def _annualise_return(per_period: float, trading_days: int) -> float:
    """
    Compound a per-period expected return into an annual one.

    Formula:
        r_annual = (1 + r_period) ** trading_days - 1

    Geometric, not `r * trading_days`: the risk report already presents
    annualised return as compounded growth, and two different annualisation
    conventions on one dashboard is worse than either one alone. A period return
    at or below -100% cannot be compounded, so it degrades to the arithmetic
    form rather than producing a complex number.
    """
    if per_period <= -1.0:
        return float(per_period * trading_days)
    grown = (1.0 + per_period) ** trading_days - 1.0
    return float(grown) if np.isfinite(grown) else float(per_period * trading_days)


# ---------------------------------------------------------------------------
# Valuation and weights - Decimal until the very last step
# ---------------------------------------------------------------------------
def _value_positions(portfolio: Portfolio) -> tuple[list[_Position], list[_Excluded]]:
    """
    Price every holding, preferring the live snapshot over the last close.

    The fallback matters: `fetch_prices --skip-live`, or one symbol whose live
    leg failed, would otherwise make the whole endpoint unusable even though a
    perfectly good close is stored. `price_source` reports which was used.

    Returns (priced, excluded). A holding with NEITHER a live price nor a stored
    close cannot be valued at all, so it is set aside rather than raised on -
    see `_Excluded` for why that changed. The only hard failure left here is a
    portfolio with no holdings in it, which is a different fact entirely.
    """
    holdings: list[Holding] = list(get_holdings(portfolio.pk))
    if not holdings:
        raise EmptyPortfolioError(
            f"Portfolio {portfolio.pk} ('{portfolio.name}') has no holdings, so "
            "there is nothing to measure. Add one at "
            "/admin/portfolio/holding/add/, then run "
            "`python manage.py fetch_prices`."
        )

    tickers = [(holding.ticker or "").strip().upper() for holding in holdings]
    live = get_latest_prices(tickers)
    closes = get_latest_closes([ticker for ticker in tickers if ticker not in live])

    positions: list[_Position] = []
    excluded: list[_Excluded] = []
    for holding, ticker in zip(holdings, tickers):
        price, source = live.get(ticker), "live"
        if price is None:
            price, source = closes.get(ticker), "last_close"
        if price is None:
            excluded.append(
                _Excluded(ticker=ticker, quantity=holding.quantity, reason=NO_PRICE)
            )
            continue
        positions.append(
            _Position(
                ticker=ticker,
                quantity=holding.quantity,
                price=price,
                price_source=source,
            )
        )

    return positions, excluded


def _weights(positions: list[_Position]) -> list[float]:
    """
    Value weights: quantity * price / total portfolio value.

    This is the Decimal -> float64 crossing for the weights vector
    (common/MONEY.md). The division happens in Decimal and `float()` is applied
    once, to a dimensionless ratio - no money value is ever handed to NumPy.
    """
    total = sum((position.market_value for position in positions), ZERO)
    if total <= ZERO:
        raise InvalidInputError(
            "Every holding in this portfolio values at zero, so there are no "
            "weights to compute. Check the quantities, and that the fetched "
            "prices are non-zero."
        )

    weights = [float(position.market_value / total) for position in positions]
    # Each float() can land a few ULPs off; the engine's HHI and portfolio
    # volatility assume the vector sums to 1, so normalise once here.
    scale = sum(weights)
    return [weight / scale for weight in weights]


# ---------------------------------------------------------------------------
# Price series -> aligned returns matrix
# ---------------------------------------------------------------------------
def _close_series(
    positions: list[_Position], benchmark: str, days: int
) -> tuple[dict[str, pd.Series], list[str], list[_Excluded]]:
    """
    Load one close series per ticker, plus the benchmark's.

    Returns (series_by_ticker, warnings, excluded).

    A HOLDING with no stored history used to be fatal, on the reasoning that
    dropping it would silently remove a position from the portfolio. The
    reasoning was right and the remedy was wrong: killing the whole report
    removes ALL of them. It is now excluded and reported - loudly, in
    `warnings`, and still visible in the holdings table - which drops it from
    the maths without dropping it from the user's sight.

    A missing BENCHMARK has always degraded rather than failed: every other
    metric is still valid, so beta becomes null and the reason is a warning.
    """
    series: dict[str, pd.Series] = {}
    excluded: list[_Excluded] = []
    for position in positions:
        frame = get_history_df(position.ticker, days)
        if frame.empty:
            excluded.append(
                _Excluded(
                    ticker=position.ticker,
                    quantity=position.quantity,
                    reason=NO_HISTORY,
                )
            )
            continue
        series[position.ticker] = frame[CLOSE_COLUMN]

    warnings: list[str] = []
    if benchmark and benchmark not in series:
        frame = get_history_df(benchmark, days)
        if frame.empty:
            warnings.append(
                f"No stored price history for the benchmark {benchmark}, so "
                f"beta could not be computed and is null. {_FETCH_HINT}"
            )
        else:
            series[benchmark] = frame[CLOSE_COLUMN]

    return series, warnings, excluded


def _returns_matrix(closes: dict[str, pd.Series], min_observations: int) -> pd.DataFrame:
    """
    Inner-join the close series on date, then difference into daily returns.

    Prices are aligned BEFORE differencing (engine.align_returns' documented
    preference): differencing first would measure each ticker against its own
    previous trading day, which the other tickers may never have traded.
    """
    try:
        aligned = engine.align_returns(closes)
    except ValueError as exc:
        raise InsufficientHistoryError(
            f"{', '.join(closes)} share no overlapping trading dates, so no "
            "returns matrix can be built. Re-fetch them over the same window: "
            "`python manage.py fetch_prices --days 252`.",
            details={"tickers": list(closes)},
        ) from exc

    returns = engine.daily_returns(aligned)
    observations = int(len(returns))
    if observations < min_observations:
        raise InsufficientHistoryError(
            f"Only {observations} overlapping return observation(s) across "
            f"{len(closes)} ticker(s); at least {min_observations} are needed "
            "for the statistics to mean anything. Fetch a longer window: "
            "`python manage.py fetch_prices --days 252`.",
            details={
                "observations": observations,
                "required": int(min_observations),
                "tickers": list(closes),
            },
        )
    return returns


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def _portfolio_block(
    portfolio: Portfolio,
    positions: list[_Position],
    weights: list[float],
    excluded: list[_Excluded],
) -> dict:
    """
    Which portfolio this is, and how it was valued.

    Money is serialised as `str` (common/MONEY.md): exact in transit, and it
    keeps a 4-decimal Decimal from arriving as 1234.5600000000001. Weights stay
    float - they are ratios, not money.

    `holdings` is the MEASURED subset and keeps exactly the shape it always had,
    so the allocation pie, the risk cards and the PDF's table need no changes -
    every row in it still has a price, a market value and a weight that sums
    with the others to 1.

    `excluded` is the separate, additive answer to "then where did TATAMOTORS
    go?". Kept out of `holdings` rather than mixed in with null prices, because
    a consumer that iterated holdings and multiplied by weight would otherwise
    have to learn about a row shape that cannot be arithmetic.
    """
    total = sum((position.market_value for position in positions), ZERO)
    return {
        "id": portfolio.pk,
        "name": portfolio.name,
        "base_currency": portfolio.base_currency,
        # The value of what could be measured. When `excluded` is non-empty this
        # is NOT the whole portfolio, and the warning says so in words.
        "market_value": str(total),
        "excluded": [entry.as_dict() for entry in excluded],
        "holdings": [
            {
                "ticker": position.ticker,
                "quantity": str(position.quantity),
                "price": str(position.price),
                "price_source": position.price_source,
                "market_value": str(position.market_value),
                "weight": weight,
            }
            for position, weight in zip(positions, weights)
        ],
    }


# TODO Phase 5: optimizer results (min-variance / max-Sharpe target weights)
#               hang off this same service, reusing the covariance matrix
#               build_report already computes.
# TODO Phase 6: cache the report per (portfolio, latest PriceHistory date) -
#               the Monte Carlo leg is the expensive part and its inputs only
#               move once a day.
