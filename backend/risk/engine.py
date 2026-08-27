"""
Pure risk mathematics.

Architecture rule 2: this module is PURE. It imports NumPy, pandas and SciPy
and nothing else - no Django, no ORM, no settings, no yfinance, no I/O. Every
function takes arrays/Series/DataFrames and returns numbers, arrays or dicts,
so the whole file is importable and testable standalone:

    >>> from risk.engine import sharpe          # no DJANGO_SETTINGS_MODULE needed

Configuration (risk-free rate, trading calendar) arrives as function arguments
with defaults. `risk/services.py` is the layer allowed to read Django settings
and pass them in.

--------------------------------------------------------------------------
Conventions used throughout
--------------------------------------------------------------------------
Returns
    Simple (arithmetic) returns, not log returns: r_t = P_t / P_(t-1) - 1.
    "Per-period" means one row of the input - daily, for a daily price series.

Risk-free rate
    `rf` is always PER PERIOD, already divided by the trading calendar. An
    annual 6.5% is passed as 0.065 / 252.

Annualisation
    Volatility scales with sqrt(trading_days); returns scale geometrically.
    This assumes returns are i.i.d. - the standard simplification.

Sign of loss metrics
    VaR and CVaR are returned as POSITIVE loss fractions: 0.023 means "a 2.3%
    loss". They are NOT clipped at zero, so a negative VaR legitimately means
    that even the tail of the distribution is a gain.
    max_drawdown is NEGATIVE: -0.25 means the portfolio fell 25% from its peak.

Degrees of freedom
    Sample statistics use ddof=1 (n-1). Covariance and correlation matrices are
    PER-PERIOD; annualise them at the point of use, never in storage.

Undefined results
    A ratio whose denominator is zero (a constant return series has no
    volatility) is undefined. These return 0.0 rather than NaN, because NaN is
    not representable in strict JSON and would break the API envelope. Each
    such case is called out in the relevant docstring.

    "Zero" is tested against _ZERO_TOLERANCE, not against exact 0.0: summing n
    identical floats and dividing by n does not always return that float, so a
    constant series can carry a residual standard deviation around 1e-18. Left
    exact, that turns a 0/0 into a ratio of 1e17 - a meaningless number that
    looks like a real one.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from scipy import stats

# Default calendar and rate. Mirrored by settings.TRADING_DAYS_PER_YEAR and
# settings.RISK_FREE_RATE, which services.py passes in explicitly; duplicated
# here only so the engine has sensible standalone defaults.
TRADING_DAYS: int = 252
DEFAULT_RISK_FREE_ANNUAL: float = 0.065
DEFAULT_RF_PER_PERIOD: float = DEFAULT_RISK_FREE_ANNUAL / TRADING_DAYS
DEFAULT_CONFIDENCE: float = 0.95

# A standard deviation at or below this counts as zero dispersion. Set well
# above floating-point residue (~1e-18 on a constant series) and far below any
# meaningful daily volatility (~1e-3), so it can only ever catch the degenerate
# case it is meant to catch.
_ZERO_TOLERANCE: float = 1e-12


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _as_1d(values: Any, *, name: str = "series") -> np.ndarray:
    """
    Coerce a Series/array/sequence to a 1-D float64 array.

    Raises ValueError on an empty input or on any NaN: silently dropping NaNs
    would break the pairwise alignment that beta and covariance depend on, and
    silently keeping them would poison every downstream metric.
    """
    array = np.asarray(
        values.to_numpy() if isinstance(values, (pd.Series, pd.DataFrame)) else values,
        dtype="float64",
    ).ravel()
    if array.size == 0:
        raise ValueError(f"{name} is empty.")
    if np.isnan(array).any():
        raise ValueError(f"{name} contains NaN; align and clean the series first.")
    return array


def _as_frame(frame: Any, *, name: str = "frame") -> pd.DataFrame:
    """Coerce to a DataFrame and reject empties."""
    result = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    if result.empty:
        raise ValueError(f"{name} is empty.")
    return result


def _as_matrix(matrix: Any, *, name: str = "matrix") -> np.ndarray:
    """Coerce a DataFrame/array to a 2-D float64 array."""
    array = np.asarray(
        matrix.to_numpy() if isinstance(matrix, pd.DataFrame) else matrix, dtype="float64"
    )
    if array.ndim != 2 or array.shape[0] != array.shape[1]:
        raise ValueError(f"{name} must be a square 2-D matrix, got shape {array.shape}.")
    return array


def _finite(value: Any, default: float = 0.0) -> float:
    """Map NaN/inf to `default` so results stay JSON-encodable."""
    number = float(value)
    return number if np.isfinite(number) else default


def _finite_or_none(value: Any) -> float | None:
    """Map NaN/inf to None - for matrix cells, where a fake 0.0 would mislead."""
    number = float(value)
    return number if np.isfinite(number) else None


# ---------------------------------------------------------------------------
# Building the returns matrix
# ---------------------------------------------------------------------------
def daily_returns(prices_df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-period simple returns from a price matrix.

    Formula:
        r_t = P_t / P_(t-1) - 1

    Assumptions:
        - `prices_df` is indexed by date, ascending, one column per ticker, and
          already aligned (see `align_returns`). Prices are adjusted closes, so
          splits and dividends are already handled.
        - The first row becomes NaN by construction and is dropped.
        - Any row still holding a missing observation is dropped, so the result
          is a complete matrix - the precondition of every metric below.

    Returns:
        DataFrame of returns with the same columns, one row shorter (or more,
        if rows had gaps).
    """
    frame = _as_frame(prices_df, name="prices_df").sort_index()
    returns = frame.pct_change().iloc[1:]
    return returns.dropna(how="any")


