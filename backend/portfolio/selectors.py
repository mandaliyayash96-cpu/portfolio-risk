"""
Portfolio reads (architecture rule 1: views never touch the ORM).

Query functions return querysets, model instances or plain dicts - never
Response objects, and never raw ORM access from a view or a command.
"""

from django.db.models import QuerySet

from common.exceptions import NotFoundError
from portfolio.models import Holding, Portfolio


def get_all_held_tickers() -> list[str]:
    """
    Every distinct ticker held in any portfolio, alphabetically.

    This is the fetch list for market data: `manage.py fetch_prices` today,
    the Celery poll task from Phase 6 onward.
    """
    return list(
        Holding.objects.order_by("ticker").values_list("ticker", flat=True).distinct()
    )


def get_portfolio(portfolio_id: int) -> Portfolio:
    """
    One portfolio by id.

    Raises:
        NotFoundError: no such portfolio. The exception handler renders this as
            a 404 {"code": "not_found"} envelope, so a bad id in the URL never
            reaches the client as a bare DoesNotExist traceback.

    TODO Phase 4 (auth): take `user` and scope the lookup to it, so one
    investor cannot read another's portfolio by guessing an id.
    """
    try:
        return Portfolio.objects.select_related("user").get(pk=portfolio_id)
    except (Portfolio.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFoundError(f"Portfolio {portfolio_id} does not exist.") from exc


def get_holdings(portfolio_id: int) -> QuerySet[Holding]:
    """
    Every current position in one portfolio, ticker-ordered.

    Returns an EMPTY queryset both when the portfolio holds nothing and when it
    does not exist. Callers that must tell those apart - the risk endpoint does,
    404 versus 400 - call `get_portfolio()` first.
    """
    return (
        Holding.objects.filter(portfolio_id=portfolio_id)
        .select_related("portfolio")
        .order_by("ticker")
    )


def get_tickers(portfolio_id: int) -> list[str]:
    """
    One portfolio's symbols, upper-cased and de-duplicated.

    Upper-casing matters: `marketdata.services` normalises before writing, so
    PriceSnapshot/PriceHistory rows are always upper-case. A holding entered as
    "reliance.ns" in the admin would otherwise silently find no prices.
    """
    seen: dict[str, None] = {}
    for raw in get_holdings(portfolio_id).values_list("ticker", flat=True):
        cleaned = (raw or "").strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def get_holding(holding_id: int, portfolio_id: int | None = None) -> Holding:
    """
    One holding by id, optionally scoped to the portfolio that owns it.

    `portfolio_id` is not decoration. The delete endpoint spells the portfolio
    in its URL, and scoping the lookup to it means a guessed holding id cannot
    reach into somebody else's portfolio - the same hole the TODO on
    `get_portfolio` describes, closed for this one path today rather than left
    for the auth phase.

    Raises:
        NotFoundError: no such holding, or it belongs to another portfolio.
            Both render as the same 404 on purpose: telling the caller which of
            the two it was is telling them an id exists.
    """
    filters = {"pk": holding_id}
    if portfolio_id is not None:
        filters["portfolio_id"] = portfolio_id
    try:
        return Holding.objects.select_related("portfolio").get(**filters)
    except (Holding.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFoundError(f"Holding {holding_id} does not exist.") from exc


def serialize_holding(holding: Holding) -> dict:
    """
    One holding as JSON.

    Money and quantity are emitted as STRINGS (common/MONEY.md): they are
    Decimals in the database and floating them through JSON to save four
    characters is how 1234.5600 becomes 1234.5600000000001 in a browser.

    `id` is the field the risk report's holdings block deliberately lacks - that
    block describes a VALUATION, this one describes a ROW you can edit or
    delete. The dashboard joins the two by ticker.
    """
    return {
        "id": holding.pk,
        "portfolio_id": holding.portfolio_id,
        "ticker": holding.ticker,
        "quantity": str(holding.quantity),
        "avg_buy_price": str(holding.avg_buy_price),
        "cost_basis": str(holding.cost_basis),
        "buy_date": holding.buy_date.isoformat() if holding.buy_date else None,
        "asset_type": holding.asset_type,
        "sector": holding.sector,
    }


def list_holdings(portfolio_id: int) -> list[dict]:
    """
    Every position in one portfolio, serialised, ticker-ordered.

    Unlike `get_holdings` this asserts the portfolio EXISTS first, so the
    endpoint answers a bad id with a 404 rather than an empty list that reads
    as "you own nothing".
    """
    get_portfolio(portfolio_id)
    return [serialize_holding(holding) for holding in get_holdings(portfolio_id)]

# TODO Phase 4 (remaining):
#     list_portfolios(user) -> QuerySet[Portfolio]
#     get_portfolio_valuation(portfolio_id) -> dict                  # joins latest PriceSnapshot
#         - market value, cost basis, unrealised P&L, per-holding weights (Decimal)
#       Note: risk/services.py already values the portfolio to build its weights
#       vector; move that valuation here when a /api/portfolios/ endpoint needs it
#       too, rather than growing a second copy.
