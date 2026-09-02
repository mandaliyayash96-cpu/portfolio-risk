"""
Tests for the performance curve: risk/services.py:compute_performance and the
/api/performance/<portfolio_id>/ endpoint.

The maths is already covered by test_engine.py, so what matters here is what
only the service and the view can get wrong: that the curve is built from the
SAME prepared inputs as the risk report - and therefore quotes the same max
drawdown - that the three parallel lists really are parallel, that dates arrive
as plain YYYY-MM-DD, that nothing non-JSON escapes, and that all four failure
modes come back in the envelope rather than as a 500.
"""

import json
from datetime import date

import pytest
from django.urls import reverse

from common.exceptions import (
    EmptyPortfolioError,
    InsufficientHistoryError,
    MissingPriceDataError,
    NotFoundError,
)
from risk.services import REBASE_VALUE, compute_performance, compute_risk

from .conftest import make_history, make_snapshot

pytestmark = pytest.mark.django_db


def performance_url(portfolio_id: int) -> str:
    return reverse("risk:performance", args=[portfolio_id])


def test_url_is_the_documented_path():
    assert performance_url(1) == "/api/performance/1/"


def _reject(constant):  # pragma: no cover - only called if the JSON is invalid
    raise AssertionError(f"non-JSON constant: {constant}")


# ---------------------------------------------------------------------------
# Service - shape
# ---------------------------------------------------------------------------
class TestPerformanceShape:
    def test_returns_every_documented_key(self, funded_portfolio):
        result = compute_performance(funded_portfolio.pk)

        assert set(result) == {
            "portfolio",
            "dates",
            "equity_curve",
            "drawdown_series",
            "peak_value",
            "current_value",
            "max_drawdown",
            "start_value",
            "observations",
            "start",
            "end",
            "excluded",
            "warnings",
        }

    def test_the_three_series_are_parallel(self, funded_portfolio):
        """The frontend zips these by index; unequal lengths would misdate
        every point after the first divergence."""
        result = compute_performance(funded_portfolio.pk)

        assert len(result["dates"]) == len(result["equity_curve"])
        assert len(result["dates"]) == len(result["drawdown_series"])
        assert result["observations"] == len(result["dates"])

    def test_length_matches_the_risk_report_window(self, funded_portfolio):
        """Same _prepare, so the same number of observations - not one more or
        one fewer than the report describes."""
        performance = compute_performance(funded_portfolio.pk)
        report = compute_risk(funded_portfolio.pk)

        assert performance["observations"] == report["observations"]

    def test_dates_are_plain_iso_days(self, funded_portfolio):
        result = compute_performance(funded_portfolio.pk)

        for value in result["dates"]:
            assert date.fromisoformat(value)  # raises on a time component
        assert result["start"] == result["dates"][0]
        assert result["end"] == result["dates"][-1]

    def test_dates_ascend(self, funded_portfolio):
        dates = compute_performance(funded_portfolio.pk)["dates"]

        assert dates == sorted(dates)

    def test_is_json_safe(self, funded_portfolio):
        """NaN and Infinity parse in Python but break every other client."""
        result = compute_performance(funded_portfolio.pk)

        assert json.loads(json.dumps(result), parse_constant=_reject)


# ---------------------------------------------------------------------------
# Service - the curve itself
# ---------------------------------------------------------------------------
class TestTheCurve:
    def test_starts_near_the_rebase_value(self, funded_portfolio):
        """
        Near, not exactly at: the first point is already one return in - see
        engine.portfolio_equity_curve for why that is deliberate.
        """
        result = compute_performance(funded_portfolio.pk)

        assert result["start_value"] == REBASE_VALUE
        assert result["equity_curve"][0] == pytest.approx(REBASE_VALUE, rel=0.1)

    def test_peak_is_the_highest_point_and_current_is_the_last(self, funded_portfolio):
        result = compute_performance(funded_portfolio.pk)
        curve = result["equity_curve"]

        assert result["peak_value"] == max(curve)
        assert result["current_value"] == curve[-1]

    def test_drawdown_is_never_positive(self, funded_portfolio):
        result = compute_performance(funded_portfolio.pk)

        assert all(value <= 0.0 for value in result["drawdown_series"])

    def test_drawdown_is_zero_at_the_peak_date(self, funded_portfolio):
        """The peak defines the zero line, so its own date must sit on it."""
        result = compute_performance(funded_portfolio.pk)
        peak_index = result["equity_curve"].index(result["peak_value"])

        assert result["drawdown_series"][peak_index] == pytest.approx(0.0)

    def test_drawdown_series_is_percent_and_max_drawdown_is_a_fraction(
        self, funded_portfolio
    ):
        """
        The two units this response mixes on purpose - the chart axis wants
        percentage points, the reference figure matches the risk report's
        fraction. Pinned so a later tidy-up cannot quietly unify them.
        """
        result = compute_performance(funded_portfolio.pk)

        assert min(result["drawdown_series"]) == pytest.approx(
            result["max_drawdown"] * 100.0
        )
        assert -1.0 <= result["max_drawdown"] <= 0.0

    def test_max_drawdown_matches_the_risk_report_exactly(self, funded_portfolio):
        """
        The whole reason compute_performance reuses _prepare. The underwater
        chart and the max-drawdown card are one number, so they can never be
        seen disagreeing on the same screen.
        """
        performance = compute_performance(funded_portfolio.pk)
        report = compute_risk(funded_portfolio.pk)

        assert performance["max_drawdown"] == report["max_drawdown"]

    def test_rising_portfolio_never_goes_underwater(self, portfolio, holding_factory):
        """A holding that only ever rises has no peak to fall from."""
        holding_factory("UPONLY.NS", "10")
        make_history("UPONLY.NS", days=40, base=100.0, drift=0.01, vol=0.0)
        make_snapshot("UPONLY.NS", "150.0000")

        result = compute_performance(portfolio.pk)
        curve = result["equity_curve"]

        assert all(value == pytest.approx(0.0) for value in result["drawdown_series"])
        assert result["max_drawdown"] == pytest.approx(0.0)
        assert all(later > earlier for earlier, later in zip(curve, curve[1:]))

    def test_start_value_is_configurable(self, funded_portfolio):
        """Rebasing is presentation; it must not move the drawdown."""
        hundred = compute_performance(funded_portfolio.pk)
        thousand = compute_performance(funded_portfolio.pk, start_value=1000.0)

        assert thousand["equity_curve"][0] == pytest.approx(
            hundred["equity_curve"][0] * 10.0
        )
        assert thousand["max_drawdown"] == pytest.approx(hundred["max_drawdown"])


