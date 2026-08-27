"""
Markowitz mean-variance optimisation.

Same contract as `engine.py` (architecture rule 2): PURE. NumPy and SciPy only -
no Django, no ORM, no settings, no pandas import. Every function takes arrays
and returns arrays or plain floats, so the module is importable and testable
standalone:

    >>> from risk.optimizer import min_variance_weights   # no Django needed

A pandas DataFrame may still be passed in: `np.asarray` converts one without
this module importing pandas, so `engine.covariance_matrix(...)` output drops
straight in with its column order preserved.

--------------------------------------------------------------------------
Conventions
--------------------------------------------------------------------------
Units
    Every function is UNIT-AGNOSTIC. Feed it per-period (daily) moments and you
    get per-period risk and return back; feed it annualised moments and you get
    annualised results. Nothing here annualises anything - `risk/services.py`
    does that at the point of presentation, exactly as it does for the engine's
    covariance matrix.

Constraints
    Long-only and fully invested throughout:

        sum(w) = 1,   w_i >= 0

    No shorting, no leverage, no per-asset cap. That matches what a retail
    investor can actually execute with the holdings they already own, which is
    what the rebalance suggestion is for.

Weight ordering
    Weights come back in the covariance matrix's own axis order, so the caller
    can zip them against the same ticker list it built the matrix from.

Failure
    A solve that does not converge raises ValueError rather than returning a
    plausible-looking vector. `services.py` turns that into a clean envelope.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import minimize

#: How many points `efficient_frontier` samples by default.
DEFAULT_FRONTIER_POINTS: int = 20

#: SLSQP settings. maxiter is generous because these problems are small and
#: convex; ftol is tight because portfolio variances are O(1e-4) and the default
#: 1e-6 would stop while the weights were still visibly wrong.
_SOLVER_OPTIONS = {"maxiter": 500, "ftol": 1e-12}

#: Below this, a spread of expected returns is a rounding artefact rather than a
#: real range to sweep a frontier across.
_RETURN_TOLERANCE: float = 1e-15

#: Variance at or below this counts as zero dispersion (mirrors the engine).
_ZERO_TOLERANCE: float = 1e-12


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _as_cov(cov: Any) -> np.ndarray:
    """Coerce to a square 2-D float64 covariance matrix."""
    matrix = np.asarray(cov, dtype="float64")
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError(f"cov must be a square 2-D matrix, got shape {matrix.shape}.")
    if matrix.shape[0] == 0:
        raise ValueError("cov is empty.")
    if not np.isfinite(matrix).all():
        raise ValueError("cov contains NaN or inf; clean the returns matrix first.")
    return matrix


def _as_mean(mean_vec: Any, size: int) -> np.ndarray:
    """Coerce expected returns to a 1-D float64 vector matching `size` assets."""
    vector = np.asarray(mean_vec, dtype="float64").ravel()
    if vector.size != size:
        raise ValueError(
            f"mean_vec has {vector.size} entries but the covariance matrix "
            f"describes {size} assets."
        )
    if not np.isfinite(vector).all():
        raise ValueError("mean_vec contains NaN or inf.")
    return vector


def _equal_weights(n: int) -> np.ndarray:
    """The starting point for every solve: 1/n across the board."""
    return np.full(n, 1.0 / n)


def _sum_to_one() -> dict:
    """The fully-invested equality constraint, with its exact gradient."""
    return {"type": "eq", "fun": lambda w: np.sum(w) - 1.0, "jac": lambda w: np.ones_like(w)}


def _finalise(result: Any, *, label: str) -> np.ndarray:
    """
    Turn a SciPy result into a clean weights vector.

    SLSQP satisfies its constraints to within its own tolerance, so the raw
    vector routinely carries -3e-18 entries and sums to 1 +/- 1e-16. Both are
    numerical residue, not decisions: clip and renormalise once here so callers
    never have to reason about a "negative" weight in a long-only portfolio.
    """
    if not result.success:
        raise ValueError(f"{label} did not converge: {result.message}")

    weights = np.clip(np.asarray(result.x, dtype="float64"), 0.0, None)
    total = float(weights.sum())
    if total <= 0.0 or not np.isfinite(total):
        raise ValueError(f"{label} produced an unusable weights vector.")
    return weights / total


def _variance(weights: np.ndarray, cov: np.ndarray) -> float:
    """w' Sigma w, floored at zero against floating-point noise."""
    return max(float(weights @ cov @ weights), 0.0)


