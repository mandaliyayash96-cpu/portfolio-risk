"""
Tests for compute_rebalance and GET /api/rebalance/<id>/.

The optimiser's maths is covered by test_optimizer.py, so these check the
service layer's job: that the suggestion is built from the SAME inputs as the
risk report, that annualisation is consistent between the two endpoints, and
that every failure is the same clean envelope /api/risk/ produces.
"""

from datetime import date

import pytest
from django.test import override_settings
from django.urls import reverse

from common.exceptions import (
    EmptyPortfolioError,
    InsufficientHistoryError,
    MissingPriceDataError,
    NotFoundError,
)
from risk.services import compute_rebalance, compute_risk

from .conftest import BENCHMARK, make_history, make_snapshot

pytestmark = pytest.mark.django_db

TOL = 1e-9


def rebalance_url(portfolio_id: int) -> str:
    return reverse("risk:rebalance", args=[portfolio_id])


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
class TestPayloadShape:
    def test_has_every_documented_key(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)

        assert {
            "portfolio",
            "tickers",
            "observations",
            "start",
            "end",
            "current",
            "min_variance",
            "max_sharpe",
            "efficient_frontier",
            "params",
            "warnings",
        } <= set(result)

    def test_each_allocation_is_fully_described(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)

        for key in ("current", "min_variance", "max_sharpe"):
            allocation = result[key]
            assert set(allocation) == {"weights", "volatility", "expected_return", "sharpe"}
            assert set(allocation["weights"]) == {"RELIANCE.NS", "TCS.NS"}
            assert sum(allocation["weights"].values()) == pytest.approx(1.0, abs=1e-6)
            assert all(weight >= -1e-9 for weight in allocation["weights"].values())

    def test_is_json_safe(self, funded_portfolio):
        import json

        payload = json.dumps(compute_rebalance(funded_portfolio.pk), allow_nan=False)

        assert "NaN" not in payload and "Infinity" not in payload

    def test_frontier_points_are_risk_return_pairs(self, funded_portfolio):
        frontier = compute_rebalance(funded_portfolio.pk)["efficient_frontier"]

        assert len(frontier) > 1
        for point in frontier:
            assert set(point) == {"risk", "return"}
        risks = [point["risk"] for point in frontier]
        assert risks == sorted(risks)

    def test_suggests_no_new_tickers(self, funded_portfolio):
        """A rebalance the investor cannot execute is not a suggestion."""
        result = compute_rebalance(funded_portfolio.pk)

        assert set(result["min_variance"]["weights"]) == set(result["current"]["weights"])
        assert set(result["max_sharpe"]["weights"]) == set(result["current"]["weights"])


# ---------------------------------------------------------------------------
# The point of the whole phase: consistency with /api/risk/
# ---------------------------------------------------------------------------
class TestAgreesWithTheRiskReport:
    def test_current_volatility_matches_the_report_exactly(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)
        rebalance = compute_rebalance(funded_portfolio.pk)

        assert rebalance["current"]["volatility"] == pytest.approx(
            report["annualized_volatility"], abs=TOL
        )

    def test_current_sharpe_matches_the_report_exactly(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)
        rebalance = compute_rebalance(funded_portfolio.pk)

        assert rebalance["current"]["sharpe"] == pytest.approx(report["sharpe"], abs=TOL)

    def test_current_weights_match_the_report(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)
        rebalance = compute_rebalance(funded_portfolio.pk)

        assert rebalance["current"]["weights"] == report["weights"]

    def test_same_window(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)
        rebalance = compute_rebalance(funded_portfolio.pk)

        assert rebalance["observations"] == report["observations"]
        assert rebalance["start"] == report["start"]
        assert rebalance["end"] == report["end"]
        assert rebalance["tickers"] == report["tickers"]

    def test_partial_overlap_shortens_both_endpoints_identically(
        self, portfolio, holding_factory
    ):
        """The shared join must apply to the suggestion too, not just the report."""
        holding_factory("LONG.NS", "10")
        holding_factory("LATE.NS", "10")
        make_history("LONG.NS", days=90, start=date(2026, 1, 5), seed=1)
        make_history("LATE.NS", days=40, start=date(2026, 3, 30), seed=2)
        make_history(BENCHMARK, days=90, start=date(2026, 1, 5), seed=3)
        make_snapshot("LONG.NS", "100.0000")
        make_snapshot("LATE.NS", "100.0000")

        report = compute_risk(portfolio.pk)
        rebalance = compute_rebalance(portfolio.pk)

        assert rebalance["observations"] == report["observations"]
        assert rebalance["current"]["volatility"] == pytest.approx(
            report["annualized_volatility"], abs=TOL
        )