def align_returns(series_by_ticker: Mapping[str, Any]) -> pd.DataFrame:
    """
    Inner-join several per-ticker series on their date index.

    Formula:
        result_index = intersection of every input index

    Assumptions:
        - Series must NOT be assumed equal-length: an index (^NSEI) and an
          equity trade on different calendars, and a ticker listed midway
          through the window has a shorter history. Only dates present in
          EVERY series survive.
        - Rows with a missing value after the join are dropped.
        - Values may be prices or returns. Prefer aligning PRICES and then
          calling `daily_returns`: differencing after the join measures every
          return over the same interval, whereas differencing first would
          measure each ticker against its own previous trading day, which may
          be a date the other tickers never traded.

    Args:
        series_by_ticker: {ticker: Series} - or {ticker: single-column
            DataFrame}, which is squeezed for convenience, since
            marketdata.selectors.get_history_df returns a "close" frame.

    Returns:
        DataFrame indexed by the common dates (ascending), one column per
        ticker, column order following the mapping's order.

    Raises:
        ValueError: no series given, or the intersection is empty.
    """
    if not series_by_ticker:
        raise ValueError("No series to align.")

    columns: dict[str, pd.Series] = {}
    for ticker, values in series_by_ticker.items():
        series = values
        if isinstance(series, pd.DataFrame):
            if series.shape[1] != 1:
                raise ValueError(
                    f"{ticker}: expected a Series or single-column DataFrame, "
                    f"got {series.shape[1]} columns."
                )
            series = series.iloc[:, 0]
        elif not isinstance(series, pd.Series):
            series = pd.Series(series)
        columns[ticker] = series.astype("float64")

    aligned = pd.concat(columns, axis=1, join="inner").dropna(how="any").sort_index()
    if aligned.empty:
        raise ValueError(
            "Inner join produced no overlapping dates - the series share no common history."
        )
    return aligned


