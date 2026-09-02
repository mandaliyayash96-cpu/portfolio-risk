"""
Portfolio writes (architecture rule 1: views never touch the ORM).

Every create/update/delete for Portfolio, Holding and Transaction lands here,
raises common.exceptions.DomainError subclasses on business-rule failures, and
returns model instances or plain dicts.

WHY THIS MODULE FETCHES PRICES
------------------------------
A holding the user just typed in has no stored price, and the risk report
refuses to compute against a ticker it has no history for. Left alone, adding
your first position would produce a dashboard that says `missing_price_data`
until somebody SSHes in and runs `manage.py fetch_prices`. So the write path
warms the new symbols itself.

That fetch is SYNCHRONOUS and it talks to yfinance, which is slow and
occasionally hostile. Four rules keep that from being a liability:

  1. Rows are COMMITTED BEFORE the fetch runs. A timeout, a rate limit or a
     dead network costs you prices, never data.
  2. Only tickers with no stored history are fetched (`get_stored_tickers`), so
     re-importing the same CSV touches the network zero times.
  3. At most MAX_SYNC_FETCH_TICKERS symbols are fetched per request. The rest
     are saved and reported as un-fetched, with the command to run.
  4. A per-ticker failure becomes a WARNING on that row. One bad symbol never
     sinks an import, and nothing here can turn into a 500.

TODO Phase 8: hand this to the Celery worker that marketdata/services.py
already has a TODO for, and return 202 with a job id. The four rules above
make that swap a change of caller, not a rewrite - the write path already does
not depend on the fetch succeeding.

TODO Phase 4 (still open):
    create_portfolio(user, name, base_currency) -> Portfolio
    record_transaction(portfolio_id, ticker, side, quantity, price, timestamp)
        - wrap in transaction.atomic()
        - recompute the affected Holding's quantity + avg_buy_price from the ledger
        - reject a SELL that exceeds the held quantity (raise InvalidInputError)
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from common.exceptions import DomainError, InvalidInputError, ProviderError
from common.models import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    QUANTITY_DECIMAL_PLACES,
    QUANTITY_MAX_DIGITS,
    TICKER_MAX_LENGTH,
)
from marketdata import services as marketdata_services
from marketdata.providers import MarketDataProvider
from marketdata.selectors import get_stored_tickers
from portfolio.brokers import broker_label, fetch_broker_holdings, normalise_broker
from portfolio.models import AssetType, Holding, Portfolio
from portfolio.selectors import get_holding, get_portfolio, serialize_holding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
#: Upload ceiling. Comfortably under Django's DATA_UPLOAD_MAX_MEMORY_SIZE
#: (2.5 MB), so this module's clean 400 is what the user sees rather than the
#: framework's raw SuspiciousOperation.
MAX_CSV_BYTES = 1_048_576

#: Row ceiling. A file over this is rejected WHOLE rather than truncated: a
#: half-imported broker export is worse than a rejected one, and a per-row
#: report 500 entries long is not a report anyone reads.
MAX_CSV_ROWS = 500

#: How many symbols one request may fetch from the provider. Each costs up to
#: two upstream calls at a 20s timeout, so an uncapped import is a request that
#: can block a worker for minutes. Anything past the cap is saved and reported.
MAX_SYNC_FETCH_TICKERS = 25

REQUIRED_COLUMNS = ("ticker", "quantity", "avg_buy_price")
OPTIONAL_COLUMNS = ("buy_date", "asset_type", "sector")

#: DecimalField quanta. Values are rounded to these BEFORE the write so what we
#: echo back is byte-for-byte what was stored - the database rounding silently
#: is how an API starts disagreeing with itself in the fourth decimal.
_MONEY_QUANT = Decimal(1).scaleb(-MONEY_DECIMAL_PLACES)
_QUANTITY_QUANT = Decimal(1).scaleb(-QUANTITY_DECIMAL_PLACES)

#: Accepted buy_date spellings. ISO first because that is what <input type=date>
#: sends; the other three are what spreadsheets export in India.
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")

#: Status values in a per-row import result. "skipped" always carries a reason.
ADDED = "added"
UPDATED = "updated"
SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Field validation
#
# Plain functions raising InvalidInputError, not DRF serializers - there is no
# serializer anywhere in this codebase, and the CSV path needs to validate one
# row and keep going, which is exactly what a serializer makes awkward.
# ---------------------------------------------------------------------------
def _clean_ticker(value) -> str:
    """Non-empty, stripped, upper-cased, and short enough for the column."""
    ticker = str(value if value is not None else "").strip().upper()
    if not ticker:
        raise InvalidInputError("Ticker is required.", details={"field": "ticker"})
    if len(ticker) > TICKER_MAX_LENGTH:
        raise InvalidInputError(
            f"Ticker {ticker!r} is longer than {TICKER_MAX_LENGTH} characters.",
            details={"field": "ticker", "value": ticker},
        )
    if any(character.isspace() for character in ticker):
        raise InvalidInputError(
            f"Ticker {ticker!r} contains a space. Use the yfinance symbol, "
            "e.g. RELIANCE.NS.",
            details={"field": "ticker", "value": ticker},
        )
    return ticker


#: A symbol that carries no exchange suffix and is shaped like a plain equity
#: code: letters and digits, optionally an ampersand (M&M). Deliberately does
#: NOT match anything containing "-", because that is the shape of the pairs
#: yfinance uses for crypto and FX (BTC-USD), where no suffix is expected and a
#: hint would be noise.
_BARE_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&]{0,14}$")

#: What we would suggest. The app is INR-denominated and NSE-focused, so this
#: is the suffix a suffix-less Indian symbol is nearly always missing.
_DEFAULT_SUFFIX = ".NS"


def _suffix_hint(ticker: str) -> str | None:
    """
    Warn about a missing exchange suffix, without ever changing the symbol.

    "TCS" is a real and common mistake: it is a valid yfinance symbol (Tata
    Consultancy's US-listed ADR is not it - "TCS" resolves to Container Store),
    so it fetches, stores prices, and quietly measures the wrong company. That
    is worse than a symbol that fails, because nothing about the dashboard looks
    broken afterwards.

    NOT auto-corrected, and that is the whole design. Rewriting "TCS" to
    "TCS.NS" would be right most of the time and silently wrong the rest, and a
    holding the user did not type is not one they can debug. So the symbol is
    saved exactly as given and the doubt is handed back to them.

    Returns None for anything already carrying a suffix, and for shapes where
    the question does not arise.
    """
    if "." in ticker or not _BARE_SYMBOL.match(ticker):
        return None
    return (
        f"{ticker} has no exchange suffix - did you mean {ticker}{_DEFAULT_SUFFIX}? "
        f"Indian listings need it; leave it as-is if this is a US symbol."
    )


def _unverified_tickers(tickers: list[str]) -> set[str]:
    """
    Which of these the risk report still cannot use, after any fetch has run.

    "Unverified" is defined against STORED HISTORY rather than against whether
    the fetch raised, because that is the condition the report actually applies
    (`risk.services._close_series`). A symbol whose fetch failed and a symbol
    nobody ever fetched are the same problem from the dashboard's side, and
    both should be flagged the moment the holding is saved rather than
    discovered later - which, before the exclusion safeguard landed, meant
    discovering it as a dead dashboard.

    One query for the whole batch, so an import does not pay per row.
    """
    fresh, _known = _new_tickers(tickers)
    return set(fresh)


def _entry_warning(*parts: str | None) -> str | None:
    """
    Fold the hints about one saved row into the single `warning` string the
    API has always returned. None when there is nothing to say.
    """
    said = [part for part in parts if part]
    return " ".join(said) if said else None


def _annotate_saved_rows(
    results: list[dict], price_warnings: dict[str, str], saved: list[str]
) -> None:
    """
    Attach `warning` and `unverified` to every row an import actually wrote.

    Shared by the CSV importer and the broker importer because their reports
    have the same shape and should not be able to drift into describing a saved
    row two different ways. Mutates `results` in place - it is a local list
    being finished off, not a value being passed around.

    SKIPPED rows are left alone: they already carry the `reason` they were
    rejected, and a row that was never written has nothing to be unverified
    about. `unverified` is set to False on them explicitly rather than left
    absent, so a client can read the key on every row without checking.
    """
    unverified = _unverified_tickers(saved) if saved else set()
    for entry in results:
        if entry["status"] == SKIPPED:
            entry["unverified"] = False
            continue
        ticker = entry["ticker"]
        entry["warning"] = _entry_warning(
            _suffix_hint(ticker), price_warnings.get(ticker)
        )
        entry["unverified"] = ticker in unverified


def _positive_decimal(value, *, field: str, quant: Decimal, max_digits: int) -> Decimal:
    """
    A finite Decimal strictly greater than zero, quantised to its column.

    `str(value)` before Decimal() on purpose: float('nan') and float('inf') are
    perfectly good floats, and both would reach a DecimalField as something no
    comparison is ever true about.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        raise InvalidInputError(f"{field} is required.", details={"field": field})

    try:
        parsed = Decimal(raw)
        if not parsed.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ArithmeticError, ValueError, TypeError) as exc:
        raise InvalidInputError(
            f"{field} must be a number, got {raw!r}.",
            details={"field": field, "value": raw},
        ) from exc

    if parsed <= 0:
        raise InvalidInputError(
            f"{field} must be greater than zero, got {raw}.",
            details={"field": field, "value": raw},
        )

    quantised = parsed.quantize(quant, rounding=ROUND_HALF_UP)
    if quantised <= 0:
        # Survived the > 0 check but rounds away to nothing at the column's
        # precision. Storing it would silently make it zero.
        raise InvalidInputError(
            f"{field} {raw} is smaller than this field can store "
            f"({quant} is the smallest step).",
            details={"field": field, "value": raw},
        )

    limit = Decimal(10) ** (max_digits - abs(quant.as_tuple().exponent))
    if quantised >= limit:
        raise InvalidInputError(
            f"{field} {raw} is too large for this field (must be under {limit}).",
            details={"field": field, "value": raw},
        )
    return quantised