# ---------------------------------------------------------------------------
# The suggestion has to be worth making
# ---------------------------------------------------------------------------
class TestSuggestionQuality:
    def test_min_variance_is_no_riskier_than_the_current_weights(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)

        assert result["min_variance"]["volatility"] <= result["current"]["volatility"] + 1e-9

    def test_min_variance_is_the_calmest_point_on_the_frontier(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)
        floor = result["efficient_frontier"][0]["risk"]

        assert result["min_variance"]["volatility"] == pytest.approx(floor, abs=1e-6)

    def test_max_sharpe_beats_current_and_min_variance_on_sharpe(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)

        assert result["max_sharpe"]["sharpe"] >= result["current"]["sharpe"] - 1e-6
        assert result["max_sharpe"]["sharpe"] >= result["min_variance"]["sharpe"] - 1e-6

    def test_annualisation_is_consistent_across_endpoints(self, funded_portfolio):
        """
        Volatility is annualised by sqrt(252) in both places, so the frontier's
        risk axis and the report's headline number live in the same units.
        """
        report = compute_risk(funded_portfolio.pk)
        result = compute_rebalance(funded_portfolio.pk)
        frontier_risks = [point["risk"] for point in result["efficient_frontier"]]

        assert min(frontier_risks) <= report["annualized_volatility"] + 1e-9
        # Annualised daily vol lands in a sane band; a missing sqrt(252) would
        # put it near 1% and a doubled one near 300%.
        assert 0.01 < report["annualized_volatility"] < 3.0

    @override_settings(TRADING_DAYS_PER_YEAR=250, RISK_FREE_RATE=0.10)
    def test_settings_reach_the_optimizer(self, funded_portfolio):
        result = compute_rebalance(funded_portfolio.pk)

        assert result["params"]["trading_days"] == 250
        assert result["params"]["rf_annual"] == pytest.approx(0.10)
        assert result["params"]["rf_per_period"] == pytest.approx(0.10 / 250)


# ---------------------------------------------------------------------------
# Degenerate portfolios
# ---------------------------------------------------------------------------
class TestDegeneratePortfolios:
    def test_single_holding_is_answered_not_crashed(self, portfolio, holding_factory):
        holding_factory("RELIANCE.NS", "100")
        make_history("RELIANCE.NS", seed=1)
        make_history(BENCHMARK, seed=3, base=22000.0)
        make_snapshot("RELIANCE.NS", "1000.0000")

        result = compute_rebalance(portfolio.pk)

        assert result["min_variance"]["weights"] == {"RELIANCE.NS": pytest.approx(1.0)}
        assert len(result["efficient_frontier"]) == 1
        assert any("single ticker" in warning for warning in result["warnings"])

    def test_benchmark_warning_is_carried_through(self, portfolio, holding_factory):
        """The rebalance page reports the same degradations as the risk page."""
        holding_factory("RELIANCE.NS", "100")
        holding_factory("TCS.NS", "50")
        make_history("RELIANCE.NS", seed=1)
        make_history("TCS.NS", seed=2, base=200.0)
        make_snapshot("RELIANCE.NS", "1000.0000")
        make_snapshot("TCS.NS", "2000.0000")

        result = compute_rebalance(portfolio.pk)

        assert any("benchmark" in warning for warning in result["warnings"])

    def test_three_holdings_produce_a_full_frontier(self, portfolio, holding_factory):
        for index, ticker in enumerate(("A.NS", "B.NS", "C.NS")):
            holding_factory(ticker, "10")
            make_history(ticker, seed=index + 1, base=100.0 * (index + 1))
            make_snapshot(ticker, f"{100 * (index + 1)}.0000")
        make_history(BENCHMARK, seed=9, base=22000.0)

        result = compute_rebalance(portfolio.pk)

        assert len(result["tickers"]) == 3
        assert len(result["efficient_frontier"]) > 5


# ---------------------------------------------------------------------------
# Failures - identical to the risk endpoint's
# ---------------------------------------------------------------------------
class TestFailureModes:
    def test_unknown_portfolio(self, db):
        with pytest.raises(NotFoundError):
            compute_rebalance(9999)

    def test_empty_portfolio(self, portfolio):
        with pytest.raises(EmptyPortfolioError):
            compute_rebalance(portfolio.pk)

    def test_unfetched_ticker(self, portfolio, holding_factory):
        holding_factory("NEVER.NS", "10")

        with pytest.raises(MissingPriceDataError):
            compute_rebalance(portfolio.pk)

    def test_short_history(self, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5, seed=1)
        make_snapshot("SHORT.NS", "100.0000")

        with pytest.raises(InsufficientHistoryError):
            compute_rebalance(portfolio.pk)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class TestEndpoint:
    def test_url_is_the_documented_path(self):
        assert rebalance_url(1) == "/api/rebalance/1/"

    def test_risk_url_is_unchanged(self):
        """Phase 5 re-mounted the app's urls; the Phase 4 path must not move."""
        assert reverse("risk:report", args=[1]) == "/api/risk/1/"

    def test_returns_the_success_envelope(self, client, funded_portfolio):
        response = client.get(rebalance_url(funded_portfolio.pk))
        body = response.json()

        assert response.status_code == 200
        assert set(body) == {"success", "data", "error"}
        assert body["success"] is True
        assert body["data"]["tickers"] == ["RELIANCE.NS", "TCS.NS"]
        assert body["data"]["min_variance"]["volatility"] > 0

    def test_is_read_only(self, client, funded_portfolio):
        assert client.post(rebalance_url(funded_portfolio.pk)).status_code == 405

    def test_unknown_portfolio_is_a_404_envelope(self, client, db):
        response = client.get(rebalance_url(9999))
        body = response.json()

        assert response.status_code == 404
        assert body["success"] is False
        assert body["error"]["code"] == "not_found"

    def test_empty_portfolio_is_a_400_envelope(self, client, portfolio):
        response = client.get(rebalance_url(portfolio.pk))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_portfolio"

    def test_missing_prices_is_a_422_envelope(self, client, portfolio, holding_factory):
        holding_factory("NEVER.NS", "10")

        response = client.get(rebalance_url(portfolio.pk))
        body = response.json()

        assert response.status_code == 422
        assert body["error"]["code"] == "missing_price_data"
        assert "fetch_prices" in body["error"]["message"]

    def test_short_history_is_a_422_envelope(self, client, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5, seed=1)
        make_snapshot("SHORT.NS", "100.0000")

        response = client.get(rebalance_url(portfolio.pk))

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_history"