# ---------------------------------------------------------------------------
# Portfolio aggregation
# ---------------------------------------------------------------------------
def portfolio_return_series(R: pd.DataFrame, w: Iterable[float]) -> pd.Series:
    """
    Weighted portfolio return for each period.

    Formula:
        r_p,t = sum_i ( w_i * r_i,t )     ->  r_p = R @ w

    Assumptions:
        - `w` is ordered to match R's columns and sums to 1. Weights are NOT
          renormalised here; a caller passing unnormalised weights gets an
          unnormalised result, which is the honest outcome.
        - Weights are constant over the window (no intra-period rebalancing).

    Returns:
        Series indexed like R, named "portfolio".

    Raises:
        ValueError: length of `w` does not match the number of columns in R.
    """
    frame = _as_frame(R, name="R")
    weights = _as_1d(w, name="w")
    if weights.size != frame.shape[1]:
        raise ValueError(
            f"weights length {weights.size} does not match {frame.shape[1]} asset columns."
        )
    values = frame.to_numpy(dtype="float64") @ weights
    return pd.Series(values, index=frame.index, name="portfolio")


def equity_curve(returns: Any, initial: float = 1.0) -> Any:
    """
    Compound a return series into a wealth index.

    Formula:
        V_t = V_0 * prod_(s<=t) ( 1 + r_s )

    Assumptions:
        - Returns compound geometrically with no cash flows in or out.
        - `initial` is arbitrary; drawdown and CAGR are scale-invariant.

    Returns:
        Series (index preserved) when given a Series, else a 1-D array.
    """
    array = _as_1d(returns, name="returns")
    curve = initial * np.cumprod(1.0 + array)
    if isinstance(returns, pd.Series):
        return pd.Series(curve, index=returns.index, name="equity")
    return curve


def portfolio_equity_curve(portfolio_returns: Any, start_value: float = 100.0) -> Any:
    """
    A portfolio's return series compounded into a REBASED wealth index.

    Formula:
        V_t = start_value * prod_(s<=t) ( 1 + r_s )

    This is `equity_curve` with a conventional starting value instead of 1.0
    and nothing else - the compounding lives in one place. Rebasing to 100
    makes the curve unit-free, so a chart of it can never be misread as a rupee
    valuation and two portfolios of very different size still share one axis.

    Assumptions:
        - Input is the PORTFOLIO return series (`portfolio_return_series`), not
          the per-asset matrix.
        - Weights are constant across the window and no cash moves in or out,
          inherited from `portfolio_return_series`.
        - The first point is the value AFTER the first return, so the curve
          opens at start_value * (1 + r_0) rather than at start_value itself.
          A return series has no row for the day it is measured FROM, and
          `start_value` belongs to that prior close, which is outside the
          window. This is deliberate rather than an off-by-one: it makes the
          curve bit-for-bit the one `build_report` already hands to
          `max_drawdown`, so an underwater chart drawn from it and the
          max-drawdown figure on the report are the same number - not two that
          nearly agree.

    Returns:
        Series (index preserved) when given a Series, else a 1-D array.
    """
    return equity_curve(portfolio_returns, initial=start_value)


# ---------------------------------------------------------------------------
# Dispersion
# ---------------------------------------------------------------------------
def covariance_matrix(R: pd.DataFrame) -> pd.DataFrame:
    """
    Per-period sample covariance between assets.

    Formula:
        Sigma_ij = sum_t ( (r_i,t - mu_i)(r_j,t - mu_j) ) / (n - 1)

    Assumptions:
        - PER-PERIOD (daily), not annualised. Multiply by `trading_days` to
          annualise - `portfolio_volatility` does this for you.
        - ddof=1 (sample, not population).
        - R is already aligned and NaN-free.

    Returns:
        Square DataFrame labelled by ticker on both axes.
    """
    return _as_frame(R, name="R").cov()


def correlation_matrix(R: pd.DataFrame) -> pd.DataFrame:
    """
    Pearson correlation between assets.

    Formula:
        rho_ij = Sigma_ij / (sigma_i * sigma_j)

    Assumptions:
        - Scale-free, so annualisation is irrelevant.
        - A constant column has zero variance and yields NaN against every
          other asset - correlation is genuinely undefined there.

    Returns:
        Square DataFrame with 1.0 on the diagonal.
    """
    return _as_frame(R, name="R").corr()


