"""
Holdings API.

Thin, like risk/views.py and alerts/views.py: no ORM, no validation, no maths,
no envelope building. Each view reads the URL, pulls the fields off the body,
calls a service and returns whatever dict comes back.

That is what keeps the failure modes honest. Every rule about tickers,
quantities, prices, CSV headers and file size lives in `portfolio.services` and
raises a DomainError, which `common.exceptions.custom_exception_handler` turns
into a 400 {"code": "invalid_input"} or a 404 {"code": "not_found"} envelope. A
bad upload therefore cannot reach the client as a 500, and it cannot reach it
in a shape the dashboard has not already been taught to render.

WHOSE PORTFOLIO IS BEING WRITTEN
--------------------------------
Each view runs the URL's id through `accounts.selectors.resolve_portfolio_id`
first: a request carrying a verified Firebase token writes to its OWN
portfolio whatever id it typed, and an anonymous one still addresses the id in
the URL so portfolio 1 stays testable. The delete route resolves the portfolio
id BEFORE it is used to scope the holding lookup, so a signed-in caller cannot
delete out of a portfolio that is not theirs even by guessing both ids.
"""

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.selectors import resolve_portfolio_id
from portfolio.selectors import list_holdings
from portfolio.services import add_holding, delete_holding, import_holdings_csv


@api_view(["GET", "POST"])
# Same posture as /api/risk/ - open so the dashboard works before the login
# screen exists. An authenticated caller is already pinned to their own
# portfolio; an anonymous one can still write to any id, and this route WRITES,
# so it is the most urgent of the three to close.
# TODO Part 3 (enforce auth): IsAuthenticated here, and drop the URL-id
#      fallback in accounts.selectors.resolve_portfolio_id.
@permission_classes([AllowAny])
def holdings(request, portfolio_id: int):
    """
    GET  /api/portfolio/<portfolio_id>/holdings/  - every position, with ids.
    POST /api/portfolio/<portfolio_id>/holdings/  - add or replace one.

    POST body:
        {"ticker": "RELIANCE.NS", "quantity": "10", "avg_buy_price": "1400.50",
         "buy_date": "2026-01-05", "asset_type": "EQUITY", "sector": "Energy"}

    Only the first three are required. Posting a ticker the portfolio already
    holds REPLACES that position (the unique constraint allows one row per
    ticker), so 201 means created and 200 means replaced.

    The response carries a `warning` when the ticker saved but its prices could
    not be fetched - an unknown symbol is saved deliberately, so a typo is a
    thing you can see and fix rather than a thing that vanished.

    GET is here because the risk report's holdings block describes a valuation
    and carries no row ids; the dashboard needs those for the delete button.
    """
    portfolio_id = resolve_portfolio_id(request, portfolio_id)

    if request.method == "POST":
        # request.data is a dict for JSON and form posts alike, but a bare JSON
        # array or string arrives as a list/str, where .get() raises. Checked
        # once here rather than in the service, exactly as alerts/views.py does.
        body = request.data if isinstance(request.data, dict) else {}
        result = add_holding(
            portfolio_id,
            body.get("ticker"),
            body.get("quantity"),
            body.get("avg_buy_price"),
            buy_date=body.get("buy_date"),
            asset_type=body.get("asset_type"),
            sector=body.get("sector"),
        )
        return Response(result, status=201 if result["created"] else 200)

    return Response(list_holdings(portfolio_id))


@api_view(["POST"])
@permission_classes([AllowAny])
# Declared explicitly rather than left to the defaults: this endpoint accepts
# ONLY a multipart upload, and saying so means a JSON body gets DRF's own 415
# instead of the service's "no file uploaded".
@parser_classes([MultiPartParser, FormParser])
def holdings_import(request, portfolio_id: int):
    """
    POST /api/portfolio/<portfolio_id>/holdings/import/

    Multipart upload with the CSV in the `file` field. Required columns are
    ticker, quantity, avg_buy_price; buy_date, asset_type and sector are
    optional. Header case and spacing are normalised, so "Avg Buy Price" works.

    Returns 200 with a per-row report - added / updated / skipped-with-reason -
    even when some rows failed, because a partial import IS the expected
    outcome and the caller needs to see which half was which. The upload is
    rejected outright (400) only for problems no row survives: not a CSV, over
    1 MB, over 500 rows, or missing a required column.
    """
    return Response(
        import_holdings_csv(resolve_portfolio_id(request, portfolio_id), request.FILES.get("file"))
    )


@api_view(["DELETE"])
@permission_classes([AllowAny])
def holding_detail(request, portfolio_id: int, holding_id: int):
    """
    DELETE /api/portfolio/<portfolio_id>/holdings/<holding_id>/

    Removes one position. The portfolio id is not decorative - the lookup is
    scoped to it, so a guessed holding id cannot delete out of a portfolio the
    URL does not name. Stored prices for the ticker are kept: another portfolio
    may hold it, and re-adding it should not need the network again.

    404s on a holding that does not exist or belongs elsewhere; there is no
    difference between those two answers on purpose.
    """
    return Response(
        delete_holding(holding_id, portfolio_id=resolve_portfolio_id(request, portfolio_id))
    )
