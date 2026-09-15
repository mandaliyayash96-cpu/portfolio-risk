"""
POST /api/insights/<id>/ end to end through DRF, with Gemini mocked.

What is under test here is the wire contract: who may call it, whose portfolio
it reads, what the envelope looks like on success and on a model failure, and
that the key never appears in a response.
"""

import pytest

from insights import gemini
from insights.services import DISCLAIMER, UNAVAILABLE_MESSAGE

from .conftest import SAMPLE_INSIGHTS

pytestmark = pytest.mark.django_db


def url(portfolio_id) -> str:
    return f"/api/insights/{portfolio_id}/"


class TestAiInsightsEndpoint:
    def test_returns_insights_in_the_envelope(self, api, my_funded_portfolio, fake_gemini):
        with fake_gemini():
            response = api.post(url(my_funded_portfolio.pk))

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True and body["error"] is None
        data = body["data"]
        assert data["available"] is True
        assert data["insights"] == SAMPLE_INSIGHTS
        assert data["disclaimer"] == DISCLAIMER
        assert data["portfolio"]["id"] == my_funded_portfolio.pk

    def test_every_insight_has_a_known_category(self, api, my_funded_portfolio, fake_gemini):
        with fake_gemini():
            data = api.post(url(my_funded_portfolio.pk)).json()["data"]

        for insight in data["insights"]:
            assert insight["category"] in gemini.CATEGORIES
            assert insight["text"]

    def test_model_failure_is_200_with_available_false(self, api, my_funded_portfolio, fake_gemini):
        """The dashboard must never see a 500 from this endpoint."""
        with fake_gemini(side_effect=gemini.GeminiRateLimited("429")):
            response = api.post(url(my_funded_portfolio.pk))

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["available"] is False
        assert data["reason"] == "rate_limited"
        assert data["message"] == UNAVAILABLE_MESSAGE
        assert data["insights"] == []
        assert data["disclaimer"] == DISCLAIMER

    def test_timeout_is_200_with_available_false(self, api, my_funded_portfolio, fake_gemini):
        with fake_gemini(side_effect=gemini.GeminiUnavailable("timeout")):
            data = api.post(url(my_funded_portfolio.pk)).json()["data"]

        assert data["available"] is False
        assert data["reason"] == "unavailable"

    def test_anonymous_callers_are_401_and_the_model_is_not_called(
        self, anon, my_funded_portfolio, fake_gemini
    ):
        with fake_gemini() as generate:
            response = anon.post(url(my_funded_portfolio.pk))

        assert response.status_code == 401
        assert response.json()["success"] is False
        assert generate.call_count == 0

    def test_get_is_not_allowed(self, api, my_funded_portfolio, fake_gemini):
        with fake_gemini() as generate:
            response = api.get(url(my_funded_portfolio.pk))

        assert response.status_code == 405
        assert generate.call_count == 0

    def test_reads_the_callers_own_portfolio_whatever_the_url_says(
        self, api, my_funded_portfolio, fake_gemini
    ):
        """Same scoping rule as /api/risk/: a signed-in user gets their own."""
        with fake_gemini():
            data = api.post(url(999_999)).json()["data"]

        assert data["portfolio"]["id"] == my_funded_portfolio.pk

    def test_empty_portfolio_keeps_its_existing_error_code(self, api, app_user, fake_gemini):
        """Data errors are not flattened into 'unavailable' - the client knows these codes."""
        with fake_gemini() as generate:
            response = api.post(url(app_user.user.portfolios.first().pk))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_portfolio"
        assert generate.call_count == 0

    def test_dead_tickers_are_named_in_the_response(self, api, with_dead_ticker, fake_gemini):
        with fake_gemini():
            data = api.post(url(with_dead_ticker.pk)).json()["data"]

        assert [e["ticker"] for e in data["excluded"]] == ["DEADCO.NS"]
        assert data["excluded"][0]["reason"] == "no_price"

    def test_the_response_never_carries_the_key(
        self, api, my_funded_portfolio, fake_gemini, gemini_settings
    ):
        with fake_gemini():
            response = api.post(url(my_funded_portfolio.pk))

        assert gemini_settings.GEMINI_API_KEY not in response.content.decode()
