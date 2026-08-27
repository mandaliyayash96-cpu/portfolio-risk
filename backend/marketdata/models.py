"""
Market data storage.

PriceSnapshot is the live tape (append-only, latest row wins); PriceHistory is
the daily close series the risk engine turns into returns.

Nothing in this app imports yfinance yet - Phase 2 adds a provider interface and
a yfinance implementation behind it (architecture rule 4).
"""

from django.db import models

from common.models import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    TICKER_MAX_LENGTH,
    TimeStampedModel,
)


class PriceSnapshot(TimeStampedModel):
    """
    The current live price for one ticker: exactly one row per ticker, updated
    in place on every poll (latest wins).

    TODO Phase 6: price_move_pct alerts therefore cannot diff two snapshots -
    compare this live price against the previous close in PriceHistory instead.
    """

    ticker = models.CharField(max_length=TICKER_MAX_LENGTH, unique=True)
    price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    timestamp = models.DateTimeField(help_text="When this price was fetched (UTC).")

    class Meta:
        ordering = ["ticker"]
        verbose_name = "price snapshot"
        verbose_name_plural = "price snapshots"

    def __str__(self) -> str:
        return f"{self.ticker} @ {self.price} ({self.timestamp:%Y-%m-%d %H:%M})"


class PriceHistory(TimeStampedModel):
    """
    One daily close for one ticker - the raw material for the returns matrix.

    Unique on (ticker, date) so a re-fetch of an overlapping window is an
    idempotent upsert rather than a duplicate.
    """

    ticker = models.CharField(max_length=TICKER_MAX_LENGTH, db_index=True)
    date = models.DateField()
    close = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    # TODO Phase 2: adjusted close / volume if splits and dividends start to matter.

    class Meta:
        ordering = ["ticker", "-date"]
        verbose_name = "price history"
        verbose_name_plural = "price history"
        constraints = [
            models.UniqueConstraint(fields=["ticker", "date"], name="uniq_price_per_ticker_date"),
        ]
        indexes = [models.Index(fields=["ticker", "date"], name="history_ticker_date_idx")]

    def __str__(self) -> str:
        return f"{self.ticker} {self.date} close={self.close}"