def _parse_buy_date(value) -> date | None:
    """
    A date, or None when the caller left it out.

    None is a legal ANSWER here but not a legal COLUMN value - Holding.buy_date
    is NOT NULL - so `_validated_fields` substitutes today. The two steps are
    separate because "you sent nothing" and "you sent nonsense" are different
    outcomes, and only the second is an error.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = str(value).strip()
    if not raw:
        return None

    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue

    raise InvalidInputError(
        f"buy_date {raw!r} is not a date I can read. Use YYYY-MM-DD.",
        details={"field": "buy_date", "value": raw, "formats": list(_DATE_FORMATS)},
    )


def _clean_asset_type(value) -> str:
    """One of AssetType, defaulting to EQUITY. Spaces and case are forgiven."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return AssetType.EQUITY

    normalised = raw.upper().replace(" ", "_").replace("-", "_")
    valid = [choice.value for choice in AssetType]
    if normalised not in valid:
        raise InvalidInputError(
            f"asset_type {raw!r} is not one of: {', '.join(valid)}.",
            details={"field": "asset_type", "value": raw, "supported": valid},
        )
    return normalised


def _clean_sector(value) -> str:
    """Free text, trimmed. Blank is the model's own default, not an error."""
    sector = str(value if value is not None else "").strip()
    max_length = Holding._meta.get_field("sector").max_length
    if len(sector) > max_length:
        raise InvalidInputError(
            f"sector is longer than {max_length} characters.",
            details={"field": "sector", "value": sector[:80]},
        )
    return sector


