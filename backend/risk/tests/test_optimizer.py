"""
Unit tests for risk/optimizer.py.

Like the engine tests, these import NO Django. Every assertion is either a
closed-form value the maths must reproduce, or a property that has to hold no
matter how the solver is implemented:

  * weights are a valid long-only allocation (non-negative, summing to 1),
  * the min-variance portfolio is at least as calm as any single asset in it,
  * the tangency portfolio has the best Sharpe on the frontier,
  * the frontier is monotone and anchored at the min-variance point.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from risk import optimizer
from risk.optimizer import efficient_frontier, max_sharpe_weights, min_variance_weights

BACKEND_DIR = Path(__file__).resolve().parents[2]

#: SLSQP is iterative; equality constraints hold to solver tolerance, not to
#: machine epsilon. Tight enough to catch a real error, loose enough not to flap.
TOL = 1e-6


# ---------------------------------------------------------------------------
# Fixtures - covariance matrices with known structure
# ---------------------------------------------------------------------------
def cov_from(vols, corr):
    """Build a covariance matrix from volatilities and a correlation matrix."""
    deviations = np.asarray(vols, dtype="float64")
    return np.outer(deviations, deviations) * np.asarray(corr, dtype="float64")


@pytest.fixture
def two_uncorrelated():
    """Vols 0.20 and 0.10, zero correlation - the textbook worked example."""
    return cov_from([0.20, 0.10], [[1.0, 0.0], [0.0, 1.0]])


@pytest.fixture
def three_assets():
    """Three assets with mixed correlations; nothing degenerate."""
    return cov_from(
        [0.25, 0.18, 0.12],
        [
            [1.0, 0.30, 0.10],
            [0.30, 1.0, -0.20],
            [0.10, -0.20, 1.0],
        ],
    )


@pytest.fixture
def realistic():
    """A five-asset covariance drawn from a seeded return sample."""
    rng = np.random.default_rng(11)
    returns = rng.normal(0.0006, 0.012, size=(500, 5))
    returns[:, 1] += 0.4 * returns[:, 0]  # give the matrix real off-diagonals
    returns[:, 3] -= 0.3 * returns[:, 2]
    return np.cov(returns, rowvar=False)


# ---------------------------------------------------------------------------
# The allocation contract: long-only and fully invested
# ---------------------------------------------------------------------------
class TestWeightsAreValidAllocations:
    def test_min_variance_sums_to_one(self, three_assets):
        weights = min_variance_weights(three_assets)

        assert weights.sum() == pytest.approx(1.0, abs=TOL)

    def test_min_variance_is_long_only(self, three_assets):
        assert (min_variance_weights(three_assets) >= -TOL).all()

    def test_max_sharpe_sums_to_one(self, three_assets):
        weights = max_sharpe_weights([0.0008, 0.0005, 0.0003], three_assets, rf=0.0002)

        assert weights.sum() == pytest.approx(1.0, abs=TOL)

    def test_max_sharpe_is_long_only(self, three_assets):
        weights = max_sharpe_weights([0.0008, 0.0005, 0.0003], three_assets, rf=0.0002)

        assert (weights >= -TOL).all()

    def test_holds_on_a_larger_realistic_matrix(self, realistic):
        means = np.array([0.0009, 0.0007, 0.0004, 0.0006, 0.0002])

        for weights in (
            min_variance_weights(realistic),
            max_sharpe_weights(means, realistic, rf=0.0002),
        ):
            assert weights.shape == (5,)
            assert weights.sum() == pytest.approx(1.0, abs=TOL)
            assert (weights >= -TOL).all()

    def test_weights_follow_the_covariance_axis_order(self, two_uncorrelated):
        """Asset 0 is the volatile one, so it must receive the smaller weight."""
        weights = min_variance_weights(two_uncorrelated)

        assert weights[0] < weights[1]


# ---------------------------------------------------------------------------
# Diversification: the whole point of the exercise
# ---------------------------------------------------------------------------
class TestMinVarianceBeatsAnySingleAsset:
    def _volatility(self, weights, cov):
        return float(np.sqrt(weights @ cov @ weights))

    def test_no_single_asset_is_calmer(self, three_assets):
        weights = min_variance_weights(three_assets)
        portfolio = self._volatility(weights, three_assets)
        single_asset = np.sqrt(np.diag(three_assets))

        assert portfolio <= single_asset.min() + TOL

    def test_holds_for_the_realistic_matrix(self, realistic):
        weights = min_variance_weights(realistic)
        portfolio = self._volatility(weights, realistic)

        assert portfolio <= np.sqrt(np.diag(realistic)).min() + TOL

    def test_beats_equal_weighting(self, three_assets):
        """If it did not, there would be no reason to run an optimiser."""
        optimal = self._volatility(min_variance_weights(three_assets), three_assets)
        naive = self._volatility(np.full(3, 1 / 3), three_assets)

        assert optimal <= naive + TOL

    def test_negative_correlation_is_exploited(self):
        """
        Two assets with identical vol and correlation -1 hedge each other
        exactly: the 50/50 mix has ZERO variance, and the optimiser must find it.
        """
        cov = cov_from([0.2, 0.2], [[1.0, -1.0], [-1.0, 1.0]])

        weights = min_variance_weights(cov)

        assert weights == pytest.approx([0.5, 0.5], abs=1e-4)
        assert self._volatility(weights, cov) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Closed-form checks
# ---------------------------------------------------------------------------
class TestKnownTwoAssetCases:
    def test_uncorrelated_min_variance_is_inverse_variance_weighted(self, two_uncorrelated):
        """
        With zero correlation the minimum-variance weights are proportional to
        the inverse of each variance:

            w1 = (1/0.04) / (1/0.04 + 1/0.01) = 25 / 125 = 0.2
            w2 = 0.8
        """
        weights = min_variance_weights(two_uncorrelated)

        assert weights == pytest.approx([0.2, 0.8], abs=1e-5)

    def test_correlated_min_variance_matches_the_closed_form(self):
        """
        General two-asset solution:

            w1 = (s2^2 - rho*s1*s2) / (s1^2 + s2^2 - 2*rho*s1*s2)
        """
        s1, s2, rho = 0.30, 0.15, 0.25
        cov = cov_from([s1, s2], [[1.0, rho], [rho, 1.0]])
        expected = (s2**2 - rho * s1 * s2) / (s1**2 + s2**2 - 2 * rho * s1 * s2)

        weights = min_variance_weights(cov)

        assert weights == pytest.approx([expected, 1 - expected], abs=1e-5)

    def test_uncorrelated_tangency_matches_the_closed_form(self, two_uncorrelated):
        """
        With zero correlation the tangency weights are proportional to excess
        return over variance:

            w_i  proportional to  (mu_i - rf) / sigma_i^2
        """
        means, rf = np.array([0.10, 0.06]), 0.02
        variances = np.diag(two_uncorrelated)
        raw = (means - rf) / variances
        expected = raw / raw.sum()

        weights = max_sharpe_weights(means, two_uncorrelated, rf=rf)

        assert weights == pytest.approx(expected, abs=1e-4)

    def test_tangency_has_the_best_sharpe_available(self, three_assets):
        means, rf = np.array([0.0008, 0.0005, 0.0003]), 0.0002

        def sharpe(weights):
            deviation = float(np.sqrt(weights @ three_assets @ weights))
            return float((weights @ means - rf) / deviation)

        best = sharpe(max_sharpe_weights(means, three_assets, rf=rf))

        assert best >= sharpe(min_variance_weights(three_assets)) - TOL
        assert best >= sharpe(np.full(3, 1 / 3)) - TOL
        for asset in range(3):
            single = np.zeros(3)
            single[asset] = 1.0
            assert best >= sharpe(single) - TOL

    def test_tangency_concentrates_on_the_only_asset_worth_holding(self, two_uncorrelated):
        """When one asset's excess return is negative, long-only drops it."""
        weights = max_sharpe_weights([0.10, -0.04], two_uncorrelated, rf=0.02)

        assert weights[0] == pytest.approx(1.0, abs=1e-4)
        assert weights[1] == pytest.approx(0.0, abs=1e-4)


