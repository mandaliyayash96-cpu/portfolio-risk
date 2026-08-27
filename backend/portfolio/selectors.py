"""
Portfolio reads (architecture rule 1: views never touch the ORM).

Query functions return querysets, model instances or plain dicts - never
Response objects, and never raw ORM access from a view or a command.
"""

from portfolio.models import Holding


def get_all_held_tickers() -> list[str]:
    """
    Every distinct ticker held in any portfolio, alphabetically.

    This is the fetch list for market data: `manage.py fetch_prices` today,
    the Celery poll task from Phase 6 onward.
    """
    return list(
        Holding.objects.order_by("ticker").values_list("ticker", flat=True).distinct()
    )


# TODO Phase 4:
#     get_portfolio(portfolio_id, user=None) -> Portfolio            # raise NotFoundError
#     list_portfolios(user) -> QuerySet[Portfolio]
#     get_holdings(portfolio_id) -> QuerySet[Holding]                # select_related portfolio
#     get_tickers(portfolio_id) -> list[str]                         # one portfolio's symbols
#     get_portfolio_valuation(portfolio_id) -> dict                  # joins latest PriceSnapshot
#         - market value, cost basis, unrealised P&L, per-holding weights (Decimal)
