"""
Alert reads (architecture rule 1: views never touch the ORM).

Also home to the two serialisers, because an alert has exactly THREE readers -
the REST endpoint, the WebSocket snapshot on connect, and the WebSocket push on
breach - and all three must describe an event identically. A tab that connected
before a breach and a tab that connected after it would otherwise render the
same event from two different shapes. One function, one shape.

Money and thresholds leave as STRINGS (common/MONEY.md): exact in transit, and
it stops a 4-decimal Decimal arriving in the browser as 2.3100000000000005.
"""

from __future__ import annotations

from django.db.models import QuerySet

from alerts.models import AlertEvent, AlertRule


def active_rules(portfolio_id: int | None = None) -> QuerySet[AlertRule]:
    """
    Rules that are switched on, newest-configured last.

    Args:
        portfolio_id: narrow to one portfolio. None means every portfolio,
            which is what `manage.py scan_alerts` iterates.

    Returns:
        A queryset with `portfolio` joined, because the evaluator builds alert
        messages naming the portfolio and would otherwise fire one extra query
        per rule.
    """
    queryset = AlertRule.objects.filter(active=True).select_related("portfolio")
    if portfolio_id is not None:
        queryset = queryset.filter(portfolio_id=portfolio_id)
    return queryset


def rules_for(portfolio_id: int) -> QuerySet[AlertRule]:
    """
    Every rule on a portfolio, active or not - what the config list shows.

    Distinct from `active_rules` on purpose: the dashboard must be able to show
    you a rule you have switched off, or you cannot switch it back on.
    """
    return AlertRule.objects.filter(portfolio_id=portfolio_id).select_related("portfolio")


def open_events(portfolio_id: int) -> QuerySet[AlertEvent]:
    """
    Unacknowledged events for one portfolio, newest first.

    "Open" is the state that matters twice over: it is what a freshly connected
    socket is sent as its snapshot, and it is the dedupe key that stops one
    standing breach writing a new row on every scan (see
    `alerts.services.scan_and_emit`).
    """
    return (
        AlertEvent.objects.filter(rule__portfolio_id=portfolio_id, acknowledged=False)
        .select_related("rule", "rule__portfolio")
        .order_by("-triggered_at", "-id")
    )


def recent_events(portfolio_id: int, limit: int = 50) -> QuerySet[AlertEvent]:
    """The last `limit` events for a portfolio, acknowledged or not."""
    return (
        AlertEvent.objects.filter(rule__portfolio_id=portfolio_id)
        .select_related("rule", "rule__portfolio")
        .order_by("-triggered_at", "-id")[:limit]
    )


def open_rule_ids(portfolio_id: int) -> set[int]:
    """
    Which rules already have an unacknowledged event standing against them.

    One query for the whole scan rather than an `.exists()` per rule: the
    dedupe check in scan_and_emit runs once per breached rule, and a portfolio
    with a dozen rules should not cost a dozen round trips to learn something a
    single indexed IN query answers.
    """
    return set(
        AlertEvent.objects.filter(
            rule__portfolio_id=portfolio_id, acknowledged=False
        ).values_list("rule_id", flat=True)
    )


def unacknowledged_count(portfolio_id: int) -> int:
    """How many open alerts the portfolio has - the badge number."""
    return open_events(portfolio_id).count()


# ---------------------------------------------------------------------------
# Serialisation - the one shape an alert has, wherever it is read from
# ---------------------------------------------------------------------------
def serialize_rule(rule: AlertRule) -> dict:
    """
    One rule as JSON.

    `metric_label` and `operator_label` are sent alongside the raw values so the
    dashboard renders Django's own choice labels instead of maintaining a
    second copy of them in JavaScript that can drift.
    """
    return {
        "id": rule.pk,
        "portfolio_id": rule.portfolio_id,
        "metric": rule.metric,
        "metric_label": rule.get_metric_display(),
        "operator": rule.operator,
        "operator_label": rule.get_operator_display(),
        "threshold": str(rule.threshold),
        "active": rule.active,
        "created_at": rule.created_at.isoformat(),
    }


def serialize_event(event: AlertEvent) -> dict:
    """
    One event as JSON, with enough of its rule inlined to render standalone.

    The rule is embedded rather than referenced by id because the feed is a
    push: an event arriving over the socket has to be displayable on its own,
    without the client holding a rules cache it would then have to keep in sync.

    Callers must have `rule` (and `rule__portfolio`) selected - the selectors
    above all do. From a consumer this runs inside database_sync_to_async, so a
    lazy FK dereference here would be a synchronous ORM call on the event loop.
    """
    rule = event.rule
    return {
        "id": event.pk,
        "rule_id": event.rule_id,
        "portfolio_id": rule.portfolio_id,
        "metric": rule.metric,
        "metric_label": rule.get_metric_display(),
        "operator": rule.operator,
        "operator_label": rule.get_operator_display(),
        # Same units as `value`, so the feed can show "2.31% vs 2.00%" without
        # knowing anything about the percent scaling in alerts.evaluator.
        "threshold": str(rule.threshold),
        "value": str(event.value),
        "message": event.message,
        "acknowledged": event.acknowledged,
        "triggered_at": event.triggered_at.isoformat(),
    }