# ---------------------------------------------------------------------------
# Single-portfolio optimisers
# ---------------------------------------------------------------------------
def min_variance_weights(cov: Any) -> np.ndarray:
    """
    The long-only portfolio with the smallest possible variance.

    Formula:
        minimise    w' Sigma w
        subject to  sum(w) = 1,  w_i >= 0

    Assumptions:
        - `cov` is a covariance matrix in any consistent unit; the result is a
          set of weights, which is unitless either way.
        - Long-only. Shorting would generally reach a lower variance, so this
          answer is the best EXECUTABLE one, not the unconstrained optimum.
        - Expected returns are deliberately ignored: this is the one point on
          the frontier that needs no return forecast, which is why it is the
          suggestion the dashboard leads with.
        - The objective is convex and the feasible set is a simplex, so the
          minimum is global - SLSQP cannot land on a local trap here.

    Args:
        cov: (n, n) covariance matrix. A DataFrame is accepted; its axis order
            is preserved in the result.

    Returns:
        (n,) float64 weights, non-negative and summing to 1.

    Raises:
        ValueError: `cov` is not square/finite, or the solve failed.
    """
    matrix = _as_cov(cov)
    size = matrix.shape[0]
    if size == 1:
        # One asset: fully invested means fully invested in it. SLSQP would
        # reach the same answer, but not asking is cheaper and cannot fail.
        return np.ones(1)

    result = minimize(
        lambda w: _variance(w, matrix),
        _equal_weights(size),
        method="SLSQP",
        jac=lambda w: 2.0 * matrix @ w,
        bounds=[(0.0, 1.0)] * size,
        constraints=[_sum_to_one()],
        options=_SOLVER_OPTIONS,
    )
    return _finalise(result, label="min_variance_weights")


def max_sharpe_weights(mean_vec: Any, cov: Any, rf: float = 0.0) -> np.ndarray:
    """
    The long-only tangency portfolio: the best return per unit of risk.

    Formula:
        maximise    (w' mu - rf) / sqrt(w' Sigma w)
        subject to  sum(w) = 1,  w_i >= 0

        Solved as the minimisation of the negated ratio.

    Assumptions:
        - `rf` is PER PERIOD and in the same unit as `mean_vec`: pass
          annual_rate / trading_days alongside daily means, never the annual
          rate alongside daily means.
        - Expected returns are the historical sample means of the window. That
          is the standard Markowitz input and also its standard weakness - mean
          estimates are far noisier than covariance estimates, so this portfolio
          moves around a lot more between windows than the min-variance one.
        - When no long-only portfolio beats `rf`, every achievable Sharpe is
          negative and the solver returns the least-bad mix. That is a real
          answer about a bad market, not an error.
        - The ratio is not convex in w, so SLSQP finds a local optimum. From an
          equal-weight start on a simplex this is the global one in practice;
          it is not guaranteed the way min-variance is.

    Args:
        mean_vec: (n,) expected per-period returns, ordered to match `cov`.
        cov: (n, n) covariance matrix.
        rf: per-period risk-free rate.

    Returns:
        (n,) float64 weights, non-negative and summing to 1.

    Raises:
        ValueError: shapes disagree, inputs are not finite, or the solve failed.
    """
    matrix = _as_cov(cov)
    size = matrix.shape[0]
    means = _as_mean(mean_vec, size)
    rate = float(rf)

    if size == 1:
        return np.ones(1)

    def negative_sharpe(weights: np.ndarray) -> float:
        deviation = np.sqrt(_variance(weights, matrix))
        if deviation <= _ZERO_TOLERANCE:
            # A riskless mix has an infinite ratio; return a large finite
            # penalty-free value so the solver keeps moving instead of hitting
            # inf and stalling.
            return -1e12 * float(weights @ means - rate)
        return -float((weights @ means - rate) / deviation)

    result = minimize(
        negative_sharpe,
        _equal_weights(size),
        method="SLSQP",
        bounds=[(0.0, 1.0)] * size,
        constraints=[_sum_to_one()],
        options=_SOLVER_OPTIONS,
    )
    return _finalise(result, label="max_sharpe_weights")


