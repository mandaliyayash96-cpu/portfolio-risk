"""
Shared abstract models and the numeric field precisions every app must use.

Money/quantity contract (architecture rule 5) — see common/MONEY.md:
Decimal everywhere the ORM is involved, float64 only inside risk/engine.py.
"""

from django.db import models

# Prices, costs, thresholds, metric values. 18 digits total, 4 after the point:
# enough for a 99-crore share price quoted in paise.
MONEY_MAX_DIGITS = 18
MONEY_DECIMAL_PLACES = 4

# Share/unit counts. Mutual fund units routinely carry 3-4 decimals; 6 is slack.
QUANTITY_MAX_DIGITS = 20
QUANTITY_DECIMAL_PLACES = 6

# Ticker symbols as yfinance knows them: "RELIANCE.NS", "^NSEI", "AAPL".
TICKER_MAX_LENGTH = 32


class TimeStampedModel(models.Model):
    """Abstract base giving every concrete model created_at / updated_at."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
