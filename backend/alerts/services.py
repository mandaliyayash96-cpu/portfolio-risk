"""
Alert writes (architecture rule 1).

Three jobs: configure rules, turn breaches into AlertEvent rows, and push those
rows to whoever is watching. The measuring itself belongs to `alerts.evaluator`,
which belongs to `risk.services` - nothing in this module knows what a VaR is.

THE ORDER OF THE TWO SIDE EFFECTS
---------------------------------
`scan_and_emit` writes to the database FIRST and to the channel layer SECOND,
and never the other way round. The database is the record; Redis is a
notification. If the broadcast fails, the event still exists, still appears in
the REST list, and still arrives in the snapshot the next socket to connect
receives - the user learns about it a moment later instead of never. Broadcast
first would risk the opposite: a breach announced in a browser that no row
backs, which vanishes on refresh.

That is why `_broadcast` swallows its exceptions. A Redis blip is a degraded
notification channel, not a reason for `manage.py scan_alerts` to exit non-zero
having already committed rows.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from alerts.evaluator import METRICS, evaluate_rules
from alerts.models import AlertEvent, AlertOperator, AlertRule
from alerts.selectors import open_events, open_rule_ids, serialize_event, serialize_rule
from common.exceptions import InvalidInputError, NotFoundError
from portfolio.selectors import get_portfolio

logger = logging.getLogger(__name__)

#: DecimalField(decimal_places=4) - quantise before saving rather than letting
#: the database round, so the value we broadcast is byte-for-byte the value we
#: stored. Feed and API must not disagree in the fourth decimal.
_QUANT = Decimal("0.0001")

#: The message type the consumer's `alert_event` handler is named after.
#: Channels maps "alert.event" -> `alert_event()`; the dots-to-underscores rule
#: is the whole contract between this module and alerts/consumers.py.
EVENT_MESSAGE_TYPE = "alert.event"


def group_name(portfolio_id: int) -> str:
    """
    The channel group every socket watching one portfolio joins.

    Defined here and imported by the consumer rather than spelled twice: a
    typo in either copy would be invisible - sockets would connect happily,
    scans would report success, and nothing would ever arrive.
    """
    return f"alerts_{portfolio_id}"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def create_rule(
    portfolio_id: int,
    *,
    metric: str,
    operator: str,
    threshold,
    active: bool = True,
) -> dict:
    """
    Configure a new rule on a portfolio.

    Validation lives here rather than in the view (architecture rule 1) and
    raises DomainErrors, which the exception handler renders as clean 400/404
    envelopes. There is no DRF serializer because there is no DRF serializer
    anywhere in this codebase - four scalar fields do not need one.

    Args:
        portfolio_id: which portfolio the rule belongs to.
        metric: an `AlertMetric` value. Validated against `evaluator.METRICS`,
            not just against the model choices, so it is impossible to save a
            rule the evaluator has no way to check.
        operator: an `AlertOperator` value.
        threshold: number or numeric string, in the metric's rule units (see
            AlertRule's docstring: whole percent, or bare for hhi/beta).
        active: rules start switched on.

    Returns:
        The created rule, serialised.

    Raises:
        NotFoundError: no such portfolio.
        InvalidInputError: unknown metric, unknown operator, or a threshold
            that is not a finite number.
    """
    portfolio = get_portfolio(portfolio_id)  # 404s on a bad id

    if metric not in METRICS:
        raise InvalidInputError(
            f"Unknown metric {metric!r}. Choose one of: {', '.join(sorted(METRICS))}.",
            details={"metric": metric, "supported": sorted(METRICS)},
        )

    valid_operators = [choice.value for choice in AlertOperator]
    if operator not in valid_operators:
        raise InvalidInputError(
            f"Unknown operator {operator!r}. Choose one of: {', '.join(valid_operators)}.",
            details={"operator": operator, "supported": valid_operators},
        )

    try:
        # str() first: float("nan") is a perfectly good float and would reach
        # the database as a value no comparison can ever be true about.
        parsed = Decimal(str(threshold).strip())
        if not parsed.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ArithmeticError, AttributeError, TypeError, ValueError) as exc:
        raise InvalidInputError(
            f"Threshold {threshold!r} is not a number. Percent metrics take a "
            "whole number (5 = 5%, -15 = -15%); hhi and beta take the bare value.",
            details={"threshold": threshold},
        ) from exc

    rule = AlertRule.objects.create(
        portfolio=portfolio,
        metric=metric,
        operator=operator,
        threshold=parsed.quantize(_QUANT, rounding=ROUND_HALF_UP),
        active=bool(active),
    )
    logger.info("Created alert rule %s on portfolio %s: %s", rule.pk, portfolio_id, rule)
    return serialize_rule(rule)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def scan_and_emit(portfolio_id: int, *, report: dict | None = None) -> dict:
    """
    Measure one portfolio, record what is newly breached, and push it out.

    DEDUPE
    ------
    A breach is a STATE, not an occurrence. Volatility above your threshold
    stays above it for days, and a scan every minute would otherwise write a
    thousand identical rows and ring a thousand times. So: a rule that already
    has an unacknowledged event is considered "already firing" and is skipped.
    Acknowledging the event re-arms the rule - the next scan that still finds it
    breached opens a fresh event.

    Rule identity is enough to dedupe on. A rule owns exactly one metric, so
    "same rule" and "same rule + metric" select the same rows; keying on the FK
    lets the check be one indexed query for the whole scan instead of one per
    breach.

    Args:
        portfolio_id: which portfolio to scan.
        report: an already-computed risk report to evaluate against, passed
            through to the evaluator. Left None in normal use.

    Returns:
        A summary dict - `breached`, `created`, `suppressed`, `events`,
        `broadcast` - which the management command prints and the tests assert
        on. `suppressed` is the count deduped away, and is the number that
        tells you the dedupe is working rather than that nothing broke.

    Raises:
        The evaluator's four data errors (not found / empty / missing prices /
        insufficient history). Callers scanning MANY portfolios should catch
        DomainError per portfolio - `manage.py scan_alerts` does.
    """
    breaches = evaluate_rules(portfolio_id, report=report)

    already_open = open_rule_ids(portfolio_id)
    fresh = [breach for breach in breaches if breach.rule.pk not in already_open]
    suppressed = len(breaches) - len(fresh)

    # One transaction for the whole batch: a scan that half-succeeds would leave
    # some rules re-armed and some not, and the next scan would fire a partial
    # duplicate set.
    with transaction.atomic():
        events = [
            AlertEvent.objects.create(
                rule=breach.rule,
                value=_to_decimal(breach.current_value),
                message=breach.message,
            )
            for breach in fresh
        ]

    # Committed. Only now does anyone get told.
    payloads = [serialize_event(event) for event in events]
    delivered = sum(_broadcast(portfolio_id, payload) for payload in payloads)

    if events:
        logger.info(
            "Portfolio %s: %s new alert(s), %s suppressed as already open.",
            portfolio_id,
            len(events),
            suppressed,
        )
    return {
        "portfolio_id": portfolio_id,
        "breached": len(breaches),
        "created": len(events),
        "suppressed": suppressed,
        "broadcast": delivered,
        "events": payloads,
    }


def acknowledge(event_id: int) -> dict:
    """
    Mark one event read, which also re-arms its rule for the next scan.

    Acknowledging an already-acknowledged event is a no-op that returns the
    event rather than an error: two dashboard tabs clicking the same button is
    not a conflict, and a 409 there would be noise.

    Raises:
        NotFoundError: no such event.
    """
    try:
        event = AlertEvent.objects.select_related("rule", "rule__portfolio").get(pk=event_id)
    except (AlertEvent.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFoundError(f"Alert event {event_id} does not exist.") from exc

    if not event.acknowledged:
        event.acknowledged = True
        event.save(update_fields=["acknowledged", "updated_at"])
        logger.info("Acknowledged alert event %s (rule %s).", event.pk, event.rule_id)

    # TODO Phase-later: broadcast the acknowledgement too, so a second open tab
    #                   clears the same event without a refresh. Left out for
    #                   now because it needs a second message type on the wire
    #                   and the panel already updates itself optimistically.
    return serialize_event(event)


def open_alerts(portfolio_id: int) -> list[dict]:
    """Every unacknowledged event, serialised - the socket's connect snapshot."""
    return [serialize_event(event) for event in open_events(portfolio_id)]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _to_decimal(value: float) -> Decimal:
    """
    float (rule units) -> Decimal, quantised to the column's 4 places.

    `str(value)` rather than `Decimal(value)`: the float repr is the shortest
    string that round-trips, whereas constructing from the float directly drags
    in the full binary expansion (0.1 becomes 0.1000000000000000055511151231...)
    and then rounds THAT. Same answer here, but only by luck at 4 places.
    """
    return Decimal(str(value)).quantize(_QUANT, rounding=ROUND_HALF_UP)