def annualized_volatility(returns: Any, trading_days: int = TRADING_DAYS) -> float:
    """
    Annualised standard deviation of a return series.

    Formula:
        sigma_annual = stdev(r, ddof=1) * sqrt(trading_days)

    Assumptions:
        - Returns are i.i.d., so variance scales linearly with time and
          volatility with its square root.
        - A series of fewer than two observations has no dispersion: 0.0.
        - A constant series gives exactly 0.0 (see _ZERO_TOLERANCE).
    """
    array = _as_1d(returns, name="returns")
    if array.size < 2:
        return 0.0
    deviation = float(np.std(array, ddof=1))
    if deviation <= _ZERO_TOLERANCE:
        return 0.0
    return _finite(deviation * np.sqrt(trading_days))


def portfolio_volatility(
    w: Iterable[float], cov: pd.DataFrame, trading_days: int = TRADING_DAYS
) -> float:
    """
    Annualised volatility of the weighted portfolio.

    Formula:
        sigma_p = sqrt( w' Sigma w ) * sqrt(trading_days)

    Assumptions:
        - `cov` is the PER-PERIOD covariance matrix from `covariance_matrix`,
          and `w` is ordered to match its axes.
        - This is the diversified volatility: it is generally LOWER than the
          weighted average of individual volatilities, and equals it only when
          every pairwise correlation is 1.
        - Tiny negative variances from floating-point noise are clamped to 0.

    Raises:
        ValueError: `w` length does not match the covariance matrix.
    """
    weights = _as_1d(w, name="w")
    matrix = _as_matrix(cov, name="cov")
    if weights.size != matrix.shape[0]:
        raise ValueError(
            f"weights length {weights.size} does not match covariance matrix "
            f"of size {matrix.shape[0]}."
        )
    variance = float(weights @ matrix @ weights)
    deviation = float(np.sqrt(max(variance, 0.0)))
    if deviation <= _ZERO_TOLERANCE:
        return 0.0
    return _finite(deviation * np.sqrt(trading_days))


# ---------------------------------------------------------------------------
# Risk-adjusted performance
# ---------------------------------------------------------------------------
def beta(port_ret: Any, bench_ret: Any) -> float:
    """
    Sensitivity of the portfolio to its benchmark.

    Formula:
        beta = Cov(r_p, r_b) / Var(r_b)          (ddof=1)

    Assumptions:
        - Both series are aligned to the SAME dates and equal length. Align
          them with `align_returns` before calling; a length mismatch raises
          rather than silently truncating.
        - beta of a series against itself is exactly 1.
        - A benchmark with zero variance makes beta undefined; returns 0.0.
        - Annualisation is irrelevant: beta is a ratio of like-scaled moments.
    """
    portfolio = _as_1d(port_ret, name="port_ret")
    benchmark = _as_1d(bench_ret, name="bench_ret")
    if portfolio.size != benchmark.size:
        raise ValueError(
            f"port_ret ({portfolio.size}) and bench_ret ({benchmark.size}) must be "
            "the same length; inner-join them on date first."
        )
    if portfolio.size < 2:
        return 0.0
    if float(np.std(benchmark, ddof=1)) <= _ZERO_TOLERANCE:
        return 0.0
    benchmark_variance = float(np.var(benchmark, ddof=1))
    covariance = float(np.cov(portfolio, benchmark, ddof=1)[0, 1])
    return _finite(covariance / benchmark_variance)


def sharpe(
    returns: Any,
    rf: float = DEFAULT_RF_PER_PERIOD,
    trading_days: int = TRADING_DAYS,
) -> float:
    """
    Excess return per unit of total volatility, annualised.

    Formula:
        excess_t = r_t - rf
        Sharpe   = mean(excess) / stdev(excess, ddof=1) * sqrt(trading_days)

    Assumptions:
        - `rf` is PER PERIOD (annual 6.5% -> 0.065/252), matching `returns`.
        - Dispersion is measured on EXCESS returns, not raw returns. With a
          constant rf the two are identical, but the excess form is correct if
          a varying rate is ever passed.
        - Penalises upside and downside symmetrically - that is what
          `sortino` exists to fix.
        - Undefined for a constant series (zero dispersion): returns 0.0.
    """
    excess = _as_1d(returns, name="returns") - rf
    if excess.size < 2:
        return 0.0
    deviation = float(np.std(excess, ddof=1))
    if deviation <= _ZERO_TOLERANCE:
        return 0.0
    return _finite(float(np.mean(excess)) / deviation * np.sqrt(trading_days))


