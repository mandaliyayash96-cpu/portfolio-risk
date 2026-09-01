"""
Whose portfolio the DATA endpoints read once a caller is signed in.

The rule under test (accounts.selectors.resolve_portfolio_id):

    signed in with a Firebase token  ->  the caller's own portfolio,
                                         whatever id the URL carries
    anything else                    ->  the id in the URL, unchanged

The second half - an anonymous caller still addressing the id in the URL - is
asserted here too, because it is a deliberate decision rather than an oversight
and its removal should be a visible edit to this file.

That decision survived Part 3. Payment gating closed the three holdings WRITES
(401 without an account, 402 without a paid unlock); READS were deliberately
left open, so the dashboard is worth looking at before anyone is asked to pay
to change it, and `curl /api/risk/1/` still works.

Nothing here goes near the market data provider: every row is created through
the ORM, and every endpoint exercised is a read - except the one delete, which
grants itself an unlock first.
"""

from datetime import date
from decimal import Decimal

import pytest

from accounts.tests.conftest import PHONE
from portfolio.models import Holding, Portfolio

pytestmark = pytest.mark.django_db

SESSION_URL = "/api/auth/session/"


def holdings_url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/"


def add_holding_row(portfolio: Portfolio, ticker: str = "RELIANCE.NS") -> Holding:
    """A position, written straight to the ORM - no provider, no network."""
    return Holding.objects.create(
        portfolio=portfolio,
        ticker=ticker,
        quantity=Decimal("10"),
        avg_buy_price=Decimal("1000.0000"),
        buy_date=date(2026, 1, 5),
    )


@pytest.fixture
def my_portfolio(signed_in) -> Portfolio:
    """Sign in once so the account and its portfolio exist, then hand it back."""
    with signed_in() as (api, _):
        api.post(SESSION_URL, {})
    return Portfolio.objects.get(user__app_user__phone_number=PHONE)


class TestAuthenticatedCallersReadTheirOwnPortfolio:
    def test_holdings_ignores_the_id_in_the_url(
        self, signed_in, my_portfolio, other_investors_portfolio
    ):
        add_holding_row(my_portfolio, "INFY.NS")
        add_holding_row(other_investors_portfolio, "TCS.NS")

        with signed_in() as (api, _):
            response = api.get(holdings_url(other_investors_portfolio.pk))

        assert response.status_code == 200
        tickers = [row["ticker"] for row in response.json()["data"]]
        assert tickers == ["INFY.NS"]

    def test_the_returned_rows_belong_to_the_caller(
        self, signed_in, my_portfolio, other_investors_portfolio
    ):
        add_holding_row(my_portfolio, "INFY.NS")

        with signed_in() as (api, _):
            response = api.get(holdings_url(other_investors_portfolio.pk))

        assert {row["portfolio_id"] for row in response.json()["data"]} == {my_portfolio.pk}

    def test_delete_cannot_reach_into_another_portfolio(
        self, signed_in, my_portfolio, other_investors_portfolio, paid_unlock
    ):
        """
        Both ids guessed correctly and it still 404s: the portfolio id is
        replaced by the caller's own before it is used to scope the lookup.

        The unlock is granted first so that the 404 is the SCOPING answer.
        Without it the request would stop at the payment gate with a 402, and
        this test would pass while proving nothing about whose data it is.
        """
        theirs = add_holding_row(other_investors_portfolio, "TCS.NS")
        paid_unlock()

        with signed_in() as (api, _):
            response = api.delete(
                f"/api/portfolio/{other_investors_portfolio.pk}/holdings/{theirs.pk}/"
            )

        assert response.status_code == 404
        assert Holding.objects.filter(pk=theirs.pk).exists()

    def test_the_risk_report_is_computed_on_the_callers_own_portfolio(
        self, signed_in, my_portfolio, other_investors_portfolio
    ):
        """
        The caller's portfolio is empty and the URL's is not. An `empty_portfolio`
        answer is therefore proof the report was computed on the RIGHT one -
        and it needs no stored prices to prove it.
        """
        add_holding_row(other_investors_portfolio, "TCS.NS")

        with signed_in() as (api, _):
            response = api.get(f"/api/risk/{other_investors_portfolio.pk}/")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_portfolio"


class TestAnonymousCallersReadTheUrlId:
    """
    Reads are open to anyone, and address the id in the URL.

    Still true after Part 3, and on purpose: only the WRITES are gated. If
    reads are ever closed too, these are the two tests that have to change, and
    they will say exactly what changed.
    """

    def test_an_anonymous_caller_still_reads_the_url_id(self, other_investors_portfolio, api):
        add_holding_row(other_investors_portfolio, "TCS.NS")

        response = api.get(holdings_url(other_investors_portfolio.pk))

        assert response.status_code == 200
        assert [row["ticker"] for row in response.json()["data"]] == ["TCS.NS"]

    def test_an_admin_session_is_not_scoped_away_from_the_url_id(
        self, api, django_user_model, other_investors_portfolio
    ):
        """
        A superuser browsing the API is authenticated but is not an investor,
        so the URL still means what it says - the admin's own (nonexistent)
        portfolio would otherwise 404 every page in the browsable API.
        """
        add_holding_row(other_investors_portfolio, "TCS.NS")
        django_user_model.objects.create_superuser(username="root2", password="pw", email="")
        api.login(username="root2", password="pw")

        response = api.get(holdings_url(other_investors_portfolio.pk))

        assert response.status_code == 200
        assert [row["ticker"] for row in response.json()["data"]] == ["TCS.NS"]
