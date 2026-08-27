"""
Market data reads (architecture rule 1: all reads go through selectors).

This module is the Decimal -> float64 boundary described in common/MONEY.md:
prices live in the DB as Decimal, and `get_history_df` converts them exactly
once on the way into pandas, because an object-dtype DataFrame of Decimals is
useless to NumPy.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from marketdata.models import PriceHistory, PriceSnapshot

DEFAULT_HISTORY_DAYS = 252

#: Column name every price frame uses, so the risk engine can rely on it.
CLOSE_COLUMN = "close"
DATE_INDEX_NAME = "date"


def get_latest_price(ticker: str) -> Decimal | None:
    """
    Current price for `ticker` from PriceSnapshot, or None if never fetched.

    Returns Decimal - callers doing money math must not convert to float.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    return (
        PriceSnapshot.objects.filter(ticker=symbol)
        .values_list("price", flat=True)
        .first()
    )


def get_latest_prices(tickers: list[str]) -> dict[str, Decimal]:
    """
    Current price for several tickers in one query: {ticker: Decimal}.

    Tickers with no snapshot are simply absent from the mapping.
    """
    symbols = [(t or "").strip().upper() for t in tickers or []]
    symbols = [t for t in symbols if t]
    if not symbols:
        return {}
    return dict(
        PriceSnapshot.objects.filter(ticker__in=symbols).values_list("ticker", "price")
    )


def get_history_df(ticker: str, days: int = DEFAULT_HISTORY_DAYS) -> pd.DataFrame:
    """
    The last `days` stored daily closes for `ticker`, oldest first.

    Returns:
        DataFrame with a tz-naive DatetimeIndex named "date" and a single
        float64 column "close". Empty (but correctly typed and indexed) when
        nothing is stored, so callers can chain without None checks.
    """
    symbol = (ticker or "").strip().upper()
    rows: list[tuple] = []
    if symbol:
        rows = list(
            PriceHistory.objects.filter(ticker=symbol)
            .order_by("-date")
            .values_list("date", "close")[: max(int(days), 1)]
        )
    rows.reverse()  # newest-first query -> oldest-first series

    if not rows:
        return pd.DataFrame(
            {CLOSE_COLUMN: pd.Series(dtype="float64")},
            index=pd.DatetimeIndex([], name=DATE_INDEX_NAME),
        )

    # The one and only Decimal -> float64 conversion (common/MONEY.md).
    index = pd.DatetimeIndex([row[0] for row in rows], name=DATE_INDEX_NAME)
    closes = [float(row[1]) for row in rows]
    return pd.DataFrame({CLOSE_COLUMN: closes}, index=index)


def get_stored_tickers() -> list[str]:
    """Every ticker with at least one stored daily close, alphabetically."""
    return list(
        PriceHistory.objects.order_by("ticker").values_list("ticker", flat=True).distinct()
    )


# TODO Phase 3: price_history_frame(tickers, days) -> one wide DataFrame,
#               inner-joined across tickers so the risk engine gets aligned rows.
