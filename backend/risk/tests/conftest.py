"""
Shared fixtures for the Django-backed risk tests.

test_engine.py deliberately imports no Django; nothing here is imported by it.
"""

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from django.contrib.auth import get_user_model

from marketdata.models import PriceHistory, PriceSnapshot
from portfolio.models import Holding, Portfolio

BENCHMARK = "^NSEI"


def business_days(count: int, start: date = date(2026, 1, 5)) -> list[date]:
    """`count` consecutive weekdays from `start` - a crude NSE calendar."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def make_history(
    ticker: str,
    *,
    days: int = 90,
    start: date = date(2026, 1, 5),
    base: float = 100.0,
    seed: int = 7,
    drift: float = 0.0004,
    vol: float = 0.011,
) -> list[date]:
    """
    Store a seeded random-walk close series and return the dates written.

    Seeded so a failure is reproducible: an unseeded walk can wander into a
    genuinely zero-volatility stretch and make a metric assertion flap.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, days)
    closes = base * np.cumprod(1.0 + steps)
    dates = business_days(days, start)
    PriceHistory.objects.bulk_create(
        [
            PriceHistory(ticker=ticker, date=day, close=Decimal(f"{close:.4f}"))
            for day, close in zip(dates, closes)
        ]
    )
    return dates


def make_snapshot(ticker: str, price: str) -> PriceSnapshot:
    """The live price used to value a position."""
    from django.utils import timezone

    return PriceSnapshot.objects.create(
        ticker=ticker, price=Decimal(price), timestamp=timezone.now()
    )


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="investor", password="x")


@pytest.fixture
def portfolio(user) -> Portfolio:
    """An empty portfolio - tests add whatever holdings they need."""
    return Portfolio.objects.create(user=user, name="Core", base_currency="INR")


@pytest.fixture
def holding_factory(portfolio):
    def _make(ticker: str, quantity: str, avg_buy_price: str = "100.0000") -> Holding:
        return Holding.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            quantity=Decimal(quantity),
            avg_buy_price=Decimal(avg_buy_price),
            buy_date=date(2026, 1, 5),
        )

    return _make


@pytest.fixture
def funded_portfolio(portfolio, holding_factory) -> Portfolio:
    """
    Two holdings and the benchmark, each with 90 days of stored closes and a
    live snapshot. Quantities and snapshot prices are chosen so the two
    positions are worth exactly 100,000 each - a 50/50 portfolio.
    """
    holding_factory("RELIANCE.NS", "100")
    holding_factory("TCS.NS", "50")

    make_history("RELIANCE.NS", seed=1, base=100.0)
    make_history("TCS.NS", seed=2, base=200.0)
    make_history(BENCHMARK, seed=3, base=22000.0, vol=0.008)

    make_snapshot("RELIANCE.NS", "1000.0000")
    make_snapshot("TCS.NS", "2000.0000")
    make_snapshot(BENCHMARK, "22500.0000")
    return portfolio