# ---------------------------------------------------------------------------
# The frontier
# ---------------------------------------------------------------------------
def efficient_frontier(
    mean_vec: Any, cov: Any, n_points: int = DEFAULT_FRONTIER_POINTS
) -> list[dict[str, float]]:
    """
    The efficient half of the Markowitz frontier, as (risk, return) points.

    Formula:
        for each target return mu* spanning
            [ mu of the min-variance portfolio ,  max(mu_i) ]:

            minimise    w' Sigma w
            subject to  w' mu = mu*,  sum(w) = 1,  w_i >= 0

        Each solve contributes { "risk": sqrt(w' Sigma w), "return": w' mu }.

    Assumptions:
        - The sweep starts at the min-variance portfolio, so every point
          returned is EFFICIENT: nothing on this curve is beaten on both risk
          and return by another point. The lower, inefficient half of the
          hyperbola is deliberately not returned.
        - The top end is max(mu_i), which under long-only constraints is the
          all-in-one-asset portfolio - the highest return reachable without
          leverage.
        - Risk and return come back in the input's unit (per-period in,
          per-period out).
        - An individual target that fails to solve is skipped rather than
          aborting the sweep: a frontier with 19 of 20 points still draws
          correctly, and a partial curve beats a 500.

    Args:
        mean_vec: (n,) expected per-period returns, ordered to match `cov`.
        cov: (n, n) covariance matrix.
        n_points: how many targets to sample. Values below 2 yield 1 point.

    Returns:
        A list of {"risk": float, "return": float}, ascending in risk. Degenerate
        inputs (a single asset, or assets whose expected returns are identical)
        collapse to one point rather than repeating the same one `n_points`
        times.

    Raises:
        ValueError: shapes disagree, inputs are not finite, or the anchoring
            min-variance solve failed.
    """
    matrix = _as_cov(cov)
    size = matrix.shape[0]
    means = _as_mean(mean_vec, size)
    count = max(int(n_points), 1)

    def point(weights: np.ndarray) -> dict[str, float]:
        return {
            "risk": float(np.sqrt(_variance(weights, matrix))),
            "return": float(weights @ means),
        }

    # The frontier's left-hand anchor. Everything to its left is unreachable;
    # everything below it on the same risk is inefficient by definition.
    floor_weights = min_variance_weights(matrix)
    floor = point(floor_weights)

    if size == 1 or count == 1:
        return [floor]

    lowest_return = floor["return"]
    highest_return = float(means.max())
    if highest_return - lowest_return <= _RETURN_TOLERANCE:
        # Every asset promises the same thing: the frontier is a single point,
        # and pretending otherwise would draw a line through one value.
        return [floor]

    points = [floor]
    # Skip the first target: it IS the min-variance portfolio, already anchored.
    for target in np.linspace(lowest_return, highest_return, count)[1:]:
        result = minimize(
            lambda w: _variance(w, matrix),
            _equal_weights(size),
            method="SLSQP",
            jac=lambda w: 2.0 * matrix @ w,
            bounds=[(0.0, 1.0)] * size,
            constraints=[
                _sum_to_one(),
                {
                    "type": "eq",
                    "fun": lambda w, t=target: float(w @ means - t),
                    "jac": lambda w, _=None: means,
                },
            ],
            options=_SOLVER_OPTIONS,
        )
        if result.success:
            points.append(point(np.clip(result.x, 0.0, None) / max(result.x.sum(), 1e-16)))

    points.sort(key=lambda entry: entry["risk"])
    return points


__all__ = [
    "DEFAULT_FRONTIER_POINTS",
    "min_variance_weights",
    "max_sharpe_weights",
    "efficient_frontier",
]
