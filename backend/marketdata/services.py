"""
Market data writes (architecture rule 1: all writes go through services).

Everything here talks to the feed through `marketdata.providers.get_provider()`
and never imports yfinance. Money crosses into the DB as Decimal - see
common/MONEY.md.

Batch semantics differ on purpose:
  * fetch_live()    - one bad ticker must not sink the whole poll, so
                      per-ticker ProviderErrors are collected, not raised.
  * fetch_history() - single-ticker call, so the error is the caller's to
                      handle and propagates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from common.exceptions import ProviderError
from marketdata.models import PriceHistory, PriceSnapshot
from marketdata.providers import MarketDataProvider, get_provider

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS = 252


@dataclass(frozen=True)
class LiveFetchResult:
    """Outcome of one fetch_live() call."""

    prices: dict[str, Decimal] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def fetched(self) -> int:
        return len(self.prices)

    @property
    def failed(self) -> int:
        return len(self.errors)


@dataclass(frozen=True)
class HistoryFetchResult:
    """Outcome of one fetch_history() call."""

    ticker: str
    created: int
    updated: int
    first_date: date | None
    last_date: date | None

    @property
    def rows(self) -> int:
        """Rows written this run (inserted + refreshed)."""
        return self.created + self.updated


def _normalise(tickers) -> list[str]:
    """Upper-case, strip, drop blanks, de-duplicate, keep input order."""
    seen: dict[str, None] = {}
    for raw in tickers or []:
        cleaned = (raw or "").strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def fetch_live(
    tickers: list[str], *, provider: MarketDataProvider | None = None
) -> LiveFetchResult:
    """
    Fetch the current price for each ticker and upsert it into PriceSnapshot.

    One row per ticker, updated in place - the latest poll wins.

    Args:
        tickers: symbols in provider format (e.g. ["RELIANCE.NS", "^NSEI"]).
        provider: override, mainly for tests. Defaults to the configured one.

    Returns:
        LiveFetchResult with the prices written and a message per failed ticker.
        Never raises for a single bad ticker; a misconfigured provider still
        raises, because that is not a per-ticker problem.
    """
    provider = provider or get_provider()
    prices: dict[str, Decimal] = {}
    errors: dict[str, str] = {}

    for ticker in _normalise(tickers):
        try:
            price = provider.get_live_price(ticker)
        except ProviderError as exc:
            logger.warning("Live price failed for %s: %s", ticker, exc.message)
            errors[ticker] = exc.message
            continue

        PriceSnapshot.objects.update_or_create(
            ticker=ticker,
            defaults={"price": price, "timestamp": timezone.now()},
        )
        prices[ticker] = price

    return LiveFetchResult(prices=prices, errors=errors)


def fetch_history(
    ticker: str,
    days: int = DEFAULT_HISTORY_DAYS,
    *,
    provider: MarketDataProvider | None = None,
) -> HistoryFetchResult:
    """
    Fetch `days` trading days of daily closes and upsert them into PriceHistory.

    Idempotent on (ticker, date): re-running over an overlapping window
    refreshes the existing rows rather than duplicating them, so this is safe
    to schedule.

    Raises:
        ProviderError (or UnknownTickerError / EmptyHistoryError) if the feed
        cannot supply the series.
    """
    provider = provider or get_provider()
    rows = provider.get_history(ticker, days)
    symbol = (ticker or "").strip().upper()

    if not rows:
        return HistoryFetchResult(symbol, created=0, updated=0, first_date=None, last_date=None)

    dates = [row_date for row_date, _ in rows]

    with transaction.atomic():
        # Counted before the write, so "created" and "updated" are exact rather
        # than inferred from the upsert.
        existing = set(
            PriceHistory.objects.filter(ticker=symbol, date__in=dates).values_list(
                "date", flat=True
            )
        )
        PriceHistory.objects.bulk_create(
            [
                PriceHistory(ticker=symbol, date=row_date, close=close)
                for row_date, close in rows
            ],
            update_conflicts=True,
            update_fields=["close", "updated_at"],
            unique_fields=["ticker", "date"],
        )

    return HistoryFetchResult(
        ticker=symbol,
        created=len(dates) - len(existing),
        updated=len(existing),
        first_date=min(dates),
        last_date=max(dates),
    )


# TODO Phase 6: Celery Beat calls these on a schedule.
#
#   marketdata/tasks.py
#       @shared_task
#       def poll_prices():
#           fetch_live(portfolio.selectors.get_all_held_tickers())
#
#       @shared_task
#       def refresh_history(days=DEFAULT_HISTORY_DAYS):
#           for ticker in portfolio.selectors.get_all_held_tickers():
#               fetch_history(ticker, days)
#
#   settings.CELERY_BEAT_SCHEDULE
#       poll_prices      -> every 60s during market hours
#       refresh_history  -> once daily after the NSE close (15:30 IST)
#
# Until then, `manage.py fetch_prices` is the manual entry point.