def sortino(
    returns: Any,
    rf: float = DEFAULT_RF_PER_PERIOD,
    trading_days: int = TRADING_DAYS,
) -> float:
    """
    Excess return per unit of DOWNSIDE volatility, annualised.

    Formula:
        excess_t   = r_t - rf
        downside_t = min(excess_t, 0)
        DD         = sqrt( sum(downside_t^2) / n )        # n = ALL observations
        Sortino    = mean(excess) / DD * sqrt(trading_days)

    Assumptions:
        - `rf` is PER PERIOD and doubles as the minimum acceptable return.
        - Downside deviation divides by the FULL observation count n, not by
          the number of losing periods - the standard "target semideviation".
          The variant that divides by the loss count gives a smaller
          denominator and a flatteringly larger ratio; this is not that.
        - Because DD <= total stdev, Sortino >= Sharpe for any series with
          upside dispersion.
        - No losing period means DD = 0 and the ratio is undefined: 0.0.
    """
    excess = _as_1d(returns, name="returns") - rf
    if excess.size < 2:
        return 0.0
    downside = np.minimum(excess, 0.0)
    downside_deviation = float(np.sqrt(np.mean(downside**2)))
    if downside_deviation <= _ZERO_TOLERANCE:
        return 0.0
    return _finite(float(np.mean(excess)) / downside_deviation * np.sqrt(trading_days))


def max_drawdown(equity: Any) -> float:
    """
    Worst peak-to-trough decline of a wealth curve.

    Formula:
        peak_t = max(V_0 .. V_t)
        dd_t   = V_t / peak_t - 1
        MDD    = min(dd_t)

    Assumptions:
        - Input is an EQUITY CURVE (cumulative wealth), not a return series -
          build one with `equity_curve`.
        - Result is NEGATIVE or zero: -0.25 means a 25% fall from the peak.
        - A monotonically rising curve gives exactly 0.0.
        - Measured on close-to-close values, so intraday extremes are invisible.
    """
    curve = _as_1d(equity, name="equity")
    if curve.size < 2:
        return 0.0
    running_peak = np.maximum.accumulate(curve)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(running_peak != 0, curve / running_peak - 1.0, 0.0)
    return _finite(np.min(drawdowns))


def drawdown_series(equity: Any) -> Any:
    """
    Drawdown at EVERY date - the per-date form of `max_drawdown`.

    Formula:
        peak_t = max(V_0 .. V_t)
        dd_t   = V_t / peak_t - 1

    `max_drawdown` is min(dd_t) over exactly this array. The running peak, the
    zero-peak guard and the non-finite fallback are written the same way here
    on purpose, so that

        min(drawdown_series(curve)) == max_drawdown(curve)

    holds rather than nearly holds. test_engine.py asserts it, which is what
    lets an "underwater" chart and the max-drawdown card quote one number.

    Assumptions:
        - Input is an EQUITY CURVE (cumulative wealth), not a return series -
          build one with `equity_curve` or `portfolio_equity_curve`. The scale
          is irrelevant; a drawdown is a ratio.
        - Every value is NEGATIVE or zero, and exactly 0.0 at each new peak.
          That is what pins the curve to the zero line at the highs and puts it
          underwater everywhere else.
        - Measured close-to-close, so an intraday trough is invisible.
        - A non-finite ratio (a zero or infinite peak) degrades to 0.0 for that
          date, mirroring what `_finite` does to the scalar.
        - A single-point curve is its own peak, so the result is [0.0] - and
          min() of it is 0.0, which is what `max_drawdown` returns there too.

    Returns:
        Series (index preserved) when given a Series, else a 1-D array.
    """
    curve = _as_1d(equity, name="equity")
    running_peak = np.maximum.accumulate(curve)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(running_peak != 0, curve / running_peak - 1.0, 0.0)
    drawdowns = np.where(np.isfinite(drawdowns), drawdowns, 0.0)
    if isinstance(equity, pd.Series):
        return pd.Series(drawdowns, index=equity.index, name="drawdown")
    return drawdowns


