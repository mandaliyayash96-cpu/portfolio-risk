"""
Risk API.

Architecture rule 1 in its purest form: this view does no ORM work and no
maths. It takes an id off the URL, calls the service, and hands the dict back.

The envelope is not built here either - `common.renderers.EnvelopeJSONRenderer`
wraps the success case and `common.exceptions.custom_exception_handler` wraps
every DomainError the service raises, so all four failure modes
(not_found / empty_portfolio / missing_price_data / insufficient_history)
arrive as {"success": false, "data": null, "error": {...}} with the right
status code, never as a 500.
"""

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from common.exceptions import DomainError
from common.renderers import EnvelopeJSONRenderer, PDFRenderer
from common.response import envelope, error_payload
from risk.report_pdf import build_risk_pdf
from risk.services import compute_performance, compute_rebalance, compute_risk


@api_view(["GET"])
# Hackathon MVP: open so the report is testable in a browser. The portfolio is
# somebody's actual position, so this cannot ship open.
# TODO Phase 4 (auth): IsAuthenticated, and scope the lookup to request.user in
#      portfolio.selectors.get_portfolio so ids cannot be enumerated.
@permission_classes([AllowAny])
def risk_report(request, portfolio_id: int):
    """
    GET /api/risk/<portfolio_id>/

    Returns the full RiskReport for the portfolio: annualised return and
    volatility, beta against the benchmark, Sharpe, Sortino, max drawdown, VaR
    (historical / parametric / Monte Carlo), CVaR, HHI, per-asset volatility
    and the correlation/covariance matrices - plus which portfolio it is and
    how it was valued.

    Prices come from whatever `manage.py fetch_prices` last stored; this
    endpoint never calls the market data provider itself.
    """
    return Response(compute_risk(portfolio_id))


@api_view(["GET"])
# Same posture as the risk report above; the same TODO applies.
@permission_classes([AllowAny])
def rebalance_report(request, portfolio_id: int):
    """
    GET /api/rebalance/<portfolio_id>/

    Markowitz mean-variance suggestion for the SAME holdings: the current
    weights and their volatility, the minimum-variance and maximum-Sharpe
    allocations with theirs, and the efficient frontier the three sit on.

    Computed from the same prepared inputs as /api/risk/, so the "current"
    figures here match that report exactly.
    """
    return Response(compute_rebalance(portfolio_id))


@api_view(["GET"])
# Same posture as the two reports above; the same TODO applies.
@permission_classes([AllowAny])
def performance_report(request, portfolio_id: int):
    """
    GET /api/performance/<portfolio_id>/

    The portfolio's history rather than its summary: a value curve rebased to
    100 and the drawdown at every date on it, plus the peak, the current value
    and the max drawdown for reference.

    Built from the same prepared inputs as /api/risk/, so the max drawdown here
    is the identical number that report shows and the last point of the curve
    is the same day.
    """
    return Response(compute_performance(portfolio_id))


@api_view(["GET"])
# Same posture as the three reports above; the same TODO applies. This one
# leaks the portfolio NAME into a filename as well as its numbers into a file,
# which is one more reason it cannot ship open.
@permission_classes([AllowAny])
# EnvelopeJSONRenderer first, so */* and application/json both negotiate to it.
# PDFRenderer is here only so a client that asks for application/pdf - the media
# type this URL's own name advertises - is not answered 406 by content
# negotiation, which runs before the view does. Neither renderer touches the
# success path: an HttpResponse bypasses rendering entirely.
@renderer_classes([EnvelopeJSONRenderer, PDFRenderer])
def risk_report_pdf(request, portfolio_id: int):
    """
    GET /api/risk/<portfolio_id>/report.pdf

    The same report as GET /api/risk/<portfolio_id>/, rendered as a document:
    header, holdings, every headline metric with a plain-English line saying
    what it means, per-holding volatility, the measurement window, and any
    warnings. `compute_risk` is called exactly as the JSON endpoint calls it,
    so the file and the dashboard cannot disagree.

    TWO RESPONSE SHAPES, AND WHY THE ERROR ONE IS BUILT BY HAND
    -----------------------------------------------------------
    Success is a PDF. Failure is the standard JSON envelope - NOT a PDF
    containing an apology, and not an empty file with a 400 on it, either of
    which a browser would happily save to disk as `report.pdf`.

    The failure path returns a plain JsonResponse rather than a DRF Response
    because a DRF Response is rendered by whichever renderer content
    negotiation picked, and a client that asked for application/pdf would get
    its envelope back under a PDF content type. Building it by hand is exactly
    the case `common/response.py` documents its helpers for, and it makes the
    status code and body independent of what the caller asked for.

    Unexpected exceptions are not caught here: they still reach
    `custom_exception_handler`, which logs the traceback and returns the 500
    envelope. Only the four EXPECTED data failures are handled below.
    """
    try:
        report = compute_risk(portfolio_id)
    except DomainError as exc:
        # not_found (404) / empty_portfolio (400) / missing_price_data (422) /
        # insufficient_history (422) - the same four the JSON endpoint raises.
        return JsonResponse(
            envelope(error=error_payload(exc.code, exc.message, exc.details)),
            status=exc.status_code,
        )

    # `generated_at` is passed in rather than read inside the builder, which
    # keeps that module free of Django. "What time is it, in the timezone this
    # deployment cares about" is a settings question, and settings questions
    # belong on this side of the line - the same split risk/services.py keeps
    # against risk/engine.py. Left to itself the builder would stamp UTC.
    pdf = build_risk_pdf(report, generated_at=timezone.localtime())
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{_pdf_filename(report)}"'
    # Browsers cache aggressively on a URL that looks like a static file, and a
    # risk report is recomputed on every request by design.
    response["Cache-Control"] = "no-store"
    return response


def _pdf_filename(report: dict) -> str:
    """
    `risk-report-my-demo-2026-08-31.pdf`.

    The portfolio name is slugified rather than interpolated raw: it is
    user-entered text, and a name containing a quote or a newline would let the
    caller inject into the Content-Disposition header. Django's slugify drops
    everything that is not alphanumeric or a hyphen, which closes that and also
    produces a filename that survives every filesystem.
    """
    name = slugify((report.get("portfolio") or {}).get("name") or "") or "portfolio"
    return f"risk-report-{name}-{timezone.localdate().isoformat()}.pdf"
