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
Each view runs the URL's id through `accounts.selectors.resolve_portfolio_id`,
which returns the caller's OWN portfolio whatever id they typed. The delete
route resolves it BEFORE it is used to scope the holding lookup, so a caller
cannot delete out of a portfolio that is not theirs even by guessing both ids.

EDITING IS PAID; READING IS FREE
--------------------------------
Every write below - the manual add, the CSV import, the broker import and the
delete - calls `payments.gating.require_editing_unlock` first. Editing holdings
costs ₹9 for a round of changes, so a request without a live unlock is refused
with 402
{"code": "payment_required"} - and one without a signed-in user with 401,
because a grant belongs to an account.

Reading is untouched. The risk report, the charts, the holdings GET below and
the PDF are all still open, which is deliberate: the dashboard has to be worth
looking at before anyone will pay to change it.

The gate does NOT consume the unlock - a round holds as many edits as the user
wants, which is the difference between charging per round and charging per
edit. `payments/services.py` documents when a round ends.
"""

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.selectors import resolve_portfolio_id
from payments.gating import require_editing_unlock
from portfolio.selectors import list_holdings
from portfolio.services import (
    add_holding,
    delete_holding,
    import_broker_holdings,
    import_holdings_csv,
)


@api_view(["GET", "POST"])
# AllowAny is the ROUTE's permission, not the method's: GET is free and POST is
# gated inside the view, because one URL serves both and DRF's permission
# classes cannot tell them apart. The gate is the first thing the POST branch
# does - see require_editing_unlock below.
#
# TODO (reads): the GET here, and /api/risk/ and friends, are still open to an
#      anonymous caller who names a portfolio id. Closing that is a separate
#      decision from the payment gate - it costs the "curl portfolio 1" demo
#      path - so it is left explicit rather than done quietly here.
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

    GET is free. POST costs ₹9 for the editing round it belongs to, and answers
    402 {"code": "payment_required"} without one.
    """
    if request.method == "POST":
        # Before resolve_portfolio_id, so an unpaid caller is told the honest
        # thing - editing is locked - rather than 404 for a portfolio the gate
        # was never going to let them touch anyway.
        require_editing_unlock(request)

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

    Costs ₹9 for the editing round, like every other write here. A 500-row
    import and a single add are the same one unlock - the price is per round,
    not per position.
    """
    require_editing_unlock(request)
    return Response(
        import_holdings_csv(resolve_portfolio_id(request, portfolio_id), request.FILES.get("file"))
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def import_broker(request, portfolio_id: int):
    """
    POST /api/portfolio/<portfolio_id>/import-broker/

    Body: {"broker": "zerodha" | "groww" | "upstox" | "icici"}

    Pulls that broker's holdings into the portfolio and returns the CSV
    importer's report shape, plus `broker`, `broker_label` and `simulated`.

    THE FETCH IS SIMULATED. The rows come from a preset table in
    `portfolio.brokers`; no broker is contacted and no credential is asked for.
    The WRITE is not simulated - it upserts on ticker through the same service
    the manual form and the CSV import use, so importing two brokers that both
    report HDFCBANK.NS leaves one consolidated position.

    Gated like every other write: 402 without a live ₹9 unlock, 401 with no
    account. A broker import and a single add are the same one unlock.
    """
    # First, as everywhere else on this page - an unpaid caller hears "editing
    # is locked", not a broker name they cannot use yet anyway.
    require_editing_unlock(request)

    portfolio_id = resolve_portfolio_id(request, portfolio_id)
    # Same normalisation the add view does, and for the same reason: a bare
    # JSON array or string arrives as a list/str, where .get() raises.
    body = request.data if isinstance(request.data, dict) else {}
    return Response(import_broker_holdings(portfolio_id, body.get("broker")))


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
    difference between those two answers on purpose - and 402s before either,
    because deleting a position is editing and editing is paid for.
    """
    require_editing_unlock(request)
    return Response(
        delete_holding(holding_id, portfolio_id=resolve_portfolio_id(request, portfolio_id))
    )