def _validated_fields(
    *,
    ticker,
    quantity,
    avg_buy_price,
    buy_date=None,
    asset_type=None,
    sector=None,
) -> dict:
    """
    Every field checked, or the first failure raised.

    One function so the manual form and the CSV importer cannot drift into
    validating the same holding by two different sets of rules.
    """
    return {
        "ticker": _clean_ticker(ticker),
        "quantity": _positive_decimal(
            quantity,
            field="quantity",
            quant=_QUANTITY_QUANT,
            max_digits=QUANTITY_MAX_DIGITS,
        ),
        "avg_buy_price": _positive_decimal(
            avg_buy_price,
            field="avg_buy_price",
            quant=_MONEY_QUANT,
            max_digits=MONEY_MAX_DIGITS,
        ),
        # NOT NULL column, so an omitted date becomes today rather than a 500.
        "buy_date": _parse_buy_date(buy_date) or timezone.localdate(),
        "asset_type": _clean_asset_type(asset_type),
        "sector": _clean_sector(sector),
    }


# ---------------------------------------------------------------------------
# Price warm-up
# ---------------------------------------------------------------------------
def _new_tickers(tickers: list[str]) -> tuple[list[str], list[str]]:
    """
    Split symbols into (needs fetching, already has stored history).

    "Already stored" is judged on PriceHistory, not PriceSnapshot, because the
    risk report is built from the close SERIES - a lone live price would leave
    the dashboard just as stuck while looking fetched.
    """
    stored = set(get_stored_tickers())
    fresh = [ticker for ticker in tickers if ticker not in stored]
    known = [ticker for ticker in tickers if ticker in stored]
    return fresh, known


