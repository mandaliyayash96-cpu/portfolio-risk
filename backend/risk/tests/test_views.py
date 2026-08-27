"""
Tests for the /api/risk/<portfolio_id>/ endpoint.

What matters here is the HTTP contract, not the maths: every response - success
or failure - is the {success, data, error} envelope with the right status code,
and no failure mode reaches the client as a 500.
"""

import pytest
from django.urls import reverse

from .conftest import BENCHMARK, make_history, make_snapshot

pytestmark = pytest.mark.django_db


def risk_url(portfolio_id: int) -> str:
    return reverse("risk:report", args=[portfolio_id])


def test_url_is_the_documented_path():
    assert risk_url(1) == "/api/risk/1/"


class TestSuccess:
    def test_returns_the_report_in_the_success_envelope(self, client, funded_portfolio):
        response = client.get(risk_url(funded_portfolio.pk))
        body = response.json()

        assert response.status_code == 200
        assert set(body) == {"success", "data", "error"}
        assert body["success"] is True
        assert body["error"] is None
        assert body["data"]["tickers"] == ["RELIANCE.NS", "TCS.NS"]
        assert body["data"]["portfolio"]["id"] == funded_portfolio.pk
        assert body["data"]["benchmark"]["ticker"] == BENCHMARK

    def test_response_is_valid_strict_json(self, client, funded_portfolio):
        """NaN/Infinity would parse in Python but break every other client."""
        import json

        response = client.get(risk_url(funded_portfolio.pk))

        assert json.loads(response.content.decode(), parse_constant=_reject)

    def test_endpoint_is_read_only(self, client, funded_portfolio):
        assert client.post(risk_url(funded_portfolio.pk)).status_code == 405

    def test_open_without_authentication(self, client, funded_portfolio):
        """AllowAny for now - Phase 4 auth TODO in views.py still stands."""
        assert client.get(risk_url(funded_portfolio.pk)).status_code == 200


def _reject(constant):  # pragma: no cover - only called if the JSON is invalid
    raise AssertionError(f"non-JSON constant in response: {constant}")


class TestFailureEnvelopes:
    def _error(self, client, portfolio_id: int, expected_status: int) -> dict:
        response = client.get(risk_url(portfolio_id))

        assert response.status_code == expected_status
        body = response.json()
        assert body["success"] is False
        assert body["data"] is None
        return body["error"]

    def test_unknown_portfolio_is_404_not_found(self, client, db):
        error = self._error(client, 9999, 404)

        assert error["code"] == "not_found"
        assert "9999" in error["message"]

    def test_empty_portfolio_is_400(self, client, portfolio):
        error = self._error(client, portfolio.pk, 400)

        assert error["code"] == "empty_portfolio"
        assert "no holdings" in error["message"]

    def test_unfetched_ticker_is_422_naming_fetch_prices(
        self, client, portfolio, holding_factory
    ):
        holding_factory("NEVER.NS", "10")

        error = self._error(client, portfolio.pk, 422)

        assert error["code"] == "missing_price_data"
        assert "fetch_prices" in error["message"]
        assert error["details"] == {"tickers": ["NEVER.NS"]}

    def test_short_history_is_422_insufficient(self, client, portfolio, holding_factory):
        holding_factory("SHORT.NS", "10")
        make_history("SHORT.NS", days=5, seed=1)
        make_snapshot("SHORT.NS", "100.0000")

        error = self._error(client, portfolio.pk, 422)

        assert error["code"] == "insufficient_history"
        assert error["details"]["observations"] == 4

    def test_non_numeric_id_does_not_match_the_route(self, client, db):
        """<int:portfolio_id> means a bad id is a 404 from the URL resolver."""
        assert client.get("/api/risk/abc/").status_code == 404
