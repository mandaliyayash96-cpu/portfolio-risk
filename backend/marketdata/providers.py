"""
Market data provider interface and its yfinance implementation.

Architecture rule 4: this is the ONLY module in the codebase permitted to
import yfinance. Everything else goes through `get_provider()` and the
`MarketDataProvider` ABC, so the feed can be swapped (a stub in tests, a paid
feed later) without touching services, selectors, tasks or views.

Errors: no raw yfinance/network exception ever escapes. Everything is wrapped
in ProviderError (or UnknownTickerError / EmptyHistoryError, both subclasses),
which the DRF exception handler already maps onto the standard envelope.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

import yfinance as yf
from yfinance import exceptions as yf_exceptions

from common.exceptions import EmptyHistoryError, ProviderError, UnknownTickerError
from common.models import MONEY_DECIMAL_PLACES

logger = logging.getLogger(__name__)

# Money lands in the DB as Decimal with this many places - see common/MONEY.md.
_MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_DECIMAL_PLACES)

# `days` means TRADING days, but yfinance windows are calendar days. ~252
# trading days fall in ~366 calendar days; 1.5x plus a fortnight is a safe
# over-fetch that gets trimmed back to exactly `days` rows.
_CALENDAR_OVERFETCH = 1.5
_CALENDAR_PADDING_DAYS = 15

_REQUEST_TIMEOUT_SECONDS = 20


class MarketDataProvider(ABC):
    """
    Abstract market data feed.

    Implementations must be safe to construct cheaply and must raise only
    ProviderError subclasses.
    """

    #: Short identifier used in logs and error details.
    name: str = "abstract"

    @abstractmethod
    def get_live_price(self, ticker: str) -> Decimal:
        """
        Latest traded price for `ticker`, as a Decimal quantised to the money
        precision.

        Raises:
            UnknownTickerError: the symbol is not recognised or was delisted.
            ProviderError: the feed is unreachable, rate limited, or returned
                something unusable.
        """

    @abstractmethod
    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        """
        Daily closes for the last `days` TRADING days, oldest first.

        Returns a list of (date, close) pairs. Closes are Decimal, quantised to
        the money precision.

        Raises:
            UnknownTickerError: the symbol is not recognised or was delisted.
            EmptyHistoryError: the symbol resolved but has no usable price rows.
            ProviderError: the feed is unreachable or rate limited.
        """


class YFinanceProvider(MarketDataProvider):
    """
    yfinance-backed provider.

    Indian equities use the .NS suffix (RELIANCE.NS); the NIFTY 50 benchmark is
    ^NSEI. History is fetched with auto_adjust=True, so `close` is adjusted for
    splits and dividends - the correct input for a return series.
    """

    name = "yfinance"

    def __init__(self) -> None:
        _configure_yfinance()

    # -- public API ---------------------------------------------------------
    def get_live_price(self, ticker: str) -> Decimal:
        ticker = _clean_ticker(ticker)
        try:
            price = yf.Ticker(ticker).fast_info["last_price"]
        except Exception as exc:  # noqa: BLE001 - deliberately wrapped below
            logger.debug("fast_info failed for %s: %r", ticker, exc)
            price = None
            fast_info_error: Exception | None = exc
        else:
            fast_info_error = None

        if _is_usable_number(price):
            return _to_money(price, ticker)

        # fast_info can come back empty outside market hours or for thin
        # symbols; the last daily close is a good enough live price.
        try:
            recent = self.get_history(ticker, days=1)
        except ProviderError:
            if fast_info_error is not None:
                raise _wrap(ticker, fast_info_error) from fast_info_error
            raise

        if not recent:
            raise EmptyHistoryError(
                f"No live price or recent close available for {ticker}.",
                details={"ticker": ticker, "provider": self.name},
            )
        return recent[-1][1]

    def get_history(self, ticker: str, days: int) -> list[tuple[date, Decimal]]:
        ticker = _clean_ticker(ticker)
        days = max(int(days), 1)

        end = timezone.localdate() + timedelta(days=1)  # `end` is exclusive
        start = end - timedelta(days=int(days * _CALENDAR_OVERFETCH) + _CALENDAR_PADDING_DAYS)

        try:
            frame = yf.Ticker(ticker).history(
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
                actions=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - deliberately wrapped below
            raise _wrap(ticker, exc) from exc

        if frame is None or frame.empty:
            raise EmptyHistoryError(
                f"Provider returned no price rows for {ticker}.",
                details={"ticker": ticker, "provider": self.name, "days": days},
            )
        if "Close" not in frame.columns:
            raise ProviderError(
                f"Provider response for {ticker} has no Close column.",
                details={"ticker": ticker, "columns": [str(c) for c in frame.columns]},
            )

        frame = frame[frame["Close"].notna()].tail(days)
        if frame.empty:
            raise EmptyHistoryError(
                f"Provider returned only empty closes for {ticker}.",
                details={"ticker": ticker, "provider": self.name, "days": days},
            )

        rows: list[tuple[date, Decimal]] = []
        for stamp, close in zip(frame.index, frame["Close"], strict=True):
            # The index is tz-aware in the exchange's own timezone, so .date()
            # is the local trading date - exactly what PriceHistory stores.
            rows.append((stamp.date(), _to_money(close, ticker)))
        return rows


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
class ImproperlyConfiguredProvider(ProviderError):
    code = "provider_misconfigured"
    message = "Market data provider is misconfigured."


@lru_cache(maxsize=None)
def _build_provider(dotted_path: str) -> MarketDataProvider:
    provider_class = import_string(dotted_path)
    if not issubclass(provider_class, MarketDataProvider):
        raise ImproperlyConfiguredProvider(
            f"{dotted_path} is not a MarketDataProvider subclass."
        )
    return provider_class()


def get_provider() -> MarketDataProvider:
    """
    Return the configured provider instance.

    Reads the dotted path from settings.MARKET_DATA_PROVIDER, so swapping the
    feed - including to a stub in tests - is a settings change, not a code
    change. Instances are cached per dotted path.
    """
    dotted_path = getattr(
        settings, "MARKET_DATA_PROVIDER", "marketdata.providers.YFinanceProvider"
    )
    try:
        return _build_provider(dotted_path)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - bad import path, bad constructor
        raise ImproperlyConfiguredProvider(
            f"Could not load market data provider '{dotted_path}': {exc}",
            details={"provider": dotted_path},
        ) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _configure_yfinance() -> None:
    """
    Make yfinance raise instead of logging-and-returning-empty, so real
    failures can be told apart from genuinely empty history.

    Best effort: guarded because this global lives at a different place in
    different yfinance releases, and the empty-frame checks cover us anyway.
    """
    try:
        yf.config.debug.hide_exceptions = False
    except Exception as exc:  # noqa: BLE001 - non-fatal, we degrade gracefully
        logger.debug("Could not disable yfinance exception hiding: %r", exc)


def _clean_ticker(ticker: str) -> str:
    cleaned = (ticker or "").strip().upper()
    if not cleaned:
        raise UnknownTickerError("Ticker must be a non-empty string.", details={"ticker": ticker})
    return cleaned


def _is_usable_number(value) -> bool:
    """True for a real, finite number - screens out None, NaN and inf."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _to_money(value, ticker: str) -> Decimal:
    """Convert a provider float to a quantised Decimal (see common/MONEY.md)."""
    if not _is_usable_number(value):
        raise ProviderError(
            f"Provider returned a non-numeric price for {ticker}: {value!r}",
            details={"ticker": ticker, "value": repr(value)},
        )
    try:
        return Decimal(str(float(value))).quantize(_MONEY_QUANTUM)
    except (InvalidOperation, ValueError) as exc:
        raise ProviderError(
            f"Provider returned an unrepresentable price for {ticker}: {value!r}",
            details={"ticker": ticker, "value": repr(value)},
        ) from exc