class TestProvenance:
    def test_names_the_portfolio(self, funded_portfolio):
        block = compute_performance(funded_portfolio.pk)["portfolio"]

        assert block == {
            "id": funded_portfolio.pk,
            "name": funded_portfolio.name,
            "base_currency": funded_portfolio.base_currency,
        }

    def test_missing_benchmark_warning_is_carried_through(
        self, portfolio, holding_factory
    ):
        """Same _prepare, so the same degradations surface on this endpoint."""
        holding_factory("RELIANCE.NS", "100")
        make_history("RELIANCE.NS", seed=1)
        make_snapshot("RELIANCE.NS", "1000.0000")  # no ^NSEI history at all

        result = compute_performance(portfolio.pk)

        assert any("fetch_prices" in warning for warning in result["warnings"])


# ---------------------------------------------------------------------------
# Failure modes - all four originate in the shared _prepare
# ---------------------------------------------------------------------------
class TestFailureModes:
    def test_unknown_portfolio(self, db):
        with pytest.raises(NotFoundError):
            compute_performance(9999)

    def test_portfolio_without_holdings(self, portfolio):
        with pytest.raises(EmptyPortfolioError):
            compute_performance(portfolio.pk)

    def test_a_wholly_unpriceable_portfolio_is_empty_not_missing_prices(
        self, portfolio, holding_factory
    ):
        """
        One holding, no prices for it: nothing survives, so there is no curve.

        This used to raise MissingPriceDataError. It now raises the empty-
        portfolio error, because "every holding was excluded" and "there are no
        holdings" leave the endpoint in the same place - with no exposure to
        plot. The ticker is named in `details` so the two can still be told
        apart by a caller that cares.
        """
        holding_factory("NOPRICE.NS", "10")

        with pytest.raises(EmptyPortfolioError) as caught:
            compute_performance(portfolio.pk)

        assert caught.value.details == {"tickers": ["NOPRICE.NS"]}

    def test_too_few_overlapping_observations(self, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5)
        make_snapshot("SHORT.NS", "100.0000")

        with pytest.raises(InsufficientHistoryError):
            compute_performance(portfolio.pk)


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------
class TestEndpoint:
    def test_returns_the_curve_in_the_success_envelope(self, client, funded_portfolio):
        response = client.get(performance_url(funded_portfolio.pk))
        body = response.json()

        assert response.status_code == 200
        assert set(body) == {"success", "data", "error"}
        assert body["success"] is True
        assert body["error"] is None
        assert body["data"]["portfolio"]["id"] == funded_portfolio.pk
        assert len(body["data"]["dates"]) == len(body["data"]["equity_curve"])

    def test_response_is_valid_strict_json(self, client, funded_portfolio):
        response = client.get(performance_url(funded_portfolio.pk))

        assert json.loads(response.content.decode(), parse_constant=_reject)

    def test_endpoint_is_read_only(self, client, funded_portfolio):
        assert client.post(performance_url(funded_portfolio.pk)).status_code == 405

    def test_open_without_authentication(self, client, funded_portfolio):
        """AllowAny for now - the Phase 4 auth TODO in views.py still stands."""
        assert client.get(performance_url(funded_portfolio.pk)).status_code == 200

    def test_non_numeric_id_does_not_match_the_route(self, client, db):
        assert client.get("/api/performance/abc/").status_code == 404


class TestFailureEnvelopes:
    """The same four codes and statuses /api/risk/ returns - never a 500."""

    def _error(self, client, portfolio_id: int, expected_status: int) -> dict:
        response = client.get(performance_url(portfolio_id))

        assert response.status_code == expected_status
        body = response.json()
        assert body["success"] is False
        assert body["data"] is None
        return body["error"]

    def test_unknown_portfolio_is_404_not_found(self, client, db):
        assert self._error(client, 9999, 404)["code"] == "not_found"

    def test_empty_portfolio_is_400(self, client, portfolio):
        assert self._error(client, portfolio.pk, 400)["code"] == "empty_portfolio"

    def test_a_wholly_unpriceable_portfolio_is_a_400_naming_the_ticker(
        self, client, portfolio, holding_factory
    ):
        holding_factory("NOPRICE.NS", "10")
        error = self._error(client, portfolio.pk, 400)

        assert error["code"] == "empty_portfolio"
        assert "NOPRICE.NS" in error["message"]
        assert "fetch_prices" in error["message"]

    def test_short_history_is_422_insufficient(self, client, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5)
        make_snapshot("SHORT.NS", "100.0000")

        assert self._error(client, portfolio.pk, 422)["code"] == "insufficient_history"
