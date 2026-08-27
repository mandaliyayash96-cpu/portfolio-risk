"""
Service tests: dedupe, rule validation, acknowledgement, and the broadcast.

The dedupe is the behaviour worth the most coverage here. A breach is a STATE -
volatility above your limit stays above it - so the difference between a useful
alert feed and an unusable one is entirely in whether a standing breach writes
one row or one row per scan.

Redis is never involved: `in_memory_channel_layer` swaps in Channels'
in-process layer, which is enough to prove group_send was called with the right
group and payload. Whether Redis itself works is Redis's test suite's problem.
"""

from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from alerts import services
from alerts.models import AlertEvent, AlertMetric, AlertOperator
from alerts.services import (
    EVENT_MESSAGE_TYPE,
    acknowledge,
    create_rule,
    group_name,
    scan_and_emit,
)
from common.exceptions import InvalidInputError, NotFoundError

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------
def test_a_breach_creates_one_event(saved_rule_factory, report, in_memory_channel_layer):
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )

    result = scan_and_emit(rule.portfolio_id, report=report)

    assert result["breached"] == 1
    assert result["created"] == 1
    assert result["suppressed"] == 0
    assert AlertEvent.objects.filter(rule=rule).count() == 1


def test_rescanning_a_standing_breach_creates_nothing(
    saved_rule_factory, report, in_memory_channel_layer
):
    """The dedupe, stated plainly: same breach, three scans, one row."""
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )

    first = scan_and_emit(rule.portfolio_id, report=report)
    second = scan_and_emit(rule.portfolio_id, report=report)
    third = scan_and_emit(rule.portfolio_id, report=report)

    assert first["created"] == 1
    assert (second["created"], second["suppressed"]) == (0, 1)
    assert (third["created"], third["suppressed"]) == (0, 1)
    assert AlertEvent.objects.filter(rule=rule).count() == 1


def test_a_suppressed_scan_still_reports_the_breach(
    saved_rule_factory, report, in_memory_channel_layer
):
    """
    `breached` counts what is true; `created` counts what is new.

    Keeping them separate is what lets the scan command say "still breached,
    already open" instead of the misleading "nothing breached".
    """
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    scan_and_emit(rule.portfolio_id, report=report)

    second = scan_and_emit(rule.portfolio_id, report=report)

    assert second["breached"] == 1
    assert second["created"] == 0


def test_acknowledging_rearms_the_rule(saved_rule_factory, report, in_memory_channel_layer):
    """
    Acknowledge is the reset. A breach that is still true after you have read
    the alert should be able to tell you again.
    """
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    first = scan_and_emit(rule.portfolio_id, report=report)
    acknowledge(first["events"][0]["id"])

    second = scan_and_emit(rule.portfolio_id, report=report)

    assert second["created"] == 1
    assert AlertEvent.objects.filter(rule=rule).count() == 2


def test_dedupe_is_per_rule_not_per_portfolio(
    saved_rule_factory, report, in_memory_channel_layer
):
    """
    Two different rules breaching at once must both fire.

    Keying the dedupe on the portfolio instead of the rule would let the first
    breach mask every other rule on the same portfolio.
    """
    var_rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    hhi_rule = saved_rule_factory(
        metric=AlertMetric.HHI, operator=AlertOperator.GT, threshold="0.3"
    )

    result = scan_and_emit(var_rule.portfolio_id, report=report)

    assert result["created"] == 2
    assert AlertEvent.objects.filter(rule__in=[var_rule, hhi_rule]).count() == 2


def test_an_acknowledged_event_does_not_suppress(
    saved_rule_factory, open_event_factory, report, in_memory_channel_layer
):
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    stale = open_event_factory(rule=rule)
    stale.acknowledged = True
    stale.save(update_fields=["acknowledged"])

    result = scan_and_emit(rule.portfolio_id, report=report)

    assert result["created"] == 1


def test_nothing_breached_writes_nothing(
    saved_rule_factory, report, in_memory_channel_layer
):
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="99"
    )

    result = scan_and_emit(rule.portfolio_id, report=report)

    assert (result["breached"], result["created"]) == (0, 0)
    assert not AlertEvent.objects.exists()


# ---------------------------------------------------------------------------
# What gets stored
# ---------------------------------------------------------------------------
def test_event_value_is_stored_in_rule_units(
    saved_rule_factory, report, in_memory_channel_layer
):
    """
    2.31, not 0.0231.

    The event and its threshold have to be comparable without the dashboard
    knowing about the percent scaling in the evaluator.
    """
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    scan_and_emit(rule.portfolio_id, report=report)

    assert AlertEvent.objects.get(rule=rule).value == Decimal("2.3100")


