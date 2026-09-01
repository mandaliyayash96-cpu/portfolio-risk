"""
The gate: which requests need a paid unlock, and which never do.

Split from test_payments.py because these are about the HOLDINGS endpoints
rather than the payment ones. The two files answer different questions - "did
they really pay" over there, "what does paying buy" here.

Nothing in this file touches the market data provider: holdings are written
straight through the ORM where a fixture needs one, and the one POST that would
warm prices is pointed at the stub via settings.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from accounts.selectors import get_my_portfolio
from payments.models import Payment, PaymentStatus
from payments.selectors import user_has_unlock
from portfolio.models import Holding
from portfolio.tests.conftest import STUB_PROVIDER_PATH

pytestmark = pytest.mark.django_db


@pytest.fixture
def portfolio(app_user):
    """The portfolio Part 1 auto-created for this account."""
    return get_my_portfolio(app_user.user)


@pytest.fixture
def holding(portfolio):
    return Holding.objects.create(
        portfolio=portfolio,
        ticker="RELIANCE.NS",
        quantity=Decimal("10"),
        avg_buy_price=Decimal("1000.0000"),
        buy_date=date(2026, 1, 5),
    )


@pytest.fixture
def stub_prices(settings):
    """Keep the add-holding path off the network."""
    settings.MARKET_DATA_PROVIDER = STUB_PROVIDER_PATH
    return settings


def holdings_url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/holdings/"


def body(response) -> dict:
    return response.json()


ADD_PAYLOAD = {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "1400.50"}

#: A valid two-row upload. Built here rather than borrowed from
#: portfolio/tests/conftest.py because fixtures do not cross package
#: boundaries, and a CSV that must never be parsed (the gate refuses first) is
#: not worth a shared fixture.
GOOD_CSV = "\n".join(
    ["ticker,quantity,avg_buy_price", "RELIANCE.NS,10,1400.50", "TCS.NS,5,3200.00", ""]
)


def csv_file() -> SimpleUploadedFile:
    return SimpleUploadedFile("holdings.csv", GOOD_CSV.encode("utf-8"), content_type="text/csv")


class TestWritesNeedAnUnlock:
    def test_add_without_an_unlock_is_402(self, api, portfolio, stub_prices):
        response = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 402
        payload = body(response)
        assert payload["success"] is False
        assert payload["data"] is None
        assert payload["error"]["code"] == "payment_required"
        assert Holding.objects.count() == 0

    def test_csv_import_without_an_unlock_is_402(self, api, portfolio):
        response = api.post(
            f"{holdings_url(portfolio.pk)}import/",
            {"file": csv_file()},
            format="multipart",
        )

        assert response.status_code == 402
        assert Holding.objects.count() == 0

    def test_delete_without_an_unlock_is_402(self, api, portfolio, holding):
        response = api.delete(f"{holdings_url(portfolio.pk)}{holding.pk}/")

        assert response.status_code == 402
        assert Holding.objects.filter(pk=holding.pk).exists()

    def test_an_anonymous_write_is_401_not_402(self, anon, portfolio, stub_prices):
        """
        Nothing to pay WITH. Telling a signed-out visitor to pay ₹9 would send
        them to a checkout that cannot attach the payment to anybody.
        """
        response = anon.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 401
        assert body(response)["error"]["code"] == "not_authenticated"


class TestAnUnlockOpensTheRound:
    def test_add_succeeds_with_a_paid_unlock(self, api, portfolio, paid_unlock, stub_prices):
        paid_unlock()

        response = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 201
        assert Holding.objects.filter(portfolio=portfolio, ticker="RELIANCE.NS").exists()

    def test_delete_succeeds_with_a_paid_unlock(self, api, portfolio, holding, paid_unlock):
        paid_unlock()

        response = api.delete(f"{holdings_url(portfolio.pk)}{holding.pk}/")

        assert response.status_code == 200
        assert not Holding.objects.filter(pk=holding.pk).exists()

    def test_one_unlock_covers_several_edits(self, api, portfolio, paid_unlock, stub_prices):
        """
        The point of charging per ROUND. A write does not consume the grant, so
        adding two positions and deleting one is a single ₹9.
        """
        paid_unlock()

        first = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")
        second = api.post(
            holdings_url(portfolio.pk),
            {"ticker": "TCS.NS", "quantity": "5", "avg_buy_price": "3200.00"},
            format="json",
        )
        added = Holding.objects.get(ticker="TCS.NS")
        third = api.delete(f"{holdings_url(portfolio.pk)}{added.pk}/")

        assert [first.status_code, second.status_code, third.status_code] == [201, 201, 200]
        assert Payment.objects.filter(consumed=False, status=PaymentStatus.PAID).count() == 1

    def test_the_round_ends_when_the_panel_closes(self, api, portfolio, paid_unlock, stub_prices):
        """Condition 1: close the panel, and the next edit costs another ₹9."""
        paid_unlock()

        assert api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json").status_code == 201
        api.post("/api/payments/finish/", {})
        after = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert after.status_code == 402

    def test_an_expired_unlock_does_not_open_anything(
        self, api, portfolio, paid_unlock, stub_prices, settings
    ):
        """
        Condition 3, and the one that does not depend on the client behaving.
        A grant older than the TTL is dead however it got that way.
        """
        paid_unlock(paid_at=timezone.now() - settings.EDITING_UNLOCK_TTL - timedelta(minutes=1))

        response = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 402
        # And it is retired in the database, not merely filtered out of a query.
        assert Payment.objects.filter(consumed=False, status=PaymentStatus.PAID).count() == 0

    def test_another_users_unlock_does_not_open_this_ones_round(
        self, api, portfolio, paid_unlock, other_app_user, stub_prices
    ):
        paid_unlock(user=other_app_user.user)

        response = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 402

    @pytest.mark.parametrize("status", [PaymentStatus.CREATED, PaymentStatus.FAILED])
    def test_an_unpaid_row_is_not_an_unlock(self, api, portfolio, paid_unlock, stub_prices, status):
        """An order that was created, or one whose signature failed, buys nothing."""
        paid_unlock(status=status, paid_at=None)

        response = api.post(holdings_url(portfolio.pk), ADD_PAYLOAD, format="json")

        assert response.status_code == 402


class TestReadsAreFree:
    def test_holdings_list_needs_no_payment(self, api, portfolio, holding):
        response = api.get(holdings_url(portfolio.pk))

        assert response.status_code == 200
        assert [row["ticker"] for row in body(response)["data"]] == ["RELIANCE.NS"]

    def test_an_anonymous_read_still_works(self, anon, portfolio, holding):
        """The dashboard has to be worth looking at before anyone pays to edit it."""
        response = anon.get(holdings_url(portfolio.pk))

        assert response.status_code == 200

    def test_the_risk_report_needs_no_payment(self, api, portfolio, holding):
        """
        422 rather than 200 only because this portfolio has no stored prices -
        which is exactly the point: it is a DATA answer, not a payment one.
        """
        response = api.get(f"/api/risk/{portfolio.pk}/")

        assert response.status_code != 402
        assert body(response)["error"]["code"] in {"missing_price_data", "insufficient_history"}


class TestUserHasUnlock:
    def test_true_only_for_a_live_grant(self, app_user, paid_unlock):
        assert user_has_unlock(app_user.user) is False

        grant = paid_unlock()
        assert user_has_unlock(app_user.user) is True

        grant.consumed = True
        grant.save(update_fields=["consumed"])
        assert user_has_unlock(app_user.user) is False

    def test_false_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        assert user_has_unlock(AnonymousUser()) is False
        assert user_has_unlock(None) is False
