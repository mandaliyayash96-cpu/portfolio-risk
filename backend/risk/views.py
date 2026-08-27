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

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

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
