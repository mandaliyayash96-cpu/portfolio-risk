"""
Scheduled alert scan (Phase 8).

Like marketdata/tasks.py, this adds no logic of its own. `alerts.services.
scan_and_emit` already measures a portfolio, writes the new breaches and pushes
them to every socket watching - all of it Phase 6 code that has not changed.
This is the timer that calls it for every portfolio, and `manage.py scan_alerts`
still calls the same function for the same reason.

WHAT THIS COMPLETES
-------------------
Phase 6 made alerts real-time but not automatic: a breach appeared in the
browser the moment somebody ran the command. With this task on the schedule,
the loop closes without a human in it -

    beat -> refresh_all_prices  (writes fresh prices)
         -> scan_all_alerts     (10s later, measures them)
         -> scan_and_emit       (writes AlertEvent, group_send)
         -> the browser's open WebSocket updates

- and the dashboard alerts panel starts filling in on its own.

ONE PORTFOLIO'S FAILURE IS NOT THE SCAN'S FAILURE
-------------------------------------------------
Exactly the rule `manage.py scan_alerts` documents, for the same reason: a
portfolio holding one unfetched ticker raises a DomainError out of
compute_risk, and with several portfolios configured that must not stop the
others being measured. Caught per portfolio, counted as a skip.

The one place this DIFFERS from the command is the ending. The command raises
CommandError when anything was skipped, because a human asked it to scan and
needs a non-zero exit code. A scheduled task must not: an unmeasurable
portfolio is a standing condition (an empty portfolio stays empty), and turning
that into a task failure every 60 seconds would be an alarm that is always on,
which is the same as no alarm. So skips are reported in the return value and
logged, and the task succeeds.
"""

from __future__ import annotations

import logging

from celery import shared_task

from alerts.selectors import active_rules
from alerts.services import scan_and_emit
from common.exceptions import DomainError
from portfolio.models import Portfolio

logger = logging.getLogger(__name__)


@shared_task(name="alerts.scan_all_alerts")
def scan_all_alerts() -> dict:
    """
    Evaluate every active rule on every portfolio and push what newly fired.

    Scheduled by settings.CELERY_BEAT_SCHEDULE every ALERT_SCAN_SECONDS,
    published with a countdown so it lands AFTER the price refresh that feeds
    it - see the beat schedule comment in settings.py.

    Deduplication is the service's job and is unchanged: a rule with an
    unacknowledged event already standing is considered "already firing" and is
    skipped, so running this every minute does not write a row every minute or
    ring the browser every minute. Acknowledging re-arms it.

    Returns:
        A summary dict - never an exception:

            {"portfolios", "scanned", "breached", "created", "suppressed",
             "broadcast", "skipped": [{"portfolio_id", "reason"}, ...]}

        `scanned` counts portfolios actually measured; portfolios with no
        active rules are neither scanned nor skipped - there was nothing to do.
    """
    portfolios = list(Portfolio.objects.order_by("pk"))
    if not portfolios:
        logger.debug("scan_all_alerts: no portfolios.")
        return _summary(0, 0, 0, 0, 0, 0, [])

    scanned = breached = created = suppressed = broadcast = 0
    skipped: list[dict] = []

    for portfolio in portfolios:
        # Cheap query, and it keeps compute_risk - which is the expensive part
        # of this whole pipeline - from running for a portfolio that has
        # nothing to compare its answer against.
        if not active_rules(portfolio.pk).exists():
            continue

        try:
            result = scan_and_emit(portfolio.pk)
        except DomainError as exc:
            # The four data errors compute_risk raises (empty portfolio,
            # missing prices, insufficient history, not found). Each already
            # carries a message naming its own fix.
            skipped.append({"portfolio_id": portfolio.pk, "reason": exc.message})
            logger.info(
                "scan_all_alerts: portfolio %s not measurable - %s", portfolio.pk, exc.message
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one portfolio must not sink the scan
            skipped.append(
                {"portfolio_id": portfolio.pk, "reason": f"{exc.__class__.__name__}: {exc}"}
            )
            logger.exception("scan_all_alerts: unexpected failure on portfolio %s", portfolio.pk)
            continue

        scanned += 1
        breached += result["breached"]
        created += result["created"]
        suppressed += result["suppressed"]
        broadcast += result["broadcast"]

    if created:
        logger.info(
            "scan_all_alerts: %s new alert(s) across %s portfolio(s), %s pushed.",
            created,
            scanned,
            broadcast,
        )
    if created and broadcast < created:
        # Stored but not delivered. The events exist and will arrive in the
        # next socket's connect snapshot; Redis is the thing to look at.
        logger.warning(
            "scan_all_alerts: %s event(s) saved but not pushed - is Redis reachable "
            "at settings.REDIS_URL?",
            created - broadcast,
        )

    return _summary(len(portfolios), scanned, breached, created, suppressed, broadcast, skipped)


def _summary(
    portfolios: int,
    scanned: int,
    breached: int,
    created: int,
    suppressed: int,
    broadcast: int,
    skipped: list[dict],
) -> dict:
    """The task's return shape, in one place so every path agrees on it."""
    return {
        "portfolios": portfolios,
        "scanned": scanned,
        "breached": breached,
        "created": created,
        "suppressed": suppressed,
        "broadcast": broadcast,
        "skipped": skipped,
    }
