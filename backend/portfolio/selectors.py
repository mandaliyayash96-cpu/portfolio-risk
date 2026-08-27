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


# TODO Phase 4 (remaining):
#     list_portfolios(user) -> QuerySet[Portfolio]
#     get_portfolio_valuation(portfolio_id) -> dict                  # joins latest PriceSnapshot
#         - market value, cost basis, unrealised P&L, per-holding weights (Decimal)
#       Note: risk/services.py already values the portfolio to build its weights
#       vector; move that valuation here when a /api/portfolios/ endpoint needs it
#       too, rather than growing a second copy.