# ---------------------------------------------------------------------------
# The frontier
# ---------------------------------------------------------------------------
class TestEfficientFrontier:
    def test_returns_the_requested_number_of_points(self, three_assets):
        points = efficient_frontier([0.0008, 0.0005, 0.0003], three_assets, n_points=20)

        assert len(points) == 20

    def test_every_point_has_risk_and_return(self, three_assets):
        for point in efficient_frontier([0.0008, 0.0005, 0.0003], three_assets, n_points=8):
            assert set(point) == {"risk", "return"}
            assert np.isfinite(point["risk"]) and np.isfinite(point["return"])
            assert point["risk"] >= 0.0

    def test_is_anchored_at_the_minimum_variance_portfolio(self, three_assets):
        means = [0.0008, 0.0005, 0.0003]
        weights = min_variance_weights(three_assets)
        floor = float(np.sqrt(weights @ three_assets @ weights))

        points = efficient_frontier(means, three_assets, n_points=12)

        assert points[0]["risk"] == pytest.approx(floor, abs=1e-6)
        assert all(point["risk"] >= floor - TOL for point in points)

    def test_is_efficient_more_risk_buys_more_return(self, three_assets):
        """The defining property: sorted by risk, returns must not fall."""
        points = efficient_frontier([0.0008, 0.0005, 0.0003], three_assets, n_points=15)

        risks = [point["risk"] for point in points]
        returns = [point["return"] for point in points]

        assert risks == sorted(risks)
        assert all(b >= a - TOL for a, b in zip(returns, returns[1:]))

    def test_tops_out_at_the_best_single_asset(self, three_assets):
        """Long-only and no leverage: the most return available is the best asset."""
        means = [0.0008, 0.0005, 0.0003]

        points = efficient_frontier(means, three_assets, n_points=10)

        assert points[-1]["return"] == pytest.approx(max(means), abs=1e-6)

    def test_identical_expected_returns_collapse_to_one_point(self, three_assets):
        """Nothing to trade off, so there is no curve to draw."""
        points = efficient_frontier([0.0005, 0.0005, 0.0005], three_assets, n_points=20)

        assert len(points) == 1