def fetch_prices_for(
    tickers: list[str], *, provider: MarketDataProvider | None = None
) -> dict[str, str]:
    """
    Warm the price tables for newly added symbols. Returns {ticker: warning}.

    Only symbols with no stored history are fetched, and only up to
    MAX_SYNC_FETCH_TICKERS of them - see this module's docstring for why both
    limits exist.

    NEVER RAISES. Every failure mode - one dead symbol, a rate-limited feed, a
    provider that will not even construct - comes back as a warning string
    against the tickers it affected, because the holdings are already committed
    by the time this runs and "your data is saved but has no prices yet" is not
    an error the write should be reported as.

    Args:
        tickers: symbols to consider, already upper-cased.
        provider: override, mainly for tests. Defaults to the configured one.
    """
    warnings: dict[str, str] = {}
    if not tickers:
        return warnings

    fresh, _known = _new_tickers(tickers)
    if not fresh:
        return warnings

    if len(fresh) > MAX_SYNC_FETCH_TICKERS:
        for ticker in fresh[MAX_SYNC_FETCH_TICKERS:]:
            warnings[ticker] = (
                f"Saved, but prices were not fetched: this request already "
                f"fetched {MAX_SYNC_FETCH_TICKERS} new symbols. Run "
                "`python manage.py fetch_prices` to pick up the rest."
            )
        fresh = fresh[:MAX_SYNC_FETCH_TICKERS]

    try:
        # fetch_live collects per-ticker failures rather than raising them; a
        # provider that cannot be built at all still raises, and is caught below.
        live = marketdata_services.fetch_live(fresh, provider=provider)
        for ticker, message in live.errors.items():
            warnings[ticker] = f"Saved, but no live price: {message}"
    except ProviderError as exc:
        logger.warning("Live price warm-up unavailable: %s", exc.message)
        for ticker in fresh:
            warnings[ticker] = f"Saved, but the price feed is unavailable: {exc.message}"
        return warnings
    except Exception:
        # Broad by design. The provider contract says only ProviderError
        # escapes; if a transport library ever breaks that promise, the cost
        # must be a warning on a saved row, not a 500 on a successful write.
        logger.exception("Unexpected failure warming live prices for %s", fresh)
        for ticker in fresh:
            warnings[ticker] = "Saved, but the price feed failed unexpectedly."
        return warnings

    for ticker in fresh:
        try:
            result = marketdata_services.fetch_history(ticker, provider=provider)
        except ProviderError as exc:
            # History is the one the risk report actually needs, so its message
            # wins over any live-price warning already recorded for this symbol.
            warnings[ticker] = f"Saved, but no price history: {exc.message}"
            continue
        except Exception:
            logger.exception("Unexpected failure fetching history for %s", ticker)
            warnings[ticker] = "Saved, but fetching price history failed unexpectedly."
            continue

        if result.rows == 0:
            warnings[ticker] = "Saved, but the provider returned no price history."

    return warnings


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _upsert(portfolio: Portfolio, fields: dict) -> tuple[Holding, bool]:
    """
    Create the position, or REPLACE the existing one for this ticker.

    Replace rather than accumulate, and that is a real decision: it makes
    re-importing the same CSV idempotent, and it matches Holding's own
    docstring, where a holding is the CURRENT blended position and Transaction
    is the ledger that would justify adding two lots together. Once
    `record_transaction` exists, accumulation belongs there.

    The (portfolio, ticker) key is the model's own unique constraint, so this
    cannot create the duplicate the database would reject anyway.

    `fields` is read, never mutated: the ticker is the KEY and the rest are the
    defaults, so splitting them here means callers can hand over the same dict
    they validated and still read it afterwards.
    """
    defaults = {key: value for key, value in fields.items() if key != "ticker"}
    return Holding.objects.update_or_create(
        portfolio=portfolio, ticker=fields["ticker"], defaults=defaults
    )