# ---------------------------------------------------------------------------
# Tail risk
# ---------------------------------------------------------------------------
def var_historical(port_ret: Any, conf: float = DEFAULT_CONFIDENCE) -> float:
    """
    Value at Risk from the empirical distribution.

    Formula:
        VaR = -quantile(r, 1 - conf)

    Assumptions:
        - Non-parametric: the sample IS the distribution, so fat tails and
          skew are captured exactly as they occurred - but nothing worse than
          the worst observed loss can ever be predicted.
        - PER-PERIOD (one row of `port_ret`, i.e. a 1-day VaR for daily data).
        - Returned POSITIVE as a loss fraction; 0.023 means "a 2.3% loss at
          this confidence". Not clipped, so an all-gains tail gives a
          negative VaR.
        - Uses NumPy's default linear interpolation between order statistics.

    Raises:
        ValueError: `conf` outside (0, 1).
    """
    array = _as_1d(port_ret, name="port_ret")
    _validate_confidence(conf)
    return _finite(-np.quantile(array, 1.0 - conf))


def var_parametric(port_ret: Any, conf: float = DEFAULT_CONFIDENCE) -> float:
    """
    Value at Risk assuming normally distributed returns.

    Formula:
        z   = Phi^-1(1 - conf)                   # negative for conf > 0.5
        VaR = -( mu + z * sigma )                # mu, sigma from the sample

    Assumptions:
        - Returns are Gaussian. Real markets are fat-tailed and left-skewed,
          so this UNDERSTATES tail risk relative to `var_historical` on real
          data; the two converge on genuinely normal samples.
        - sigma uses ddof=1; PER-PERIOD, like the input.
        - Returned POSITIVE as a loss fraction.

    Raises:
        ValueError: `conf` outside (0, 1).
    """
    array = _as_1d(port_ret, name="port_ret")
    _validate_confidence(conf)
    if array.size < 2:
        return 0.0
    mean = float(np.mean(array))
    deviation = float(np.std(array, ddof=1))
    z_score = float(stats.norm.ppf(1.0 - conf))
    return _finite(-(mean + z_score * deviation))


def var_montecarlo(
    w: Iterable[float],
    mean_vec: Iterable[float],
    cov: pd.DataFrame,
    conf: float = DEFAULT_CONFIDENCE,
    n_sims: int = 10_000,
    horizon: int = 1,
    seed: int | None = 42,
) -> float:
    """
    Value at Risk by simulating correlated asset returns.

    Formula:
        X ~ MultivariateNormal( mu * horizon, Sigma * horizon )   # n_sims draws
        r_p = X @ w
        VaR = -quantile(r_p, 1 - conf)

    Assumptions:
        - Joint normality with the sample's mean vector and covariance. Same
          thin-tail caveat as `var_parametric`, but it respects the
          correlation structure across assets.
        - i.i.d. returns, so scaling to `horizon` periods multiplies both mean
          and covariance by `horizon` (the "square root of time" rule, applied
          to variance).
        - `seed` makes the result reproducible; pass None for a fresh draw.
          A fixed seed matters here - an unseeded VaR that moves every refresh
          is indistinguishable from a bug.
        - `w`, `mean_vec` and `cov` must share one asset ordering.
        - Returned POSITIVE as a loss fraction.

    Raises:
        ValueError: shape mismatch, non-positive `n_sims`/`horizon`, or `conf`
            outside (0, 1).
    """
    weights = _as_1d(w, name="w")
    means = _as_1d(mean_vec, name="mean_vec")
    matrix = _as_matrix(cov, name="cov")
    _validate_confidence(conf)

    if not (weights.size == means.size == matrix.shape[0]):
        raise ValueError(
            f"w ({weights.size}), mean_vec ({means.size}) and cov "
            f"({matrix.shape[0]}) must describe the same assets."
        )
    if n_sims <= 0:
        raise ValueError("n_sims must be positive.")
    if horizon <= 0:
        raise ValueError("horizon must be positive.")

    rng = np.random.default_rng(seed)
    draws = rng.multivariate_normal(means * horizon, matrix * horizon, size=n_sims)
    simulated = draws @ weights
    return _finite(-np.quantile(simulated, 1.0 - conf))