def test_negative_values_are_stored_with_their_sign(
    saved_rule_factory, report, in_memory_channel_layer
):
    rule = saved_rule_factory(
        metric=AlertMetric.MAX_DRAWDOWN, operator=AlertOperator.LT, threshold="-15"
    )
    scan_and_emit(rule.portfolio_id, report=report)

    assert AlertEvent.objects.get(rule=rule).value == Decimal("-18.4200")


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------
def test_new_events_are_pushed_to_the_portfolio_group(
    saved_rule_factory, report, in_memory_channel_layer
):
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(group_name(rule.portfolio_id), "test-channel")

    result = scan_and_emit(rule.portfolio_id, report=report)
    message = async_to_sync(layer.receive)("test-channel")

    assert result["broadcast"] == 1
    assert message["type"] == EVENT_MESSAGE_TYPE
    assert message["event"]["message"].startswith("VaR (historical) is 2.31%")
    assert message["event"]["value"] == "2.3100"


def test_suppressed_breaches_are_not_pushed(
    saved_rule_factory, report, in_memory_channel_layer
):
    """A standing breach must not re-ring the dashboard on every scan."""
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    scan_and_emit(rule.portfolio_id, report=report)

    assert scan_and_emit(rule.portfolio_id, report=report)["broadcast"] == 0


def test_a_broadcast_failure_does_not_lose_the_event(
    saved_rule_factory, report, in_memory_channel_layer, monkeypatch
):
    """
    Redis down is a degraded notification, not a lost alert.

    The row is committed before anything is pushed, so the event survives, shows
    up in the next connect snapshot, and the scan reports the delivery gap
    instead of failing.
    """

    def unreachable(*args, **kwargs):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(services, "get_channel_layer", unreachable)
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )

    result = scan_and_emit(rule.portfolio_id, report=report)

    assert result["created"] == 1
    assert result["broadcast"] == 0
    assert AlertEvent.objects.filter(rule=rule).count() == 1


# ---------------------------------------------------------------------------
# Rule creation
# ---------------------------------------------------------------------------
def test_create_rule_returns_the_serialised_rule(alert_portfolio):
    created = create_rule(
        alert_portfolio.pk,
        metric=AlertMetric.MAX_DRAWDOWN,
        operator=AlertOperator.LT,
        threshold="-15",
    )

    assert created["metric"] == "max_drawdown"
    assert created["metric_label"] == "Max drawdown"
    assert created["threshold"] == "-15.0000"
    assert created["active"] is True


def test_create_rule_rejects_an_unknown_metric(alert_portfolio):
    with pytest.raises(InvalidInputError) as exc:
        create_rule(
            alert_portfolio.pk,
            metric="sharpe_ratio",
            operator=AlertOperator.GT,
            threshold="1",
        )
    assert "sharpe_ratio" in exc.value.message


def test_create_rule_rejects_an_unknown_operator(alert_portfolio):
    with pytest.raises(InvalidInputError):
        create_rule(
            alert_portfolio.pk,
            metric=AlertMetric.HHI,
            operator="ne",
            threshold="0.3",
        )


@pytest.mark.parametrize("threshold", ["", "abc", None, "nan", "inf"])
def test_create_rule_rejects_a_non_numeric_threshold(alert_portfolio, threshold):
    """
    "nan" is the one that matters. It is a valid Decimal, and every comparison
    against it is False - so the rule would save cleanly and then never fire.
    """
    with pytest.raises(InvalidInputError):
        create_rule(
            alert_portfolio.pk,
            metric=AlertMetric.HHI,
            operator=AlertOperator.GT,
            threshold=threshold,
        )


def test_create_rule_404s_on_an_unknown_portfolio():
    with pytest.raises(NotFoundError):
        create_rule(
            99_999, metric=AlertMetric.HHI, operator=AlertOperator.GT, threshold="0.3"
        )


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------
def test_acknowledge_marks_the_event_read(open_event_factory):
    event = open_event_factory()

    result = acknowledge(event.pk)

    event.refresh_from_db()
    assert event.acknowledged is True
    assert result["acknowledged"] is True


def test_acknowledge_is_idempotent(open_event_factory):
    """Two dashboard tabs clicking the same button is not a conflict."""
    event = open_event_factory()
    acknowledge(event.pk)

    assert acknowledge(event.pk)["acknowledged"] is True


def test_acknowledge_404s_on_an_unknown_event():
    with pytest.raises(NotFoundError):
        acknowledge(99_999)