def add_holding(
    portfolio_id: int,
    ticker,
    quantity,
    avg_buy_price,
    buy_date=None,
    asset_type=None,
    sector=None,
    *,
    fetch_prices: bool = True,
    provider: MarketDataProvider | None = None,
) -> dict:
    """
    Add one position by hand, or replace the one already held in that ticker.

    Args:
        portfolio_id: which portfolio, from the URL.
        ticker: yfinance symbol. Stripped and upper-cased before storage, so
            "reliance.ns" and "RELIANCE.NS " are the same position - and the
            same one `marketdata` stores prices under.
        quantity: units held. Must be > 0.
        avg_buy_price: blended cost per unit, in the portfolio's currency. > 0.
        buy_date: YYYY-MM-DD (or a date). Defaults to today when omitted.
        asset_type: an AssetType value. Defaults to EQUITY.
        sector: free text, may be blank.
        fetch_prices: warm the price tables for a NEW ticker. Left True in
            normal use; the validation tests switch it off to stay offline.
        provider: market data provider override, mainly for tests.

    Returns:
        The serialised holding plus three keys the caller needs and the row
        itself does not carry:
            "created" - True if this added a position, False if it replaced one
            "warning" - None, or what is doubtful about this symbol: a missing
                        exchange suffix, prices that could not be fetched, or
                        both, in one string
            "unverified" - True when nothing in the price tables can value this
                        symbol yet. The row IS saved either way; this is the
                        flag that lets the UI say so immediately rather than
                        letting the user find out from the risk report later

    Raises:
        NotFoundError (404):    no such portfolio.
        InvalidInputError (400): any field that failed the rules above.

    Never raises for a price fetch that fails - see `fetch_prices_for`.
    """
    portfolio = get_portfolio(portfolio_id)  # 404s on a bad id
    fields = _validated_fields(
        ticker=ticker,
        quantity=quantity,
        avg_buy_price=avg_buy_price,
        buy_date=buy_date,
        asset_type=asset_type,
        sector=sector,
    )
    symbol = fields["ticker"]

    with transaction.atomic():
        holding, created = _upsert(portfolio, fields)

    logger.info(
        "%s holding %s on portfolio %s (%s x %s)",
        "Created" if created else "Updated",
        symbol,
        portfolio_id,
        fields["quantity"],
        fields["avg_buy_price"],
    )

    # Committed. Only now does anything slow or fallible happen.
    warnings = fetch_prices_for([symbol], provider=provider) if fetch_prices else {}

    payload = serialize_holding(holding)
    payload["created"] = created
    # Two independent doubts about this symbol, folded into one line: it may be
    # missing an exchange suffix, and it may have no prices behind it. Either
    # can be true without the other.
    payload["warning"] = _entry_warning(_suffix_hint(symbol), warnings.get(symbol))
    payload["unverified"] = symbol in _unverified_tickers([symbol])
    return payload


def delete_holding(holding_id: int, *, portfolio_id: int | None = None) -> dict:
    """
    Remove one position.

    Stored prices for the ticker are deliberately left behind: they are not
    this portfolio's property, another portfolio may hold the same symbol, and
    re-adding it should not have to hit the network again.

    Args:
        holding_id: primary key of the row to delete.
        portfolio_id: when given, the holding must belong to it or this 404s.
            The endpoint always passes it - see `get_holding`.

    Returns:
        {"id", "ticker", "deleted": True} - enough for the UI to confirm what
        went, after the row itself no longer exists.

    Raises:
        NotFoundError (404): no such holding in that portfolio.
    """
    holding = get_holding(holding_id, portfolio_id=portfolio_id)
    ticker = holding.ticker
    owning_portfolio = holding.portfolio_id

    holding.delete()
    logger.info("Deleted holding %s (%s) from portfolio %s", holding_id, ticker, owning_portfolio)

    return {
        "id": holding_id,
        "portfolio_id": owning_portfolio,
        "ticker": ticker,
        "deleted": True,
    }


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------
def _expected_columns_message() -> str:
    """The one sentence every header failure ends with."""
    return (
        f"Expected columns: {', '.join(REQUIRED_COLUMNS)} "
        f"(optional: {', '.join(OPTIONAL_COLUMNS)})."
    )


def _read_upload(file) -> str:
    """
    Turn an uploaded file into text, or explain why it is not one.

    Checks in escalating cost order: present, named .csv, small enough,
    decodable. The size check reads `.size` where the upload object has one and
    falls back to the length of the bytes, because an InMemoryUploadedFile, a
    TemporaryUploadedFile and a plain BytesIO in a test do not all agree on
    which attributes exist.
    """
    if file is None:
        raise InvalidInputError(
            "No file uploaded. Attach a CSV as the `file` field of a multipart "
            f"form. {_expected_columns_message()}",
            details={"field": "file"},
        )

    name = str(getattr(file, "name", "") or "")
    if name and not name.lower().endswith(".csv"):
        raise InvalidInputError(
            f"{name!r} is not a .csv file. Export your holdings as CSV and "
            "upload that.",
            details={"field": "file", "filename": name},
        )

    declared_size = getattr(file, "size", None)
    if isinstance(declared_size, int) and declared_size > MAX_CSV_BYTES:
        raise InvalidInputError(
            f"That file is {declared_size} bytes; the limit is {MAX_CSV_BYTES} "
            f"({MAX_CSV_BYTES // 1024} KB).",
            details={"field": "file", "size": declared_size, "limit": MAX_CSV_BYTES},
        )

    raw = file.read()
    if isinstance(raw, str):  # a StringIO in a test
        raw = raw.encode("utf-8")
    if len(raw) > MAX_CSV_BYTES:
        raise InvalidInputError(
            f"That file is {len(raw)} bytes; the limit is {MAX_CSV_BYTES} "
            f"({MAX_CSV_BYTES // 1024} KB).",
            details={"field": "file", "size": len(raw), "limit": MAX_CSV_BYTES},
        )
    if not raw.strip():
        raise InvalidInputError(
            f"That file is empty. {_expected_columns_message()}",
            details={"field": "file"},
        )

    try:
        # utf-8-sig, not utf-8: Excel on Windows writes a BOM, and without this
        # the first header reads as "﻿ticker" and every import fails on a
        # missing column the user can plainly see in their spreadsheet.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidInputError(
            "That file is not text - it could not be read as UTF-8. Save it as "
            "CSV (UTF-8) rather than .xlsx and try again.",
            details={"field": "file"},
        ) from exc


