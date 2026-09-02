"""
Simulated multi-platform broker aggregation.

EVERYTHING IN THIS MODULE IS FAKE, AND THAT IS THE POINT
--------------------------------------------------------
The product requirement is "pull holdings from several brokers into one
consolidated portfolio". The half this module actually implements is the
CONSOLIDATION half: what happens to four brokers' positions once they land in
one portfolio, and what the risk report says afterwards. The FETCHING half is
stubbed with the preset tables below.

No network call is made from here. No credential is asked for, stored or
transmitted. Nothing here talks to Zerodha, Groww, Upstox or ICICI Direct, and
none of those companies is affiliated with this application - their names are
labels on a demo, and every position under them is invented.

Kept in its own module rather than as a dict at the top of `services.py` so
that the boundary is a FILE boundary: when a real integration exists,
`fetch_broker_holdings` below grows an implementation and everything around it
- the service, the view, the payment gate, the tests - is unchanged. That
function is the only thing the rest of the codebase imports.

WHY THE SAMPLE PORTFOLIOS OVERLAP
---------------------------------
HDFCBANK.NS is in both the Zerodha and the ICICI Direct table, and INFY.NS is
in both Zerodha and Groww. That is deliberate: holding the same scrip at two
brokers is the ordinary case aggregation exists to solve, and importing both
has to leave ONE position rather than two. It does, because this import path
upserts on ticker like every other write - see
`portfolio.services.import_broker_holdings`.
"""

from copy import deepcopy

from common.exceptions import InvalidInputError

#: The slugs the endpoint accepts, in the order the UI shows them.
#:
#: Each entry is {label, holdings}. `label` is the display name the API echoes
#: back, so the frontend does not have to keep its own copy of it. `holdings`
#: are rows in exactly the shape `_validated_fields` takes - the same keys a
#: CSV row or the manual form supplies - so the import path needs no special
#: case for them and no second set of validation rules.
#:
#: MOCK: replace with real broker API (e.g. Zerodha Kite Connect) in
#: production. A live version of this table would come from an OAuth-authorised
#: call per broker - Kite Connect's /portfolio/holdings, Upstox's
#: /portfolio/long-term-holdings, and so on - mapped into these same keys. The
#: tickers already carry the .NS suffix yfinance wants, which is one of the two
#: real mapping problems a live integration has; the other is that brokers
#: report average cost net of corporate actions, and we would have to decide
#: whether to trust their number or recompute it from the trade ledger.
BROKER_SAMPLES = {
    "zerodha": {
        "label": "Zerodha",
        "holdings": [
            # SIMULATED. Real NSE symbols, invented quantities and costs.
            {
                "ticker": "RELIANCE.NS",
                "quantity": "45",
                "avg_buy_price": "1387.60",
                "buy_date": "2025-06-18",
                "asset_type": "EQUITY",
                "sector": "Energy",
            },
            {
                "ticker": "HDFCBANK.NS",
                "quantity": "60",
                "avg_buy_price": "1642.25",
                "buy_date": "2025-04-02",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "INFY.NS",
                "quantity": "80",
                "avg_buy_price": "1518.40",
                "buy_date": "2025-09-11",
                "asset_type": "EQUITY",
                "sector": "Information Technology",
            },
            {
                "ticker": "ITC.NS",
                "quantity": "150",
                "avg_buy_price": "412.85",
                "buy_date": "2025-11-27",
                "asset_type": "EQUITY",
                "sector": "Consumer Staples",
            },
            {
                "ticker": "TATAMOTORS.NS",
                "quantity": "70",
                "avg_buy_price": "938.15",
                "buy_date": "2026-01-09",
                "asset_type": "EQUITY",
                "sector": "Consumer Discretionary",
            },
        ],
    },
    "groww": {
        "label": "Groww",
        "holdings": [
            # SIMULATED.
            {
                "ticker": "TCS.NS",
                "quantity": "25",
                "avg_buy_price": "3418.70",
                "buy_date": "2025-07-23",
                "asset_type": "EQUITY",
                "sector": "Information Technology",
            },
            {
                "ticker": "SBIN.NS",
                "quantity": "120",
                "avg_buy_price": "798.30",
                "buy_date": "2025-10-14",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                # Held here AND at Zerodha. Importing both leaves one position,
                # carrying whichever broker was imported last.
                "ticker": "INFY.NS",
                "quantity": "30",
                "avg_buy_price": "1602.10",
                "buy_date": "2026-02-05",
                "asset_type": "EQUITY",
                "sector": "Information Technology",
            },
            {
                "ticker": "BAJFINANCE.NS",
                "quantity": "18",
                "avg_buy_price": "6845.00",
                "buy_date": "2025-12-01",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "NIFTYBEES.NS",
                "quantity": "200",
                "avg_buy_price": "268.45",
                "buy_date": "2025-08-19",
                "asset_type": "ETF",
                "sector": "Index",
            },
        ],
    },
    "upstox": {
        "label": "Upstox",
        "holdings": [
            # SIMULATED.
            {
                "ticker": "ICICIBANK.NS",
                "quantity": "85",
                "avg_buy_price": "1174.90",
                "buy_date": "2025-05-29",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "LT.NS",
                "quantity": "22",
                "avg_buy_price": "3492.55",
                "buy_date": "2025-09-03",
                "asset_type": "EQUITY",
                "sector": "Industrials",
            },
            {
                "ticker": "MARUTI.NS",
                "quantity": "9",
                "avg_buy_price": "11840.00",
                "buy_date": "2026-03-17",
                "asset_type": "EQUITY",
                "sector": "Consumer Discretionary",
            },
            {
                "ticker": "SUNPHARMA.NS",
                "quantity": "55",
                "avg_buy_price": "1721.35",
                "buy_date": "2025-11-06",
                "asset_type": "EQUITY",
                "sector": "Healthcare",
            },
            {
                "ticker": "WIPRO.NS",
                "quantity": "140",
                "avg_buy_price": "289.60",
                "buy_date": "2026-04-21",
                "asset_type": "EQUITY",
                "sector": "Information Technology",
            },
        ],
    },
    "icici": {
        "label": "ICICI Direct",
        "holdings": [
            # SIMULATED.
            {
                # The overlap with Zerodha, above.
                "ticker": "HDFCBANK.NS",
                "quantity": "40",
                "avg_buy_price": "1588.75",
                "buy_date": "2025-03-12",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "KOTAKBANK.NS",
                "quantity": "48",
                "avg_buy_price": "1795.20",
                "buy_date": "2025-08-07",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "AXISBANK.NS",
                "quantity": "95",
                "avg_buy_price": "1082.45",
                "buy_date": "2026-01-30",
                "asset_type": "EQUITY",
                "sector": "Financials",
            },
            {
                "ticker": "TITAN.NS",
                "quantity": "26",
                "avg_buy_price": "3315.80",
                "buy_date": "2025-10-22",
                "asset_type": "EQUITY",
                "sector": "Consumer Discretionary",
            },
            {
                "ticker": "NESTLEIND.NS",
                "quantity": "35",
                "avg_buy_price": "2248.90",
                "buy_date": "2026-02-18",
                "asset_type": "EQUITY",
                "sector": "Consumer Staples",
            },
            {
                "ticker": "ONGC.NS",
                "quantity": "210",
                "avg_buy_price": "247.15",
                "buy_date": "2025-12-15",
                "asset_type": "EQUITY",
                "sector": "Energy",
            },
        ],
    },
}

