"""
Tests for risk/services.py - the ORM/settings layer around the pure engine.

The engine's maths is already covered by test_engine.py, so these tests check
the things only the service can get wrong: value-based weights, the Decimal
boundary, benchmark separation, and every failure mode arriving as a
DomainError instead of a 500.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.test import override_settings

from common.exceptions import (
    EmptyPortfolioError,
    InsufficientHistoryError,
    InvalidInputError,
    MissingPriceDataError,
    NotFoundError,
)
from marketdata.models import PriceHistory
from risk.services import MIN_OBSERVATIONS, compute_risk

from .conftest import BENCHMARK, make_history, make_snapshot

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestReportShape:
    def test_returns_every_engine_key_plus_provenance(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)

        expected = {
            "observations", "start", "end", "tickers", "weights",
            "annualized_return", "annualized_volatility",
            "beta", "sharpe", "sortino", "max_drawdown",
            "var_historical", "var_parametric", "var_montecarlo", "cvar",
            "hhi", "effective_holdings",
            "per_asset_volatility", "correlation_matrix", "covariance_matrix",
            "params",
            # added by the service, not the engine
            "portfolio", "benchmark", "warnings",
        }
        assert expected <= set(report)

    def test_report_is_json_safe(self, funded_portfolio):
        """No NaN/inf anywhere - strict JSON has no way to represent them."""
        import json

        payload = json.dumps(compute_risk(funded_portfolio.pk), allow_nan=False)
        assert "NaN" not in payload and "Infinity" not in payload

    def test_holding_columns_exclude_the_benchmark(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)

        assert report["tickers"] == ["RELIANCE.NS", "TCS.NS"]
        assert BENCHMARK not in report["weights"]
        assert BENCHMARK not in report["correlation_matrix"]

    def test_observations_are_one_short_of_the_aligned_days(self, funded_portfolio):
        """90 stored closes, all overlapping -> 89 returns (the first differences away)."""
        assert compute_risk(funded_portfolio.pk)["observations"] == 89


# ---------------------------------------------------------------------------
# Weights: value-based, Decimal until the ratio
# ---------------------------------------------------------------------------
class TestWeights:
    def test_weights_are_value_based_not_equal(self, funded_portfolio):
        """100 x 1000 and 50 x 2000 are both 100,000 -> 50/50."""
        report = compute_risk(funded_portfolio.pk)

        assert report["weights"]["RELIANCE.NS"] == pytest.approx(0.5)
        assert report["weights"]["TCS.NS"] == pytest.approx(0.5)
        assert report["portfolio"]["market_value"].startswith("200000")

    def test_weights_track_quantity_and_price(self, portfolio, holding_factory):
        """Change one quantity and the weights follow the market value."""
        holding_factory("RELIANCE.NS", "300")   # 300 x 1000 = 300,000  -> 0.75
        holding_factory("TCS.NS", "50")         #  50 x 2000 = 100,000  -> 0.25
        make_history("RELIANCE.NS", seed=1)
        make_history("TCS.NS", seed=2, base=200.0)
        make_snapshot("RELIANCE.NS", "1000.0000")
        make_snapshot("TCS.NS", "2000.0000")

        weights = compute_risk(portfolio.pk)["weights"]

        assert weights["RELIANCE.NS"] == pytest.approx(0.75)
        assert weights["TCS.NS"] == pytest.approx(0.25)

    def test_weights_sum_to_one(self, portfolio, holding_factory):
        """Three thirds must still sum to 1.0 after the float() crossing."""
        for index, ticker in enumerate(("A.NS", "B.NS", "C.NS")):
            holding_factory(ticker, "10")
            make_history(ticker, seed=index + 1)
            make_snapshot(ticker, "333.3333")

        report = compute_risk(portfolio.pk)

        assert sum(report["weights"].values()) == pytest.approx(1.0, abs=1e-12)

    def test_falls_back_to_last_close_when_no_snapshot(self, portfolio, holding_factory):
        """`fetch_prices --skip-live` leaves history but no PriceSnapshot."""
        holding_factory("RELIANCE.NS", "100")
        holding_factory("TCS.NS", "100")
        make_history("RELIANCE.NS", seed=1)
        make_history("TCS.NS", seed=2, base=200.0)
        make_snapshot("RELIANCE.NS", "1000.0000")  # TCS has no live price

        holdings = {h["ticker"]: h for h in compute_risk(portfolio.pk)["portfolio"]["holdings"]}

        assert holdings["RELIANCE.NS"]["price_source"] == "live"
        assert holdings["TCS.NS"]["price_source"] == "last_close"
        latest_close = (
            PriceHistory.objects.filter(ticker="TCS.NS").order_by("-date").first().close
        )
        assert holdings["TCS.NS"]["price"] == str(latest_close)

    def test_money_is_serialised_as_string(self, funded_portfolio):
        block = compute_risk(funded_portfolio.pk)["portfolio"]

        assert isinstance(block["market_value"], str)
        for holding in block["holdings"]:
            assert isinstance(holding["price"], str)
            assert isinstance(holding["market_value"], str)
            assert isinstance(holding["weight"], float)  # a ratio, not money

    def test_zero_valued_portfolio_is_rejected(self, portfolio, holding_factory):
        holding_factory("RELIANCE.NS", "0")
        make_history("RELIANCE.NS", seed=1)
        make_snapshot("RELIANCE.NS", "1000.0000")

        with pytest.raises(InvalidInputError):
            compute_risk(portfolio.pk)


# ---------------------------------------------------------------------------
# Benchmark handling
# ---------------------------------------------------------------------------
class TestBenchmark:
    def test_beta_computed_against_the_configured_benchmark(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)

        assert report["benchmark"] == {"ticker": BENCHMARK, "included": True}
        assert isinstance(report["beta"], float)
        assert report["warnings"] == []

    def test_beta_is_one_when_the_portfolio_is_the_benchmark(self, portfolio, holding_factory):
        """A single holding whose prices ARE the benchmark's has beta 1."""
        holding_factory("CLONE.NS", "10")
        dates = make_history(BENCHMARK, seed=5, base=22000.0)
        PriceHistory.objects.bulk_create(
            [
                PriceHistory(ticker="CLONE.NS", date=row.date, close=row.close)
                for row in PriceHistory.objects.filter(ticker=BENCHMARK, date__in=dates)
            ]
        )
        make_snapshot("CLONE.NS", "22000.0000")

        assert compute_risk(portfolio.pk)["beta"] == pytest.approx(1.0)

    def test_missing_benchmark_degrades_to_null_beta_with_a_warning(
        self, portfolio, holding_factory
    ):
        """Every other metric is still valid, so this must not fail the request."""
        holding_factory("RELIANCE.NS", "100")
        make_history("RELIANCE.NS", seed=1)
        make_snapshot("RELIANCE.NS", "1000.0000")  # no ^NSEI history at all

        report = compute_risk(portfolio.pk)

        assert report["beta"] is None
        assert report["benchmark"]["included"] is False
        assert any("fetch_prices" in warning for warning in report["warnings"])
        assert report["annualized_volatility"] > 0  # the rest still computed

    @override_settings(DEFAULT_BENCHMARK_TICKER="")
    def test_blank_benchmark_setting_is_tolerated(self, portfolio, holding_factory):
        holding_factory("RELIANCE.NS", "100")
        make_history("RELIANCE.NS", seed=1)
        make_snapshot("RELIANCE.NS", "1000.0000")

        report = compute_risk(portfolio.pk)

        assert report["benchmark"] == {"ticker": None, "included": False}
        assert report["beta"] is None


