"""
Fixtures for the scheduled price refresh.

NO BROKER, NO NETWORK
---------------------
The task is called as a plain function. `@shared_task` leaves the underlying
callable intact, so `refresh_all_prices()` runs it here and now - no Redis, no
worker, no `always_eager` juggling. What is being tested is what the task
DOES; that Celery can deliver it is Celery's problem and is covered by the
registry assertions in test_tasks.py.

The provider is swapped through settings.MARKET_DATA_PROVIDER rather than
passed in, because the task calls `fetch_live`/`fetch_history` without a
provider argument - exactly as it will in production. A stub that had to be
injected would be testing a code path that does not exist.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from common.exceptions import ProviderError, UnknownTickerError
from marketdata.providers import MarketDataProvider
from portfolio.models import Holding, Portfolio

#: Dotted paths for settings.MARKET_DATA_PROVIDER. Spelled as constants because
#: a typo in one would silently fall back to the real yfinance provider and the
#: suite would start making network calls.
WORKING_PROVIDER = "marketdata.tests.conftest.WorkingProvider"
FLAKY_PROVIDER = "marketdata.tests.conftest.FlakyProvider"
DEAD_PROVIDER = "marketdata.tests.conftest.DeadProvider"

BENCHMARK = "^NSEI"


class WorkingProvider(MarketDataProvider):
    """Everything succeeds. The baseline the other two are measured against."""

    name = "stub-working"
    PRICE = Decimal("1000.0000")
    HISTORY_ROWS = 5

    def get_live_price(self, ticker: str) -> Decimal:
        return self.PRICE

    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        start = date(2026, 1, 5)
        return [
            (start + timedelta(days=offset), self.PRICE + Decimal(offset))
            for offset in range(min(days, self.HISTORY_ROWS))
        ]


class FlakyProvider(WorkingProvider):
    """
    One named symbol is unknown; everything else works.

    The realistic failure: a delisted ticker or a typo sitting in one holding
    while the rest of the portfolio is perfectly fetchable.
    """

    name = "stub-flaky"
    BROKEN = "BROKEN.NS"

    def get_live_price(self, ticker: str) -> Decimal:
        if ticker.strip().upper() == self.BROKEN:
            raise UnknownTickerError(f"Unknown or delisted ticker: {ticker}.")
        return super().get_live_price(ticker)

    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        if ticker.strip().upper() == self.BROKEN:
            raise UnknownTickerError(f"Unknown or delisted ticker: {ticker}.")
        return super().get_history(ticker, days)


class DeadProvider(MarketDataProvider):
    """
    The feed is down for everything - DNS failure, rate limit, no network.

    This is the case the "never crashes the worker" requirement is really
    about, so it fails the way the real provider does: a ProviderError, which
    is what `marketdata.providers` promises is the only thing that escapes.
    """

    name = "stub-dead"

    def get_live_price(self, ticker: str) -> Decimal:
        raise ProviderError("Market data provider is unavailable.")

    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        raise ProviderError("Market data provider is unavailable.")


@pytest.fixture
def working_provider(settings) -> str:
    """
    Point the process at a provider that works.

    Assigned through pytest-django's `settings` fixture so it is undone at
    teardown and a later test cannot inherit a stub by accident.
    """
    settings.MARKET_DATA_PROVIDER = WORKING_PROVIDER
    return WORKING_PROVIDER


@pytest.fixture
def flaky_provider(settings) -> str:
    settings.MARKET_DATA_PROVIDER = FLAKY_PROVIDER
    return FLAKY_PROVIDER


@pytest.fixture
def dead_provider(settings) -> str:
    settings.MARKET_DATA_PROVIDER = DEAD_PROVIDER
    return DEAD_PROVIDER


@pytest.fixture
def benchmark(settings) -> str:
    """Pin the benchmark so the ticker-list assertions do not depend on env."""
    settings.DEFAULT_BENCHMARK_TICKER = BENCHMARK
    return BENCHMARK


@pytest.fixture
def portfolio(db) -> Portfolio:
    user = get_user_model().objects.create_user(username="poll-investor", password="x")
    return Portfolio.objects.create(user=user, name="Polled", base_currency="INR")


@pytest.fixture
def holding_factory(portfolio):
    def _make(ticker: str, quantity: str = "10") -> Holding:
        return Holding.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            quantity=Decimal(quantity),
            avg_buy_price=Decimal("1000.0000"),
            buy_date=date(2026, 1, 5),
        )

    return _make


@pytest.fixture
def held(holding_factory) -> list[str]:
    """Two ordinary holdings."""
    holding_factory("RELIANCE.NS")
    holding_factory("TCS.NS")
    return ["RELIANCE.NS", "TCS.NS"]
