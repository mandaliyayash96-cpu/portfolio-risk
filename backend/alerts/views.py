"""
Alerts API.

Thin, like risk/views.py: no ORM, no validation, no envelope building. Each view
reads the URL, hands off to a service, and returns whatever dict comes back.
`common.renderers.EnvelopeJSONRenderer` wraps the success case and
`common.exceptions.custom_exception_handler` wraps every DomainError the service
raises, so a bad metric arrives as a 400 {"code": "invalid_input"} and an
unknown id as a 404 {"code": "not_found"} - never as a 500.

There is no endpoint for the event FEED here on purpose. The WebSocket sends the
open events as its connect snapshot, so a REST list would be a second way to
learn the same thing that could disagree with the first.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from alerts.selectors import rules_for, serialize_rule
from alerts.services import acknowledge, create_rule


@api_view(["GET", "POST"])
# Same posture as /api/risk/ - open so the dashboard works with no auth wired.
# TODO Phase 4 (auth): IsAuthenticated, and scope the portfolio lookup to
#      request.user so one investor cannot read or write another's rules.
@permission_classes([AllowAny])
def rules(request, portfolio_id: int):
    """
    GET  /api/alerts/rules/<portfolio_id>/   - every rule on the portfolio.
    POST /api/alerts/rules/<portfolio_id>/   - configure a new one.

    POST body:
        {"metric": "max_drawdown", "operator": "lt", "threshold": "-15"}

    `threshold` is in the metric's own units: whole percent for var_historical,
    max_drawdown, annualized_volatility and concentration; the bare number for
    hhi and beta. See AlertRule's docstring.

    Returns 201 with the created rule, or 200 with the list.
    """
    if request.method == "POST":
        # request.data is a dict for JSON and for form posts alike, but a bare
        # JSON array or string would arrive as a list/str - .get() on those
        # raises, so the shape is checked once here rather than in the service.
        body = request.data if isinstance(request.data, dict) else {}
        created = create_rule(
            portfolio_id,
            metric=body.get("metric"),
            operator=body.get("operator"),
            threshold=body.get("threshold"),
            active=body.get("active", True),
        )
        return Response(created, status=201)

    return Response([serialize_rule(rule) for rule in rules_for(portfolio_id)])


@api_view(["POST"])
@permission_classes([AllowAny])
def acknowledge_event(request, event_id: int):
    """
    POST /api/alerts/events/<event_id>/ack/

    Marks the event read and re-arms its rule: the dedupe in
    `scan_and_emit` only suppresses a breach while an UNacknowledged event
    stands against that rule, so acknowledging is also how you say "tell me
    again if this is still true".

    Idempotent - acknowledging twice returns the same event, not a 409.
    """
    return Response(acknowledge(event_id))
