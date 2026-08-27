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

Both endpoints share `_prepare()`, so they cannot disagree about the window,
the valuation or the join.
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
    MissingPriceDataError,
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

            "portfolio"  {id, name, base_currency, market_value, holdings[]}
            "benchmark"  {ticker, included}
            "warnings"   list[str] - degradations the caller should see

        Every value is JSON-safe: the engine guarantees no NaN/inf, and money
        is emitted as strings (common/MONEY.md).

    Raises:
        NotFoundError (404):            no such portfolio.
        EmptyPortfolioError (400):      the portfolio holds nothing.
        MissingPriceDataError (422):    a held ticker has no stored price/history.
        InsufficientHistoryError (422): too little overlapping history.
        InvalidInputError (400):        the portfolio values at zero.
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
        prepared.portfolio, prepared.positions, prepared.weights
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
        The same four as `compute_risk` (not found / empty / missing prices /
        insufficient history), plus OptimizationError (422) if a solve fails.
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
        "warnings": warnings,
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

    Every DomainError the two endpoints raise about data originates here, which
    is why they report identical messages for identical problems.
    """
    portfolio = get_portfolio(portfolio_id)
    positions = _value_positions(portfolio)
    weights = _weights(positions)

    benchmark = (settings.DEFAULT_BENCHMARK_TICKER or "").strip().upper()
    closes, warnings = _close_series(positions, benchmark, days)
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
def _value_positions(portfolio: Portfolio) -> list[_Position]:
    """
    Price every holding, preferring the live snapshot over the last close.

    The fallback matters: `fetch_prices --skip-live`, or one symbol whose live
    leg failed, would otherwise make the whole endpoint unusable even though a
    perfectly good close is stored. `price_source` reports which was used.
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
    missing: list[str] = []
    for holding, ticker in zip(holdings, tickers):
        price, source = live.get(ticker), "live"
        if price is None:
            price, source = closes.get(ticker), "last_close"
        if price is None:
            missing.append(ticker)
            continue
        positions.append(
            _Position(
                ticker=ticker,
                quantity=holding.quantity,
                price=price,
                price_source=source,
            )
        )

    if missing:
        symbols = sorted(set(missing))
        raise MissingPriceDataError(
            f"No stored price for {', '.join(symbols)}, so the portfolio cannot "
            f"be valued and weights cannot be computed. {_FETCH_HINT}",
            details={"tickers": symbols},
        )
    return positions


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
) -> tuple[dict[str, pd.Series], list[str]]:
    """
    Load one close series per ticker, plus the benchmark's.

    Returns (series_by_ticker, warnings). A missing HOLDING series is fatal: it
    would silently drop a position out of the portfolio. A missing BENCHMARK is
    not - every other metric is still valid, so beta degrades to null and the
    reason is reported in `warnings` instead of failing the whole request.
    """
    series: dict[str, pd.Series] = {}
    missing: list[str] = []
    for position in positions:
        frame = get_history_df(position.ticker, days)
        if frame.empty:
            missing.append(position.ticker)
            continue
        series[position.ticker] = frame[CLOSE_COLUMN]

    if missing:
        symbols = sorted(set(missing))
        raise MissingPriceDataError(
            f"No stored price history for {', '.join(symbols)}, so no return "
            f"series can be built for {'them' if len(symbols) > 1 else 'it'}. "
            f"{_FETCH_HINT}",
            details={"tickers": symbols},
        )

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

    return series, warnings


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
    portfolio: Portfolio, positions: list[_Position], weights: list[float]
) -> dict:
    """
    Which portfolio this is, and how it was valued.

    Money is serialised as `str` (common/MONEY.md): exact in transit, and it
    keeps a 4-decimal Decimal from arriving as 1234.5600000000001. Weights stay
    float - they are ratios, not money.
    """
    total = sum((position.market_value for position in positions), ZERO)
    return {
        "id": portfolio.pk,
        "name": portfolio.name,
        "base_currency": portfolio.base_currency,
        "market_value": str(total),
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