def _http_status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status from an upstream exception, without importing
    the transport library (curl_cffi/requests are implementation details)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"HTTP Error (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _wrap(ticker: str, exc: Exception) -> ProviderError:
    """Classify an upstream exception into our own error hierarchy."""
    if isinstance(exc, ProviderError):
        return exc

    details = {"ticker": ticker, "cause": f"{exc.__class__.__name__}: {exc}"}

    if isinstance(exc, (yf_exceptions.YFTickerMissingError, yf_exceptions.YFTzMissingError)):
        return UnknownTickerError(f"Unknown or delisted ticker: {ticker}.", details=details)
    if isinstance(exc, yf_exceptions.YFPricesMissingError):
        return EmptyHistoryError(f"No price rows available for {ticker}.", details=details)
    if isinstance(exc, yf_exceptions.YFRateLimitError):
        return ProviderError(
            "Market data provider is rate limiting requests.", details=details
        )
    if isinstance(exc, yf_exceptions.YFInvalidPeriodError):
        return ProviderError(f"Invalid history window for {ticker}.", details=details)

    status = _http_status_of(exc)
    if status == 404:
        return UnknownTickerError(f"Unknown or delisted ticker: {ticker}.", details=details)
    if status == 429:
        return ProviderError(
            "Market data provider is rate limiting requests.", details=details
        )

    return ProviderError(
        f"Market data provider failed for {ticker}: {exc.__class__.__name__}.",
        details=details,
    )
