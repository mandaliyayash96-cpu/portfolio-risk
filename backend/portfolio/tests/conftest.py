"""
Fixtures for the holdings-entry tests.

NOTHING HERE TOUCHES THE NETWORK
--------------------------------
`portfolio.services` warms prices for newly added tickers, which in production
means yfinance. Every test that exercises the write path therefore either
switches the fetch off (`fetch_prices=False`, for the tests that are about
validation) or routes it through `StubProvider` below - a MarketDataProvider
implementation that knows four symbols and raises the real UnknownTickerError
for everything else.

That stub is reached two different ways, and both are needed:

  * service tests pass `provider=stub_provider` directly, the same override
    `marketdata.services` already documents "mainly for tests";
  * view tests cannot - the view builds its own - so they point
    settings.MARKET_DATA_PROVIDER at this module's stub instead, which is the
    swap `marketdata.providers.get_provider` exists to make possible.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.selectors import get_my_portfolio
from accounts.services import resolve_app_user
from common.exceptions import UnknownTickerError
from marketdata.providers import MarketDataProvider
from payments.models import Payment, PaymentStatus
from portfolio.models import Holding, Portfolio

#: Where settings.MARKET_DATA_PROVIDER points during the view tests. Spelled
#: once because a typo here would silently fall back to the real yfinance
#: provider and the suite would start making network calls.
STUB_PROVIDER_PATH = "portfolio.tests.conftest.StubProvider"


class StubProvider(MarketDataProvider):
    """
    A market data feed with four symbols in it and no sockets.

    Deterministic on purpose: `get_history` returns a flat-ish ramp rather than
    a random walk, because no test here measures risk - they only care that
    rows landed, or that an unknown symbol produced a warning instead of an
    exception.
    """

    name = "stub"

    KNOWN = {
        "RELIANCE.NS": Decimal("1000.0000"),
        "TCS.NS": Decimal("2000.0000"),
        "INFY.NS": Decimal("1500.0000"),
        "HDFCBANK.NS": Decimal("1600.0000"),
    }

    def _price(self, ticker: str) -> Decimal:
        symbol = (ticker or "").strip().upper()
        if symbol not in self.KNOWN:
            raise UnknownTickerError(
                f"Unknown or delisted ticker: {symbol}.", details={"ticker": symbol}
            )
        return self.KNOWN[symbol]

    def get_live_price(self, ticker: str) -> Decimal:
        return self._price(ticker)

    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        base = self._price(ticker)
        start = date(2026, 1, 5)
        return [
            (start + timedelta(days=offset), base + Decimal(offset))
            for offset in range(min(days, 5))
        ]


@pytest.fixture
def stub_provider() -> StubProvider:
    """The provider instance the service tests pass in explicitly."""
    return StubProvider()


@pytest.fixture
def stub_provider_setting(settings) -> str:
    """
    Point the whole process at the stub, for tests that go through a view.

    Assigning through pytest-django's `settings` fixture undoes itself at
    teardown, so a later test cannot inherit a stubbed feed by accident.
    """
    settings.MARKET_DATA_PROVIDER = STUB_PROVIDER_PATH
    return STUB_PROVIDER_PATH


@pytest.fixture
def investor(db):
    """
    A signed-in investor, built the way a first login builds one.

    Not a bare create_user any more. Two things changed under these tests: a
    holdings WRITE now needs an account that can hold a payment (Part 3), and
    an authenticated caller is scoped to their OWN portfolio rather than to the
    id in the URL (Part 1). So the user and the portfolio under test have to be
    the same pair the application itself would have created.
    """
    return resolve_app_user("+919876500001").user


@pytest.fixture
def portfolio(investor) -> Portfolio:
    """
    The investor's own portfolio - the one `accounts` auto-created, still empty.

    Returned rather than created: `resolve_portfolio_id` sends every
    authenticated write to `get_my_portfolio(user)`, so a second portfolio made
    here would be one the endpoints never touch.
    """
    return get_my_portfolio(investor)


@pytest.fixture
def holding_factory(portfolio):
    """A position that already exists, for the update and delete paths."""

    def _make(
        ticker: str = "RELIANCE.NS",
        quantity: str = "10",
        avg_buy_price: str = "1000.0000",
    ) -> Holding:
        return Holding.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            quantity=Decimal(quantity),
            avg_buy_price=Decimal(avg_buy_price),
            buy_date=date(2026, 1, 5),
        )

    return _make


@pytest.fixture
def api() -> APIClient:
    """An anonymous client. Reads are free and still work with no identity."""
    return APIClient()


@pytest.fixture
def editor(investor) -> APIClient:
    """
    A signed-in client holding a paid editing unlock.

    Both halves, because a holdings write now needs both: an account (401
    without one) and a live ₹9 grant (402 without one). The grant is written
    directly rather than bought through checkout, so a failure in these tests
    means the HOLDINGS endpoint broke - `payments/tests/` owns the question of
    how a grant is obtained and when it ends.
    """
    Payment.objects.create(
        user=investor,
        razorpay_order_id="order_TESTHOLDINGSEDIT",
        razorpay_payment_id="pay_TESTHOLDINGSEDIT",
        amount=900,
        currency="INR",
        status=PaymentStatus.PAID,
        paid_at=timezone.now(),
    )
    client = APIClient()
    client.force_authenticate(user=investor)
    return client


@pytest.fixture
def csv_upload():
    """
    Build an UploadedFile the way a browser would send one.

    SimpleUploadedFile rather than BytesIO because the service checks `.name`
    and `.size`, and a test that skipped those would not be exercising the
    same code path the endpoint runs.
    """

    def _make(text: str, filename: str = "holdings.csv") -> SimpleUploadedFile:
        return SimpleUploadedFile(filename, text.encode("utf-8"), content_type="text/csv")

    return _make


@pytest.fixture
def good_csv() -> str:
    """Two clean rows, both symbols known to the stub."""
    return (
        "ticker,quantity,avg_buy_price,buy_date,asset_type,sector\n"
        "RELIANCE.NS,10,1400.50,2026-01-05,EQUITY,Energy\n"
        "TCS.NS,5,3200.00,2026-01-06,EQUITY,IT\n"
    )