def _broadcast(portfolio_id: int, payload: dict) -> bool:
    """
    Push one serialised event to every socket watching this portfolio.

    Returns True if the layer accepted it, False if there was no layer or it
    failed. Never raises: see this module's docstring - the row is already
    committed, and a notification failure must not be reported as a scan
    failure.

    `async_to_sync` is correct for every caller today (a management command and
    a DRF view, both synchronous). Calling this from inside a running event loop
    would raise, so an async caller should await `group_send` directly.
    """
    try:
        # Inside the try, not above it: channels_redis builds the layer lazily,
        # so an unparseable REDIS_URL raises HERE rather than at group_send.
        layer = get_channel_layer()
        if layer is None:
            logger.warning(
                "No channel layer configured, so alert %s was stored but not "
                "pushed. Check CHANNEL_LAYERS in config/settings.py.",
                payload.get("id"),
            )
            return False

        async_to_sync(layer.group_send)(
            group_name(portfolio_id),
            {"type": EVENT_MESSAGE_TYPE, "event": payload},
        )
    except Exception:
        # Broad on purpose: channels_redis raises connection, timeout and
        # protocol errors from several libraries, and every one of them means
        # the same thing here - stored, not delivered.
        logger.exception(
            "Could not push alert %s to %s. The event is saved and will appear "
            "in the next connect snapshot; check that Redis is reachable.",
            payload.get("id"),
            group_name(portfolio_id),
        )
        return False
    return True