# ---------------------------------------------------------------------------
# Settings are read here and only here
# ---------------------------------------------------------------------------
class TestSettingsPassthrough:
    def test_rf_is_converted_to_a_per_period_rate(self, funded_portfolio):
        report = compute_risk(funded_portfolio.pk)

        assert report["params"]["rf_per_period"] == pytest.approx(0.065 / 252)
        assert report["params"]["trading_days"] == 252

    @override_settings(RISK_FREE_RATE=0.10, TRADING_DAYS_PER_YEAR=250)
    def test_engine_receives_the_configured_values(self, funded_portfolio):
        params = compute_risk(funded_portfolio.pk)["params"]

        assert params["rf_per_period"] == pytest.approx(0.10 / 250)
        assert params["trading_days"] == 250

    def test_confidence_is_a_service_argument(self, funded_portfolio):
        assert compute_risk(funded_portfolio.pk, conf=0.99)["params"]["confidence"] == 0.99


# ---------------------------------------------------------------------------
# Failure modes - clean DomainErrors, never a 500
# ---------------------------------------------------------------------------
class TestFailureModes:
    def test_unknown_portfolio(self, db):
        with pytest.raises(NotFoundError) as caught:
            compute_risk(9999)

        assert caught.value.code == "not_found"
        assert caught.value.status_code == 404

    def test_portfolio_without_holdings(self, portfolio):
        with pytest.raises(EmptyPortfolioError) as caught:
            compute_risk(portfolio.pk)

        assert caught.value.code == "empty_portfolio"
        assert caught.value.status_code == 400
        assert "no holdings" in caught.value.message

    def test_ticker_never_fetched(self, portfolio, holding_factory):
        holding_factory("NEVER.NS", "10")

        with pytest.raises(MissingPriceDataError) as caught:
            compute_risk(portfolio.pk)

        assert caught.value.code == "missing_price_data"
        assert caught.value.status_code == 422
        assert "fetch_prices" in caught.value.message
        assert caught.value.details == {"tickers": ["NEVER.NS"]}

    def test_ticker_with_a_snapshot_but_no_history(self, portfolio, holding_factory):
        """A live poll ran but the history leg never did."""
        holding_factory("RELIANCE.NS", "100")
        make_snapshot("RELIANCE.NS", "1000.0000")

        with pytest.raises(MissingPriceDataError) as caught:
            compute_risk(portfolio.pk)

        assert "fetch_prices" in caught.value.message
        assert caught.value.details == {"tickers": ["RELIANCE.NS"]}

    def test_no_overlapping_dates(self, portfolio, holding_factory):
        """Two histories from disjoint windows cannot be inner-joined."""
        holding_factory("OLD.NS", "10")
        holding_factory("NEW.NS", "10")
        make_history("OLD.NS", days=40, start=date(2025, 1, 6), seed=1)
        make_history("NEW.NS", days=40, start=date(2026, 6, 1), seed=2)
        make_snapshot("OLD.NS", "100.0000")
        make_snapshot("NEW.NS", "100.0000")

        with pytest.raises(InsufficientHistoryError) as caught:
            compute_risk(portfolio.pk)

        assert caught.value.code == "insufficient_history"
        assert caught.value.status_code == 422
        assert "no overlapping trading dates" in caught.value.message

    def test_too_few_overlapping_observations(self, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5, seed=1)
        make_snapshot("SHORT.NS", "100.0000")

        with pytest.raises(InsufficientHistoryError) as caught:
            compute_risk(portfolio.pk)

        assert caught.value.details["observations"] == 4
        assert caught.value.details["required"] == MIN_OBSERVATIONS
        assert "fetch_prices" in caught.value.message

    def test_partial_overlap_is_used_not_rejected(self, portfolio, holding_factory):
        """
        A ticker listed midway through the window shortens the aligned matrix
        rather than failing it - as long as enough dates survive the join.
        """
        holding_factory("LONG.NS", "10")
        holding_factory("LATE.NS", "10")
        make_history("LONG.NS", days=90, start=date(2026, 1, 5), seed=1)
        make_history("LATE.NS", days=40, start=date(2026, 3, 30), seed=2)
        make_history(BENCHMARK, days=90, start=date(2026, 1, 5), seed=3)
        make_snapshot("LONG.NS", "100.0000")
        make_snapshot("LATE.NS", "100.0000")

        report = compute_risk(portfolio.pk)

        assert 0 < report["observations"] < 89
        assert report["start"] >= "2026-03-30"


# ---------------------------------------------------------------------------
# Ticker hygiene
# ---------------------------------------------------------------------------
class TestTickerNormalisation:
    def test_lowercase_holding_matches_uppercase_price_rows(self, portfolio, holding_factory):
        """marketdata stores upper-case; a holding typed in lower-case must still resolve."""
        holding_factory("reliance.ns", "100")
        make_history("RELIANCE.NS", seed=1)
        make_snapshot("RELIANCE.NS", "1000.0000")

        report = compute_risk(portfolio.pk)

        assert report["tickers"] == ["RELIANCE.NS"]
        assert report["portfolio"]["holdings"][0]["market_value"] == str(
            Decimal("100.000000") * Decimal("1000.0000")
        )
