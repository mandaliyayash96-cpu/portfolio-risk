"""
Unit tests for risk/engine.py.

Every metric is checked against either a hand-computed value or a mathematical
property that must hold regardless of implementation (beta of a series with
itself is 1; HHI of equal weights is 1/n; a constant series has zero
volatility; a monotonically rising curve has zero drawdown).

These tests import NO Django. TestEnginePurity enforces that the engine does
the same.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from risk import engine
from risk.engine import (
    align_returns,
    annualized_volatility,
    beta,
    build_report,
    correlation_matrix,
    covariance_matrix,
    cvar,
    daily_returns,
    equity_curve,
    hhi,
    max_drawdown,
    portfolio_return_series,
    portfolio_volatility,
    sharpe,
    sortino,
    var_historical,
    var_montecarlo,
    var_parametric,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _dates(n: int, start: str = "2026-01-01") -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.date_range(start, periods=n, freq="D"), name="date")


@pytest.fixture
def normal_returns() -> pd.Series:
    """1000 draws from a genuinely normal distribution - the case where the
    parametric and historical VaR estimates should agree."""
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.0005, scale=0.01, size=1000)
    return pd.Series(values, index=_dates(1000), name="portfolio")


@pytest.fixture
def two_asset_returns() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            "AAA.NS": rng.normal(0.0006, 0.012, 500),
            "BBB.NS": rng.normal(0.0004, 0.009, 500),
        },
        index=_dates(500),
    )


@pytest.fixture
def benchmark_returns() -> pd.Series:
    rng = np.random.default_rng(11)
    return pd.Series(rng.normal(0.0005, 0.010, 500), index=_dates(500), name="^NSEI")


# ---------------------------------------------------------------------------
# daily_returns
# ---------------------------------------------------------------------------
class TestDailyReturns:
    def test_hand_computed(self):
        prices = pd.DataFrame({"AAA.NS": [100.0, 110.0, 99.0]}, index=_dates(3))
        result = daily_returns(prices)
        # 110/100-1 = +0.10 ; 99/110-1 = -0.10
        assert result["AAA.NS"].tolist() == pytest.approx([0.10, -0.10])

    def test_drops_the_first_row(self):
        prices = pd.DataFrame({"AAA.NS": [1.0, 2.0, 3.0, 4.0]}, index=_dates(4))
        assert len(daily_returns(prices)) == 3

    def test_drops_rows_with_a_gap(self):
        prices = pd.DataFrame({"AAA.NS": [100.0, np.nan, 120.0, 130.0]}, index=_dates(4))
        result = daily_returns(prices)
        # Rows touching the gap are unusable; only 130/120-1 survives.
        assert len(result) == 1
        assert result["AAA.NS"].iloc[0] == pytest.approx(130 / 120 - 1)

    def test_preserves_columns_and_index(self):
        prices = pd.DataFrame(
            {"AAA.NS": [1.0, 2.0, 3.0], "BBB.NS": [10.0, 11.0, 12.0]}, index=_dates(3)
        )
        result = daily_returns(prices)
        assert list(result.columns) == ["AAA.NS", "BBB.NS"]
        assert result.index.equals(prices.index[1:])

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            daily_returns(pd.DataFrame())


# ---------------------------------------------------------------------------
# align_returns  (the inner-join requirement)
# ---------------------------------------------------------------------------
class TestAlignReturns:
    def test_inner_joins_mismatched_calendars(self):
        """^NSEI trades on days the equity does not; only shared dates survive."""
        equity = pd.Series([100.0, 101.0, 102.0], index=_dates(3, "2026-01-02"))
        index_series = pd.Series([20000.0, 20100.0, 20200.0, 20300.0], index=_dates(4, "2026-01-01"))

        aligned = align_returns({"AAA.NS": equity, "^NSEI": index_series})

        assert len(aligned) == 3  # 2026-01-02 .. 2026-01-04
        assert list(aligned.columns) == ["AAA.NS", "^NSEI"]
        assert str(aligned.index[0].date()) == "2026-01-02"
        assert str(aligned.index[-1].date()) == "2026-01-04"

    def test_no_overlap_raises(self):
        first = pd.Series([1.0, 2.0], index=_dates(2, "2026-01-01"))
        second = pd.Series([3.0, 4.0], index=_dates(2, "2026-06-01"))
        with pytest.raises(ValueError, match="no overlapping dates"):
            align_returns({"AAA.NS": first, "BBB.NS": second})

    def test_accepts_single_column_frames(self):
        """marketdata.selectors.get_history_df hands back a 'close' frame."""
        frame = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=_dates(3))
        aligned = align_returns({"AAA.NS": frame, "BBB.NS": frame})
        assert list(aligned.columns) == ["AAA.NS", "BBB.NS"]
        assert len(aligned) == 3

    def test_rejects_multi_column_frames(self):
        frame = pd.DataFrame({"close": [1.0, 2.0], "open": [1.0, 2.0]}, index=_dates(2))
        with pytest.raises(ValueError, match="single-column"):
            align_returns({"AAA.NS": frame})

    def test_sorts_ascending(self):
        series = pd.Series([3.0, 1.0, 2.0], index=_dates(3)[::-1])
        aligned = align_returns({"AAA.NS": series})
        assert aligned.index.is_monotonic_increasing

    def test_empty_mapping_raises(self):
        with pytest.raises(ValueError, match="No series"):
            align_returns({})


# ---------------------------------------------------------------------------
# portfolio_return_series / equity_curve
# ---------------------------------------------------------------------------
class TestPortfolioReturnSeries:
    def test_hand_computed(self):
        R = pd.DataFrame({"AAA.NS": [0.01, 0.02], "BBB.NS": [0.03, -0.02]}, index=_dates(2))
        result = portfolio_return_series(R, [0.5, 0.5])
        # (0.01+0.03)/2 = 0.02 ; (0.02-0.02)/2 = 0.00
        assert result.tolist() == pytest.approx([0.02, 0.0])

    def test_single_asset_is_the_column_itself(self):
        R = pd.DataFrame({"AAA.NS": [0.01, -0.02, 0.03]}, index=_dates(3))
        assert portfolio_return_series(R, [1.0]).tolist() == pytest.approx(R["AAA.NS"].tolist())

    def test_preserves_index_and_name(self):
        R = pd.DataFrame({"AAA.NS": [0.01, 0.02]}, index=_dates(2))
        result = portfolio_return_series(R, [1.0])
        assert result.index.equals(R.index)
        assert result.name == "portfolio"

    def test_weight_length_mismatch_raises(self):
        R = pd.DataFrame({"AAA.NS": [0.01], "BBB.NS": [0.02]}, index=_dates(1))
        with pytest.raises(ValueError, match="does not match"):
            portfolio_return_series(R, [1.0])


class TestEquityCurve:
    def test_compounds(self):
        assert equity_curve(np.array([0.1, 0.1])).tolist() == pytest.approx([1.1, 1.21])

    def test_series_in_series_out(self):
        returns = pd.Series([0.1, 0.1], index=_dates(2))
        curve = equity_curve(returns)
        assert isinstance(curve, pd.Series)
        assert curve.index.equals(returns.index)

    def test_respects_initial_value(self):
        assert equity_curve(np.array([0.5]), initial=200.0).tolist() == pytest.approx([300.0])


# ---------------------------------------------------------------------------
# covariance / correlation
# ---------------------------------------------------------------------------
class TestCovarianceAndCorrelation:
    def test_correlation_diagonal_is_one(self, two_asset_returns):
        corr = correlation_matrix(two_asset_returns)
        assert np.allclose(np.diag(corr.to_numpy()), 1.0)

    def test_perfectly_correlated_series(self):
        base = np.array([0.01, -0.02, 0.03, 0.005])
        R = pd.DataFrame({"AAA.NS": base, "BBB.NS": base * 2.0}, index=_dates(4))
        assert correlation_matrix(R).loc["AAA.NS", "BBB.NS"] == pytest.approx(1.0)

    def test_perfectly_anticorrelated_series(self):
        base = np.array([0.01, -0.02, 0.03, 0.005])
        R = pd.DataFrame({"AAA.NS": base, "BBB.NS": -base}, index=_dates(4))
        assert correlation_matrix(R).loc["AAA.NS", "BBB.NS"] == pytest.approx(-1.0)

    def test_covariance_matches_numpy(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        expected = np.cov(two_asset_returns.to_numpy(), rowvar=False, ddof=1)
        assert np.allclose(cov.to_numpy(), expected)

    def test_covariance_is_symmetric(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns).to_numpy()
        assert np.allclose(cov, cov.T)


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------
class TestAnnualizedVolatility:
    def test_constant_series_has_zero_volatility(self):
        assert annualized_volatility(np.full(50, 0.01)) == 0.0

    def test_matches_the_formula(self):
        values = np.array([0.01, -0.02, 0.015, 0.0, 0.03])
        expected = np.std(values, ddof=1) * np.sqrt(TRADING_DAYS)
        assert annualized_volatility(values) == pytest.approx(expected)

    def test_scales_with_sqrt_of_calendar(self):
        values = np.array([0.01, -0.02, 0.015, 0.0, 0.03])
        # 252 periods/yr vs 63: volatility must double (sqrt(4) = 2).
        assert annualized_volatility(values, trading_days=252) == pytest.approx(
            2.0 * annualized_volatility(values, trading_days=63)
        )

    def test_single_observation_is_zero(self):
        assert annualized_volatility(np.array([0.01])) == 0.0

    def test_nan_input_raises(self):
        with pytest.raises(ValueError, match="NaN"):
            annualized_volatility(np.array([0.01, np.nan]))


class TestPortfolioVolatility:
    def test_single_asset_equals_that_asset(self):
        R = pd.DataFrame({"AAA.NS": [0.01, -0.02, 0.03, 0.0]}, index=_dates(4))
        cov = covariance_matrix(R)
        assert portfolio_volatility([1.0], cov) == pytest.approx(
            annualized_volatility(R["AAA.NS"])
        )

    def test_perfectly_correlated_assets_do_not_diversify(self):
        """With rho = 1 the portfolio vol equals the weighted average vol."""
        base = np.array([0.01, -0.02, 0.03, 0.005, 0.012])
        R = pd.DataFrame({"AAA.NS": base, "BBB.NS": base * 2.0}, index=_dates(5))
        cov = covariance_matrix(R)
        weights = [0.5, 0.5]
        weighted_average = 0.5 * annualized_volatility(R["AAA.NS"]) + 0.5 * annualized_volatility(
            R["BBB.NS"]
        )
        assert portfolio_volatility(weights, cov) == pytest.approx(weighted_average)

    def test_imperfect_correlation_diversifies(self, two_asset_returns):
        """rho < 1 must give portfolio vol strictly below the weighted average."""
        cov = covariance_matrix(two_asset_returns)
        weights = [0.5, 0.5]
        weighted_average = sum(
            0.5 * annualized_volatility(two_asset_returns[col]) for col in two_asset_returns
        )
        assert portfolio_volatility(weights, cov) < weighted_average

    def test_dimension_mismatch_raises(self, two_asset_returns):
        with pytest.raises(ValueError, match="does not match"):
            portfolio_volatility([1.0], covariance_matrix(two_asset_returns))


# ---------------------------------------------------------------------------
# beta
# ---------------------------------------------------------------------------
class TestBeta:
    def test_beta_against_itself_is_one(self, normal_returns):
        assert beta(normal_returns, normal_returns) == pytest.approx(1.0)

    def test_double_the_benchmark_is_beta_two(self, normal_returns):
        assert beta(normal_returns * 2.0, normal_returns) == pytest.approx(2.0)

    def test_inverse_of_the_benchmark_is_beta_minus_one(self, normal_returns):
        assert beta(-normal_returns, normal_returns) == pytest.approx(-1.0)

    def test_constant_benchmark_is_zero(self):
        portfolio = np.array([0.01, -0.02, 0.03])
        assert beta(portfolio, np.full(3, 0.001)) == 0.0

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="same length"):
            beta(np.array([0.01, 0.02, 0.03]), np.array([0.01, 0.02]))


# ---------------------------------------------------------------------------
# sharpe / sortino
# ---------------------------------------------------------------------------
class TestSharpe:
    def test_matches_the_formula(self):
        values = np.array([0.01, 0.02, -0.01, 0.03])
        expected = np.mean(values) / np.std(values, ddof=1) * np.sqrt(TRADING_DAYS)
        assert sharpe(values, rf=0.0) == pytest.approx(expected)

    def test_constant_series_is_zero(self):
        """Zero dispersion makes the ratio undefined; 0.0, never NaN."""
        assert sharpe(np.full(20, 0.01), rf=0.0) == 0.0

    def test_higher_risk_free_rate_lowers_sharpe(self, normal_returns):
        assert sharpe(normal_returns, rf=0.0) > sharpe(normal_returns, rf=0.0005)

    def test_negative_mean_gives_negative_sharpe(self):
        assert sharpe(np.array([-0.01, -0.02, 0.005, -0.03]), rf=0.0) < 0

    def test_annualisation_scaling(self):
        values = np.array([0.01, 0.02, -0.01, 0.03])
        assert sharpe(values, rf=0.0, trading_days=252) == pytest.approx(
            2.0 * sharpe(values, rf=0.0, trading_days=63)
        )


class TestSortino:
    def test_matches_the_formula(self):
        values = np.array([0.01, 0.02, -0.01, 0.03])
        downside = np.minimum(values, 0.0)
        expected = (
            np.mean(values) / np.sqrt(np.mean(downside**2)) * np.sqrt(TRADING_DAYS)
        )
        assert sortino(values, rf=0.0) == pytest.approx(expected)

    def test_exceeds_sharpe_when_upside_dispersion_exists(self, normal_returns):
        """Downside deviation <= total stdev, so Sortino >= Sharpe."""
        assert sortino(normal_returns, rf=0.0) > sharpe(normal_returns, rf=0.0)

    def test_no_losing_period_is_zero(self):
        """No downside at all makes the denominator zero; undefined -> 0.0."""
        assert sortino(np.array([0.01, 0.02, 0.03]), rf=0.0) == 0.0

    def test_ignores_upside_magnitude(self):
        """Making a winning period bigger cannot worsen downside deviation."""
        modest = np.array([0.01, -0.02, 0.01])
        spectacular = np.array([0.50, -0.02, 0.01])
        assert sortino(spectacular, rf=0.0) > sortino(modest, rf=0.0)


# ---------------------------------------------------------------------------
# max_drawdown
# ---------------------------------------------------------------------------
class TestMaxDrawdown:
    def test_monotonic_rise_has_no_drawdown(self):
        assert max_drawdown(np.array([100.0, 110.0, 120.0, 130.0])) == 0.0

    def test_hand_computed(self):
        # Peak 120 -> trough 90 = -25%. The later rise to 150 does not undo it.
        assert max_drawdown(np.array([100.0, 120.0, 90.0, 150.0])) == pytest.approx(-0.25)

    def test_is_never_positive(self, normal_returns):
        assert max_drawdown(equity_curve(normal_returns)) <= 0.0

    def test_total_loss_is_minus_one(self):
        assert max_drawdown(np.array([100.0, 0.0])) == pytest.approx(-1.0)

    def test_single_point_is_zero(self):
        assert max_drawdown(np.array([100.0])) == 0.0


# ---------------------------------------------------------------------------
# VaR family
# ---------------------------------------------------------------------------
class TestVarHistorical:
    #: Ten evenly spaced returns; np.quantile interpolates linearly.
    SAMPLE = np.array([-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04])

    def test_hand_computed(self):
        # quantile position = 0.10 * (10-1) = 0.9 -> between -0.05 and -0.04
        # -0.05 + 0.9 * 0.01 = -0.041 -> VaR = +0.041
        assert var_historical(self.SAMPLE, conf=0.90) == pytest.approx(0.041)

    def test_higher_confidence_means_higher_var(self, normal_returns):
        assert var_historical(normal_returns, conf=0.99) > var_historical(
            normal_returns, conf=0.95
        )

    def test_all_gains_gives_negative_var(self):
        """Not clipped: a tail that is still a gain is reported honestly."""
        assert var_historical(np.array([0.01, 0.02, 0.03, 0.04]), conf=0.95) < 0

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="strictly between 0 and 1"):
            var_historical(self.SAMPLE, conf=1.5)


class TestVarParametric:
    def test_matches_the_formula(self):
        from scipy import stats

        values = np.array([0.01, -0.02, 0.015, 0.0, 0.03, -0.01])
        expected = -(
            np.mean(values) + stats.norm.ppf(0.05) * np.std(values, ddof=1)
        )
        assert var_parametric(values, conf=0.95) == pytest.approx(expected)

    def test_agrees_with_historical_on_normal_data(self, normal_returns):
        """The Gaussian assumption holds here, so the two must nearly agree."""
        historical = var_historical(normal_returns, conf=0.95)
        parametric = var_parametric(normal_returns, conf=0.95)
        assert parametric == pytest.approx(historical, rel=0.10)

    def test_understates_risk_when_losses_cluster_at_the_threshold(self):
        """
        Losses concentrated AT the 5% threshold are the case the Gaussian fit
        misses: 30 of 510 observations (5.9%) sit at -5%, so the empirical
        quantile lands squarely on them, while they are too mild to drag the
        fitted sigma out that far.

        Note the opposite holds for a handful of EXTREME outliers: a few -20%
        days sit beyond the 95% threshold and inflate sigma enough to make the
        parametric estimate the larger of the two. Which estimate is more
        conservative depends on the shape of the tail, which is precisely why
        the report carries both.
        """
        rng = np.random.default_rng(3)
        values = np.concatenate([rng.normal(0.0005, 0.008, 480), np.full(30, -0.05)])
        assert var_historical(values, conf=0.95) > var_parametric(values, conf=0.95)

    def test_higher_confidence_means_higher_var(self, normal_returns):
        assert var_parametric(normal_returns, conf=0.99) > var_parametric(
            normal_returns, conf=0.95
        )


class TestVarMonteCarlo:
    def test_is_reproducible_with_a_seed(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        means = two_asset_returns.mean().to_numpy()
        first = var_montecarlo([0.5, 0.5], means, cov, seed=42)
        second = var_montecarlo([0.5, 0.5], means, cov, seed=42)
        assert first == second

    def test_different_seeds_give_different_draws(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        means = two_asset_returns.mean().to_numpy()
        assert var_montecarlo([0.5, 0.5], means, cov, seed=1) != var_montecarlo(
            [0.5, 0.5], means, cov, seed=2
        )

    def test_converges_on_the_parametric_answer(self, normal_returns):
        """One asset drawn from its own fitted normal: MC must reproduce the
        closed-form parametric VaR to within sampling error."""
        values = normal_returns.to_numpy()
        cov = np.array([[np.var(values, ddof=1)]])
        simulated = var_montecarlo([1.0], [np.mean(values)], cov, n_sims=200_000, seed=5)
        assert simulated == pytest.approx(var_parametric(values, conf=0.95), rel=0.03)

    def test_horizon_scaling_increases_var(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        means = two_asset_returns.mean().to_numpy()
        one_day = var_montecarlo([0.5, 0.5], means, cov, horizon=1, seed=9)
        ten_day = var_montecarlo([0.5, 0.5], means, cov, horizon=10, seed=9)
        assert ten_day > one_day

    def test_shape_mismatch_raises(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        with pytest.raises(ValueError, match="same assets"):
            var_montecarlo([1.0], [0.001], cov)

    def test_non_positive_sims_raises(self, two_asset_returns):
        cov = covariance_matrix(two_asset_returns)
        with pytest.raises(ValueError, match="n_sims must be positive"):
            var_montecarlo([0.5, 0.5], [0.001, 0.001], cov, n_sims=0)


class TestCVaR:
    SAMPLE = TestVarHistorical.SAMPLE

    def test_hand_computed(self):
        # Threshold at conf 0.90 is -0.041; only -0.05 sits at or below it.
        assert cvar(self.SAMPLE, conf=0.90) == pytest.approx(0.05)

    def test_never_below_var(self, normal_returns):
        """Expected shortfall averages the tail, so it cannot be gentler
        than the cut-off that defines the tail."""
        assert cvar(normal_returns, conf=0.95) >= var_historical(normal_returns, conf=0.95)

    def test_higher_confidence_means_higher_cvar(self, normal_returns):
        assert cvar(normal_returns, conf=0.99) > cvar(normal_returns, conf=0.95)

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="strictly between 0 and 1"):
            cvar(self.SAMPLE, conf=0.0)


# ---------------------------------------------------------------------------
# hhi
# ---------------------------------------------------------------------------
class TestHHI:
    @pytest.mark.parametrize("n", [1, 2, 4, 10])
    def test_equal_weights_give_one_over_n(self, n):
        assert hhi(np.full(n, 1.0 / n)) == pytest.approx(1.0 / n)

    def test_single_position_is_one(self):
        assert hhi([1.0]) == pytest.approx(1.0)

    def test_concentration_increases_the_index(self):
        assert hhi([0.9, 0.05, 0.05]) > hhi([1 / 3, 1 / 3, 1 / 3])

    def test_bounded_between_one_over_n_and_one(self):
        weights = np.array([0.5, 0.3, 0.2])
        assert 1 / 3 <= hhi(weights) <= 1.0


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------
class TestBuildReport:
    EXPECTED_KEYS = {
        "observations",
        "start",
        "end",
        "tickers",
        "weights",
        "annualized_return",
        "annualized_volatility",
        "beta",
        "sharpe",
        "sortino",
        "max_drawdown",
        "var_historical",
        "var_parametric",
        "var_montecarlo",
        "cvar",
        "hhi",
        "effective_holdings",
        "per_asset_volatility",
        "correlation_matrix",
        "covariance_matrix",
        "params",
    }

    def test_contains_every_metric(self, two_asset_returns, benchmark_returns):
        report = build_report(two_asset_returns, [0.6, 0.4], benchmark_returns)
        assert set(report) == self.EXPECTED_KEYS

    def test_is_strictly_json_serialisable(self, two_asset_returns, benchmark_returns):
        """allow_nan=False is the real test: NaN/Infinity are not valid JSON
        and would break the API envelope in Phase 4."""
        report = build_report(two_asset_returns, [0.6, 0.4], benchmark_returns)
        json.dumps(report, allow_nan=False)

    def test_beta_is_none_without_a_benchmark(self, two_asset_returns):
        assert build_report(two_asset_returns, [0.5, 0.5])["beta"] is None

    def test_beta_is_computed_with_a_benchmark(self, two_asset_returns, benchmark_returns):
        report = build_report(two_asset_returns, [0.5, 0.5], benchmark_returns)
        portfolio = portfolio_return_series(two_asset_returns, [0.5, 0.5])
        assert report["beta"] == pytest.approx(beta(portfolio, benchmark_returns))

    def test_single_asset_portfolio(self):
        """Edge case: one holding. Every matrix collapses to 1x1."""
        rng = np.random.default_rng(21)
        R = pd.DataFrame({"AAA.NS": rng.normal(0.0005, 0.01, 300)}, index=_dates(300))
        report = build_report(R, [1.0])

        assert report["tickers"] == ["AAA.NS"]
        assert report["hhi"] == pytest.approx(1.0)
        assert report["effective_holdings"] == pytest.approx(1.0)
        assert report["annualized_volatility"] == pytest.approx(
            annualized_volatility(R["AAA.NS"])
        )
        assert report["correlation_matrix"] == {"AAA.NS": {"AAA.NS": pytest.approx(1.0)}}
        json.dumps(report, allow_nan=False)

    def test_metrics_match_their_standalone_functions(self, two_asset_returns):
        weights = [0.7, 0.3]
        report = build_report(two_asset_returns, weights)
        portfolio = portfolio_return_series(two_asset_returns, weights)

        assert report["sharpe"] == pytest.approx(sharpe(portfolio))
        assert report["sortino"] == pytest.approx(sortino(portfolio))
        assert report["cvar"] == pytest.approx(cvar(portfolio))
        assert report["var_historical"] == pytest.approx(var_historical(portfolio))
        assert report["max_drawdown"] == pytest.approx(max_drawdown(equity_curve(portfolio)))
        assert report["hhi"] == pytest.approx(hhi(weights))

    def test_effective_holdings_is_n_for_equal_weights(self, two_asset_returns):
        report = build_report(two_asset_returns, [0.5, 0.5])
        assert report["effective_holdings"] == pytest.approx(2.0)

    def test_records_the_window_and_parameters(self, two_asset_returns):
        report = build_report(two_asset_returns, [0.5, 0.5], rf=0.0001, conf=0.99, seed=7)
        assert report["observations"] == len(two_asset_returns)
        assert report["params"]["rf_per_period"] == 0.0001
        assert report["params"]["confidence"] == 0.99
        assert report["params"]["seed"] == 7
        assert report["start"] < report["end"]

    def test_weight_mismatch_raises(self, two_asset_returns):
        with pytest.raises(ValueError, match="does not match"):
            build_report(two_asset_returns, [1.0])

    def test_constant_returns_stay_json_safe(self):
        """A degenerate portfolio must not produce NaN anywhere."""
        R = pd.DataFrame({"AAA.NS": np.full(50, 0.001)}, index=_dates(50))
        report = build_report(R, [1.0])
        assert report["sharpe"] == 0.0
        assert report["annualized_volatility"] == 0.0
        # Correlation of a constant series is genuinely undefined -> None.
        assert report["correlation_matrix"]["AAA.NS"]["AAA.NS"] is None
        json.dumps(report, allow_nan=False)


# ---------------------------------------------------------------------------
# End-to-end through the pure pipeline
# ---------------------------------------------------------------------------
class TestPipeline:
    def test_mismatched_calendars_flow_through_to_a_report(self):
        """The Phase 2 shape: ^NSEI has extra trading days the equities lack.
        Align first, difference second, then compute."""
        rng = np.random.default_rng(4)
        # Overlap is trimmed at BOTH ends: the equities start later than the
        # index and run past its last date.
        equity_dates = _dates(200, "2026-01-06")  # 2026-01-06 .. 2026-07-24
        index_dates = _dates(190, "2026-01-01")  # 2026-01-01 .. 2026-07-09

        prices = {
            "AAA.NS": pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.01, 200)), equity_dates),
            "BBB.NS": pd.Series(250 * np.cumprod(1 + rng.normal(0.0004, 0.012, 200)), equity_dates),
            "^NSEI": pd.Series(24000 * np.cumprod(1 + rng.normal(0.0003, 0.008, 190)), index_dates),
        }

        aligned_prices = align_returns(prices)
        assert len(aligned_prices) == 185  # 2026-01-06 .. 2026-07-09
        assert len(aligned_prices) < min(len(equity_dates), len(index_dates))
        assert str(aligned_prices.index[0].date()) == "2026-01-06"
        assert str(aligned_prices.index[-1].date()) == "2026-07-09"

        returns = daily_returns(aligned_prices)
        assert len(returns) == 184  # one row lost to differencing

        asset_returns = returns[["AAA.NS", "BBB.NS"]]
        report = build_report(asset_returns, [0.6, 0.4], returns["^NSEI"])

        assert report["observations"] == 184
        assert report["tickers"] == ["AAA.NS", "BBB.NS"]
        assert report["beta"] is not None
        json.dumps(report, allow_nan=False)


# ---------------------------------------------------------------------------
# Architecture rule 2: the engine is pure
# ---------------------------------------------------------------------------
class TestEnginePurity:
    BANNED_ROOTS = {
        "django",
        "rest_framework",
        "yfinance",
        "marketdata",
        "portfolio",
        "alerts",
        "common",
        "config",
    }
    ALLOWED_ROOTS = {"__future__", "typing", "numpy", "pandas", "scipy"}

    def _imported_roots(self) -> set[str]:
        source = (BACKEND_DIR / "risk" / "engine.py").read_text(encoding="utf-8")
        roots: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        return roots

    def test_imports_nothing_banned(self):
        assert not self._imported_roots() & self.BANNED_ROOTS

    def test_imports_only_the_expected_libraries(self):
        assert self._imported_roots() <= self.ALLOWED_ROOTS

    def test_imports_standalone_without_django(self):
        """Import the engine in a fresh interpreter with no Django settings
        configured, and assert Django never even reaches sys.modules."""
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        env["PYTHONPATH"] = str(BACKEND_DIR)
        # Single-threaded BLAS: a nested interpreter spinning up its own thread
        # pool alongside the test runner's exhausts OpenBLAS's buffers on
        # Windows. Irrelevant to what this test checks, which is imports.
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import risk.engine as e; "
                "print('django' in sys.modules, e.sharpe([0.01, 0.02, -0.01], rf=0.0) != 0)",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(BACKEND_DIR),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False True"

    def test_module_exposes_every_required_metric(self):
        required = {
            "daily_returns",
            "align_returns",
            "portfolio_return_series",
            "covariance_matrix",
            "correlation_matrix",
            "annualized_volatility",
            "portfolio_volatility",
            "beta",
            "sharpe",
            "sortino",
            "max_drawdown",
            "var_historical",
            "var_parametric",
            "var_montecarlo",
            "cvar",
            "hhi",
            "build_report",
        }
        assert required <= set(engine.__all__)
        for name in required:
            assert callable(getattr(engine, name))

    def test_every_public_function_documents_its_formula(self):
        """Architecture rule 6. build_report is exempt from Formula: - it
        composes the functions below it rather than defining maths of its own."""
        for name in engine.__all__:
            attribute = getattr(engine, name)
            if not callable(attribute):
                continue
            doc = attribute.__doc__ or ""
            assert "Assumptions:" in doc, f"{name} has no Assumptions: section"
            if name != "build_report":
                assert "Formula:" in doc, f"{name} has no Formula: section"