def cvar(port_ret: Any, conf: float = DEFAULT_CONFIDENCE) -> float:
    """
    Conditional VaR (expected shortfall): the average loss GIVEN the VaR
    threshold is breached.

    Formula:
        q    = quantile(r, 1 - conf)
        CVaR = -mean( r | r <= q )

    Assumptions:
        - Non-parametric, from the same empirical sample as `var_historical`.
        - CVaR >= VaR always: it averages the tail rather than cutting it off,
          which is why it is the coherent risk measure of the two.
        - If no observation falls at or below the threshold (a very small
          sample), falls back to the VaR value itself.
        - Returned POSITIVE as a loss fraction.

    Raises:
        ValueError: `conf` outside (0, 1).
    """
    array = _as_1d(port_ret, name="port_ret")
    _validate_confidence(conf)
    threshold = float(np.quantile(array, 1.0 - conf))
    tail = array[array <= threshold]
    if tail.size == 0:
        return _finite(-threshold)
    return _finite(-float(np.mean(tail)))


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------
def hhi(weights: Iterable[float]) -> float:
    """
    Herfindahl-Hirschman Index of portfolio concentration.

    Formula:
        HHI = sum_i ( w_i^2 )

    Assumptions:
        - Weights are non-negative and sum to 1 (long-only, fully invested).
        - Range is 1/n (perfectly equal-weighted across n holdings) to 1.0
          (everything in a single position). Equal weights therefore give
          exactly 1/n.
        - 1/HHI is the "effective number of holdings": a portfolio with a 90%
          position and ten 1% positions scores near 1, however many lines it
          shows.
    """
    array = _as_1d(weights, name="weights")
    return _finite(np.sum(array**2))


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def build_report(
    R: pd.DataFrame,
    w: Iterable[float],
    benchmark_returns: Any = None,
    rf: float = DEFAULT_RF_PER_PERIOD,
    trading_days: int = TRADING_DAYS,
    conf: float = DEFAULT_CONFIDENCE,
    n_sims: int = 10_000,
    horizon: int = 1,
    seed: int | None = 42,
) -> dict:
    """
    Compute every metric for one portfolio and assemble the RiskReport.

    Args:
        R: aligned per-period returns, one column per ticker (see
            `align_returns` then `daily_returns`).
        w: value weights ordered to match R's columns, summing to 1.
        benchmark_returns: benchmark series aligned to R's index. Required for
            beta; when omitted, "beta" is None rather than a fabricated 0.
        rf: risk-free rate PER PERIOD.
        trading_days: periods per year, for annualisation.
        conf: VaR/CVaR confidence, e.g. 0.95.
        n_sims, horizon, seed: Monte Carlo controls.

    Assumptions:
        - Every metric inherits the assumptions of the function that computes
          it; see each docstring.
        - Weights are constant across the window (no rebalancing).
        - Every value is JSON-safe: NaN and inf never appear. Scalar ratios
          degrade to 0.0 when undefined; matrix cells degrade to None, since a
          fabricated 0.0 correlation would read as a real measurement.

    Returns:
        dict with keys:
            observations, start, end, tickers, weights
            annualized_return, annualized_volatility
            beta, sharpe, sortino, max_drawdown
            var_historical, var_parametric, var_montecarlo, cvar
            hhi, effective_holdings
            per_asset_volatility, correlation_matrix, covariance_matrix
            params

    Raises:
        ValueError: R is empty, or `w` does not match R's columns.
    """
    frame = _as_frame(R, name="R")
    weights = _as_1d(w, name="w")
    if weights.size != frame.shape[1]:
        raise ValueError(
            f"weights length {weights.size} does not match {frame.shape[1]} asset columns."
        )

    tickers = [str(column) for column in frame.columns]
    portfolio = portfolio_return_series(frame, weights)
    curve = equity_curve(portfolio)
    covariance = covariance_matrix(frame)
    correlation = correlation_matrix(frame)
    concentration = hhi(weights)

    observations = int(portfolio.size)
    # Geometric (CAGR-style) annualisation, not a naive mean * 252: compounding
    # is what an investor actually experiences.
    total_growth = float(np.prod(1.0 + portfolio.to_numpy(dtype="float64")))
    annualized_return = (
        _finite(total_growth ** (trading_days / observations) - 1.0)
        if observations > 0 and total_growth > 0
        else 0.0
    )

    return {
        # -- window ---------------------------------------------------------
        "observations": observations,
        "start": str(frame.index[0]),
        "end": str(frame.index[-1]),
        "tickers": tickers,
        "weights": {ticker: _finite(weight) for ticker, weight in zip(tickers, weights)},
        # -- return and dispersion ------------------------------------------
        "annualized_return": annualized_return,
        "annualized_volatility": portfolio_volatility(weights, covariance, trading_days),
        # -- risk-adjusted ---------------------------------------------------
        "beta": (
            beta(portfolio, benchmark_returns) if benchmark_returns is not None else None
        ),
        "sharpe": sharpe(portfolio, rf=rf, trading_days=trading_days),
        "sortino": sortino(portfolio, rf=rf, trading_days=trading_days),
        "max_drawdown": max_drawdown(curve),
        # -- tail risk -------------------------------------------------------
        "var_historical": var_historical(portfolio, conf=conf),
        "var_parametric": var_parametric(portfolio, conf=conf),
        "var_montecarlo": var_montecarlo(
            weights,
            frame.mean().to_numpy(dtype="float64"),
            covariance,
            conf=conf,
            n_sims=n_sims,
            horizon=horizon,
            seed=seed,
        ),
        "cvar": cvar(portfolio, conf=conf),
        # -- concentration ---------------------------------------------------
        "hhi": concentration,
        "effective_holdings": _finite(1.0 / concentration) if concentration > 0 else 0.0,
        # -- matrices --------------------------------------------------------
        "per_asset_volatility": {
            ticker: annualized_volatility(frame[column], trading_days=trading_days)
            for ticker, column in zip(tickers, frame.columns)
        },
        "correlation_matrix": _matrix_to_dict(correlation),
        "covariance_matrix": _matrix_to_dict(covariance),
        # -- provenance ------------------------------------------------------
        "params": {
            "rf_per_period": float(rf),
            "trading_days": int(trading_days),
            "confidence": float(conf),
            "n_sims": int(n_sims),
            "horizon": int(horizon),
            "seed": seed,
        },
    }


def _matrix_to_dict(matrix: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    """Nested {row: {col: value}} with non-finite cells as None (JSON-safe)."""
    return {
        str(row): {str(col): _finite_or_none(matrix.loc[row, col]) for col in matrix.columns}
        for row in matrix.index
    }


def _validate_confidence(conf: float) -> None:
    if not 0.0 < conf < 1.0:
        raise ValueError(f"conf must be strictly between 0 and 1, got {conf}.")


__all__ = [
    "TRADING_DAYS",
    "DEFAULT_RF_PER_PERIOD",
    "DEFAULT_CONFIDENCE",
    "daily_returns",
    "align_returns",
    "portfolio_return_series",
    "equity_curve",
    "portfolio_equity_curve",
    "covariance_matrix",
    "correlation_matrix",
    "annualized_volatility",
    "portfolio_volatility",
    "beta",
    "sharpe",
    "sortino",
    "max_drawdown",
    "drawdown_series",
    "var_historical",
    "var_parametric",
    "var_montecarlo",
    "cvar",
    "hhi",
    "build_report",
]