def _normalise_header(name) -> str:
    """'Avg Buy Price' -> 'avg_buy_price'. Header case is not the user's job."""
    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _open_reader(text: str) -> csv.DictReader:
    """A DictReader with normalised headers, or a 400 naming what was missing."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise InvalidInputError(
            f"That file has no header row. {_expected_columns_message()}",
            details={"field": "file"},
        )

    reader.fieldnames = [_normalise_header(name) for name in reader.fieldnames]
    missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
    if missing:
        raise InvalidInputError(
            f"CSV is missing required column(s): {', '.join(missing)}. "
            f"{_expected_columns_message()}",
            details={
                "field": "file",
                "missing": missing,
                "found": [name for name in reader.fieldnames if name],
                "required": list(REQUIRED_COLUMNS),
                "optional": list(OPTIONAL_COLUMNS),
            },
        )
    return reader


def _is_blank_row(row: dict) -> bool:
    """Trailing newlines and spacer rows are not errors, they are nothing."""
    return all(not str(value or "").strip() for value in row.values())


def import_holdings_csv(
    portfolio_id: int,
    file,
    *,
    fetch_prices: bool = True,
    provider: MarketDataProvider | None = None,
) -> dict:
    """
    Bulk-load holdings from an uploaded CSV.

    PARTIAL SUCCESS IS THE CONTRACT
    -------------------------------
    Valid rows are committed and invalid ones are reported; a single bad row
    never blocks the other 49. The alternative - validate everything, commit
    nothing unless all of it passes - makes a 200-row broker export unusable
    because one line has a blank price. So the response is a REPORT, not a
    yes/no, and the caller is expected to show it.

    The valid rows do go in as ONE transaction, so the outcome is always
    "everything I accepted, or nothing" - never a half-written batch from a
    database error mid-loop.

    FILE-LEVEL problems are different and DO reject the whole upload, because
    no row in them can be trusted: not a CSV, too big, too many rows, wrong or
    missing headers.

    Args:
        portfolio_id: which portfolio, from the URL.
        file: an UploadedFile (or any object with .read()).
        fetch_prices: warm prices for newly seen tickers. Tests switch it off.
        provider: market data provider override, mainly for tests.

    Returns:
        dict with keys:
            portfolio_id
            total_rows  - data rows seen, blanks excluded
            added / updated / skipped  - counts
            results     - one entry per row:
                          {row, ticker, status, reason, warning, unverified}
                          `row` is the line number in the file, so it matches
                          what the user sees in their spreadsheet.
                          `warning` names a missing exchange suffix and/or a
                          price fetch that failed; `unverified` is True when
                          the risk report still has no history to value that
                          symbol with. Both are False/None on a skipped row.
            price_fetch - {"attempted": bool, "warnings": {ticker: message}}

    Raises:
        NotFoundError (404):     no such portfolio.
        InvalidInputError (400): any of the file-level problems above.
    """
    portfolio = get_portfolio(portfolio_id)  # 404s before we read a single byte
    reader = _open_reader(_read_upload(file))

    results: list[dict] = []
    accepted: list[dict] = []
    seen: dict[str, int] = {}  # ticker -> the line that claimed it
    total_rows = 0

    for row in reader:
        if _is_blank_row(row):
            continue

        total_rows += 1
        line = reader.line_num
        if total_rows > MAX_CSV_ROWS:
            raise InvalidInputError(
                f"That file has more than {MAX_CSV_ROWS} rows. Split it and "
                "import the parts.",
                details={"field": "file", "limit": MAX_CSV_ROWS},
            )

        # DictReader hands back None for a short row and a list for a long one.
        # Neither is a value any validator should have to think about.
        cleaned = {
            key: (value if isinstance(value, str) else "")
            for key, value in row.items()
            if key in REQUIRED_COLUMNS or key in OPTIONAL_COLUMNS
        }

        try:
            fields = _validated_fields(
                ticker=cleaned.get("ticker"),
                quantity=cleaned.get("quantity"),
                avg_buy_price=cleaned.get("avg_buy_price"),
                buy_date=cleaned.get("buy_date"),
                asset_type=cleaned.get("asset_type"),
                sector=cleaned.get("sector"),
            )
        except DomainError as exc:
            results.append(
                {
                    "row": line,
                    "ticker": str(cleaned.get("ticker") or "").strip().upper() or None,
                    "status": SKIPPED,
                    "reason": exc.message,
                    "warning": None,
                }
            )
            continue

        symbol = fields["ticker"]
        if symbol in seen:
            # First occurrence wins. Applying both would make the import's
            # outcome depend on row order for no stated reason, and silently
            # dropping the first would hide a real mistake in their file.
            results.append(
                {
                    "row": line,
                    "ticker": symbol,
                    "status": SKIPPED,
                    "reason": (
                        f"Duplicate ticker in this file - row {seen[symbol]} "
                        "already sets this position."
                    ),
                    "warning": None,
                }
            )
            continue

        seen[symbol] = line
        accepted.append({"line": line, "fields": fields})

    # One transaction for every row we accepted: a database failure halfway
    # through must not leave the user with a partly-loaded portfolio and a
    # report claiming otherwise.
    with transaction.atomic():
        for entry in accepted:
            _holding, created = _upsert(portfolio, entry["fields"])
            results.append(
                {
                    "row": entry["line"],
                    "ticker": entry["fields"]["ticker"],
                    "status": ADDED if created else UPDATED,
                    "reason": None,
                    "warning": None,
                }
            )

    results.sort(key=lambda entry: entry["row"])

    # Committed. Prices are warmed after, and only for what actually landed.
    warnings: dict[str, str] = {}
    saved = [entry["fields"]["ticker"] for entry in accepted]
    if fetch_prices and accepted:
        warnings = fetch_prices_for(saved, provider=provider)
    _annotate_saved_rows(results, warnings, saved)

    counts = {ADDED: 0, UPDATED: 0, SKIPPED: 0}
    for entry in results:
        counts[entry["status"]] += 1

    logger.info(
        "CSV import into portfolio %s: %s added, %s updated, %s skipped.",
        portfolio_id,
        counts[ADDED],
        counts[UPDATED],
        counts[SKIPPED],
    )

    return {
        "portfolio_id": portfolio.pk,
        "total_rows": total_rows,
        "added": counts[ADDED],
        "updated": counts[UPDATED],
        "skipped": counts[SKIPPED],
        "results": results,
        "price_fetch": {
            "attempted": bool(fetch_prices and accepted),
            "warnings": warnings,
        },
    }


# ---------------------------------------------------------------------------
# Broker aggregation (SIMULATED)
#
# The positions come from `portfolio.brokers`, which is a preset table and not
# a broker - read that module's docstring before believing anything else here.
# Everything AFTER the fetch is the real write path: the same validation, the
# same upsert, the same price warm-up, the same one-transaction rule and the
# same per-row report as a CSV import.
# ---------------------------------------------------------------------------
def import_broker_holdings(
    portfolio_id: int,
    broker,
    *,
    fetch_prices: bool = True,
    provider: MarketDataProvider | None = None,
) -> dict:
    """
    Pull one broker's holdings into the portfolio. The FETCH is simulated.

    WHAT IS REAL AND WHAT IS NOT
    ----------------------------
    Not real: where the rows come from. `fetch_broker_holdings` returns a
    hardcoded sample per broker and touches no network - it is the seam a live
    Kite Connect / Upstox integration would replace.

    Real: everything this function then does with them. Rows go through
    `_validated_fields`, land through `_upsert`, and have their prices warmed by
    `fetch_prices_for` - the same three functions `add_holding` and
    `import_holdings_csv` use, so a broker import cannot accept a position the
    manual form would have rejected, and cannot store one in a different shape.

    WHY IT UPSERTS, AND WHY THAT IS THE FEATURE
    -------------------------------------------
    Aggregation means one consolidated portfolio, not four stacked copies.
    `_upsert` keys on (portfolio, ticker), so importing Zerodha and then ICICI
    Direct - both of which report HDFCBANK.NS in the sample data - leaves ONE
    HDFCBANK position, and re-importing the same broker twice changes nothing.
    That is the same idempotence re-importing a CSV has, for the same reason.

    Args:
        portfolio_id: which portfolio, from the URL.
        broker: one of `brokers.SUPPORTED_BROKERS`.
        fetch_prices: warm prices for newly seen tickers. Tests switch it off.
        provider: market data provider override, mainly for tests.

    Returns:
        The CSV importer's report shape, so the dashboard renders both with one
        component, plus four keys of its own:
            broker        - the canonical slug
            broker_label  - its display name, e.g. "ICICI Direct"
            simulated     - always True. The response says so out loud rather
                            than leaving the frontend to remember it.
            holdings      - what the broker "reported", verbatim, before this
                            portfolio was touched. The report says what was
                            DONE with each row; this says what arrived, so a
                            client can show the statement it imported from
                            without re-deriving it from the outcome.
        `row` in each result is the position's place in the broker's statement
        (1-based), which is the nearest honest equivalent of a CSV line number.
        Each result also carries `warning` and `unverified`, exactly as a CSV
        row does - a sample symbol with no stored prices is flagged the same
        way a hand-typed one is.

    Raises:
        NotFoundError (404):     no such portfolio.
        InvalidInputError (400): unknown broker.

    Never raises for a price fetch that fails - see `fetch_prices_for`.
    """
    # Before the portfolio lookup: an unknown broker is the caller's mistake and
    # nothing should be read or written on the strength of it.
    slug = normalise_broker(broker)
    portfolio = get_portfolio(portfolio_id)  # 404s on a bad id
    rows = fetch_broker_holdings(slug)  # MOCK: preset sample, no network.

    results: list[dict] = []
    accepted: list[dict] = []
    seen: dict[str, int] = {}  # ticker -> the position that claimed it

    for position, row in enumerate(rows, start=1):
        try:
            fields = _validated_fields(
                ticker=row.get("ticker"),
                quantity=row.get("quantity"),
                avg_buy_price=row.get("avg_buy_price"),
                buy_date=row.get("buy_date"),
                asset_type=row.get("asset_type"),
                sector=row.get("sector"),
            )
        except DomainError as exc:
            # Only reachable if the preset table itself is wrong, so it is
            # logged as OUR bug - but it is still reported per-row rather than
            # raised, because one bad sample must not sink the other four.
            logger.warning("Sample data for broker %s has a bad row: %s", slug, exc.message)
            results.append(
                {
                    "row": position,
                    "ticker": str(row.get("ticker") or "").strip().upper() or None,
                    "status": SKIPPED,
                    "reason": exc.message,
                    "warning": None,
                }
            )
            continue

        symbol = fields["ticker"]
        if symbol in seen:
            # Same rule as the CSV importer: first occurrence wins, so the
            # outcome does not depend on the order of a list we control.
            results.append(
                {
                    "row": position,
                    "ticker": symbol,
                    "status": SKIPPED,
                    "reason": (
                        f"Duplicate ticker in this broker's holdings - position "
                        f"{seen[symbol]} already sets it."
                    ),
                    "warning": None,
                }
            )
            continue

        seen[symbol] = position
        accepted.append({"position": position, "fields": fields})

    # One transaction for the whole broker, exactly as the CSV path does: a
    # database failure halfway through must not leave half a broker imported.
    with transaction.atomic():
        for entry in accepted:
            _holding, created = _upsert(portfolio, entry["fields"])
            results.append(
                {
                    "row": entry["position"],
                    "ticker": entry["fields"]["ticker"],
                    "status": ADDED if created else UPDATED,
                    "reason": None,
                    "warning": None,
                }
            )

    results.sort(key=lambda entry: entry["row"])

    # Committed. Prices are warmed after, and only for what actually landed.
    warnings: dict[str, str] = {}
    saved = [entry["fields"]["ticker"] for entry in accepted]
    if fetch_prices and accepted:
        warnings = fetch_prices_for(saved, provider=provider)
    _annotate_saved_rows(results, warnings, saved)

    counts = {ADDED: 0, UPDATED: 0, SKIPPED: 0}
    for entry in results:
        counts[entry["status"]] += 1

    logger.info(
        "Simulated %s import into portfolio %s: %s added, %s updated, %s skipped.",
        slug,
        portfolio_id,
        counts[ADDED],
        counts[UPDATED],
        counts[SKIPPED],
    )

    return {
        "portfolio_id": portfolio.pk,
        "broker": slug,
        "broker_label": broker_label(slug),
        # Said in the payload, not just in the UI copy: any client reading this
        # response should be able to tell that the source was a sample.
        "simulated": True,
        # The statement as it arrived. `rows` is already this request's private
        # deep copy (see fetch_broker_holdings), and nothing above mutates it,
        # so it can be handed back as-is.
        "holdings": rows,
        "total_rows": len(rows),
        "added": counts[ADDED],
        "updated": counts[UPDATED],
        "skipped": counts[SKIPPED],
        "results": results,
        "price_fetch": {
            "attempted": bool(fetch_prices and accepted),
            "warnings": warnings,
        },
    }
