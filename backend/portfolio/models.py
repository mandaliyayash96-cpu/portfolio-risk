"""
Portfolio domain models: what the investor owns and how they got there.

Money/quantity are Decimal at this layer — see common/MONEY.md.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from common.models import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    QUANTITY_DECIMAL_PLACES,
    QUANTITY_MAX_DIGITS,
    TICKER_MAX_LENGTH,
    TimeStampedModel,
)

ZERO = Decimal("0")


class AssetType(models.TextChoices):
    EQUITY = "EQUITY", "Equity"
    ETF = "ETF", "ETF"
    MUTUAL_FUND = "MUTUAL_FUND", "Mutual fund"
    BOND = "BOND", "Bond"
    CASH = "CASH", "Cash"
    CRYPTO = "CRYPTO", "Crypto"
    OTHER = "OTHER", "Other"


class TransactionSide(models.TextChoices):
    BUY = "BUY", "Buy"
    SELL = "SELL", "Sell"


class Portfolio(TimeStampedModel):
    """A named basket of holdings belonging to one user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="portfolios",
    )
    name = models.CharField(max_length=120)
    base_currency = models.CharField(
        max_length=3,
        default="INR",
        help_text="ISO 4217 code. All holdings are assumed quoted in this currency.",
    )

    class Meta:
        ordering = ["user", "name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="uniq_portfolio_per_user_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.user})"

    @property
    def total_cost_basis(self) -> Decimal:
        """Sum of quantity * avg_buy_price over holdings. Decimal, exact."""
        return sum((h.cost_basis for h in self.holdings.all()), ZERO)


class Holding(TimeStampedModel):
    """
    A current position: one row per ticker per portfolio.

    `avg_buy_price` is the blended cost of the position, so individual lots are
    not tracked here — Transaction is the audit trail.

    TODO Phase 4: recompute quantity/avg_buy_price from Transactions in
    portfolio/services.py instead of trusting hand-entered values.
    """

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="holdings")
    ticker = models.CharField(
        max_length=TICKER_MAX_LENGTH,
        db_index=True,
        help_text="yfinance symbol. Indian equities use the .NS suffix, e.g. RELIANCE.NS",
    )
    quantity = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
    )
    avg_buy_price = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
    )
    buy_date = models.DateField(help_text="Date the position was opened.")
    asset_type = models.CharField(max_length=16, choices=AssetType, default=AssetType.EQUITY)
    sector = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Free text for now, e.g. 'Energy'. Drives sector concentration later.",
    )

    class Meta:
        ordering = ["portfolio", "ticker"]
        constraints = [
            models.UniqueConstraint(
                fields=["portfolio", "ticker"], name="uniq_holding_per_portfolio_ticker"
            ),
        ]
        indexes = [models.Index(fields=["ticker"], name="holding_ticker_idx")]

    def __str__(self) -> str:
        return f"{self.ticker} x {self.quantity}"

    @property
    def cost_basis(self) -> Decimal:
        """quantity * avg_buy_price. Decimal, exact — never float."""
        return self.quantity * self.avg_buy_price


class Transaction(TimeStampedModel):
    """An executed buy or sell. Append-only audit trail behind the holdings."""

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="transactions")
    ticker = models.CharField(max_length=TICKER_MAX_LENGTH, db_index=True)
    side = models.CharField(max_length=4, choices=TransactionSide)
    quantity = models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
    )
    price = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        validators=[MinValueValidator(ZERO)],
        help_text="Execution price per unit.",
    )
    timestamp = models.DateTimeField(default=timezone.now)
    # TODO Phase 4: brokerage/STT/stamp-duty fees, and a realised-P&L column.

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["portfolio", "ticker", "timestamp"], name="txn_pf_ticker_ts_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.side} {self.quantity} {self.ticker} @ {self.price}"

    @property
    def gross_value(self) -> Decimal:
        """quantity * price, before any fees. Decimal, exact."""
        return self.quantity * self.price