# ---------------------------------------------------------------------------
# Degenerate inputs must not crash the endpoint
# ---------------------------------------------------------------------------
class TestDegenerateInputs:
    def test_single_asset_is_fully_invested(self):
        cov = np.array([[0.04]])

        assert min_variance_weights(cov) == pytest.approx([1.0])
        assert max_sharpe_weights([0.01], cov, rf=0.0) == pytest.approx([1.0])
        assert efficient_frontier([0.01], cov, n_points=20) == [
            {"risk": pytest.approx(0.2), "return": pytest.approx(0.01)}
        ]

    def test_identical_assets_split_without_error(self):
        cov = cov_from([0.2, 0.2], [[1.0, 1.0], [1.0, 1.0]])

        weights = min_variance_weights(cov)

        assert weights.sum() == pytest.approx(1.0, abs=TOL)
        assert (weights >= -TOL).all()

    def test_zero_variance_asset_is_preferred(self):
        """A riskless asset is the whole min-variance portfolio."""
        cov = np.array([[0.04, 0.0], [0.0, 0.0]])

        weights = min_variance_weights(cov)

        assert weights[1] == pytest.approx(1.0, abs=1e-5)

    def test_rejects_a_non_square_matrix(self):
        with pytest.raises(ValueError, match="square"):
            min_variance_weights([[0.1, 0.2, 0.3], [0.2, 0.3, 0.4]])

    def test_rejects_mismatched_mean_vector(self, three_assets):
        with pytest.raises(ValueError, match="describes 3 assets"):
            max_sharpe_weights([0.01, 0.02], three_assets)

    def test_rejects_non_finite_input(self):
        with pytest.raises(ValueError, match="NaN"):
            min_variance_weights([[0.04, np.nan], [np.nan, 0.01]])


# ---------------------------------------------------------------------------
# Purity - the same guarantee engine.py carries (architecture rule 2)
# ---------------------------------------------------------------------------
class TestOptimizerPurity:
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
    ALLOWED_ROOTS = {"__future__", "typing", "numpy", "scipy"}

    def _imported_roots(self) -> set[str]:
        source = (BACKEND_DIR / "risk" / "optimizer.py").read_text(encoding="utf-8")
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
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        env["PYTHONPATH"] = str(BACKEND_DIR)
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from risk.optimizer import min_variance_weights as m; "
                "print('django' in sys.modules, "
                "round(float(m([[0.04, 0.0], [0.0, 0.01]])[0]), 3))",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(BACKEND_DIR),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False 0.2"

    def test_every_public_function_documents_its_formula(self):
        """Architecture rule 6, same as the engine."""
        for name in optimizer.__all__:
            attribute = getattr(optimizer, name)
            if not callable(attribute):
                continue
            doc = attribute.__doc__ or ""
            assert "Formula:" in doc, f"{name} has no Formula: section"
            assert "Assumptions:" in doc, f"{name} has no Assumptions: section"