#: Slugs in display order, for error messages and for anything that wants to
#: enumerate the supported brokers without reaching into the table.
SUPPORTED_BROKERS = tuple(BROKER_SAMPLES)


def broker_label(broker: str) -> str:
    """The display name for a slug `fetch_broker_holdings` has already accepted."""
    return BROKER_SAMPLES[broker]["label"]


def normalise_broker(broker) -> str:
    """
    The canonical slug for whatever the request body said.

    Case and surrounding space are forgiven because this arrives from JSON a
    human may have typed. Anything else is a 400 - and it is raised HERE, before
    a portfolio is looked up or a row is written, so an unknown broker cannot
    half-import.

    Raises:
        InvalidInputError (400): unknown or missing broker.
    """
    slug = str(broker if broker is not None else "").strip().lower()
    if slug not in BROKER_SAMPLES:
        supported = ", ".join(SUPPORTED_BROKERS)
        raise InvalidInputError(
            f"Unknown broker {str(broker or '').strip()!r}. Supported: {supported}.",
            details={"field": "broker", "value": broker, "supported": list(SUPPORTED_BROKERS)},
        )
    return slug


def fetch_broker_holdings(broker) -> list[dict]:
    """
    The positions a broker reports for this user. SIMULATED - see the module
    docstring.

    MOCK: replace with real broker API (e.g. Zerodha Kite Connect) in
    production. This is the seam, and it is the whole seam: a live version
    authenticates the user against the broker, calls their holdings endpoint,
    and maps the response into the same list of dicts this returns. The
    signature does not change, the callers do not change, and the ₹9 gate in
    front of it does not change.

    Args:
        broker: one of SUPPORTED_BROKERS, in any case.

    Returns:
        A list of holding dicts keyed exactly as `_validated_fields` expects.
        DEEP-COPIED, because BROKER_SAMPLES is module-level state that lives for
        the life of the worker: a caller that edited a row in place would
        silently rewrite what every later import of that broker returns.

    Raises:
        InvalidInputError (400): unknown or missing broker.
    """
    return deepcopy(BROKER_SAMPLES[normalise_broker(broker)]["holdings"])
