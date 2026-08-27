"""
Alert rules and the events they fire.

Phase 6 makes these live: `alerts.evaluator` recomputes each active rule's
metric from the risk report, `alerts.services.scan_and_emit` records breaches as
AlertEvents, and `alerts.consumers` pushes them to the dashboard over a
WebSocket. Scans are triggered by hand tonight (`manage.py scan_alerts`).
"""

from django.db import models
from django.utils import timezone

from common.models import MONEY_DECIMAL_PLACES, MONEY_MAX_DIGITS, TimeStampedModel
from portfolio.models import Portfolio


class AlertMetric(models.TextChoices):
    """
    What a rule watches. Every value is a key of the risk report that
    `risk.services.compute_risk` already returns, so `alerts.evaluator` reads
    the metric straight out of the report with no translation table in between
    - one name, defined once, all the way from the dropdown to the engine.

    `concentration` is the one exception: it is derived from report["weights"]
    (the largest single holding) rather than being a top-level key, because HHI
    answers "how spread out is this portfolio" while an investor asking about
    concentration usually means "how much is riding on one name".

    Two metrics from the Phase 1 sketch are deliberately absent. price_move_pct
    and stop_loss are per-TICKER questions, and a rule has no ticker field to
    scope them with (see the TODO below); offering them in the dropdown would
    let you save a rule that could never fire.
    """

    VAR_HISTORICAL = "var_historical", "VaR (historical)"
    MAX_DRAWDOWN = "max_drawdown", "Max drawdown"
    ANNUALIZED_VOLATILITY = "annualized_volatility", "Volatility (annualised)"
    HHI = "hhi", "Concentration (HHI)"
    CONCENTRATION = "concentration", "Largest holding weight"
    BETA = "beta", "Beta vs benchmark"


class AlertOperator(models.TextChoices):
    GT = "gt", "greater than (>)"
    GTE = "gte", "greater than or equal (>=)"
    LT = "lt", "less than (<)"
    LTE = "lte", "less than or equal (<=)"


class AlertRule(TimeStampedModel):
    """
    "Notify me when <metric> <operator> <threshold> for this portfolio."

    Thresholds are Decimal and unit-dependent, and the unit follows the METRIC:

        percent metrics  var_historical, max_drawdown, annualized_volatility,
                         concentration
                         -> whole numbers. 5 means 5%, -15 means -15%.
        unitless metrics hhi, beta
                         -> the bare number. 0.30 means an HHI of 0.30.

    The risk report emits every one of these as a RATIO (0.05, not 5), so
    `alerts.evaluator` scales the percent ones by 100 before comparing. That
    conversion lives in exactly one table there - `alerts.evaluator.METRICS` -
    and nothing else in the codebase is allowed a second opinion about units.

    Signs are kept, not absolute-valued: max_drawdown is negative in the report
    and negative here, so "drawdown worse than 15%" is written `lt -15`, which
    reads the same way the number does on the dashboard.
    """

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="alert_rules")
    metric = models.CharField(max_length=32, choices=AlertMetric)
    operator = models.CharField(max_length=3, choices=AlertOperator)
    threshold = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        help_text=(
            "Percent as a whole number for var_historical, max_drawdown, "
            "annualized_volatility and concentration (5 = 5%, -15 = -15%); "
            "the bare number for hhi and beta."
        ),
    )
    active = models.BooleanField(default=True, db_index=True)
    # TODO Phase-later: optional `ticker` scope, which is what stop_loss and
    #                   price_move_pct need before they can come back as
    #                   metrics - both are questions about one holding.
    # TODO Phase-later: cooldown_minutes. Re-firing is currently prevented by
    #                   the open-event dedupe in alerts.services.scan_and_emit
    #                   (no second event while the first is unacknowledged),
    #                   which is a coarser rule: it never re-fires at all until
    #                   someone clicks Acknowledge.

    class Meta:
        ordering = ["portfolio", "metric"]
        indexes = [models.Index(fields=["active", "metric"], name="rule_active_metric_idx")]

    def __str__(self) -> str:
        return f"{self.portfolio.name}: {self.get_metric_display()} {self.operator} {self.threshold}"


class AlertEvent(TimeStampedModel):
    """
    A recorded breach of a rule, written by `alerts.services.scan_and_emit`.

    `acknowledged` does double duty: it is the read/unread flag the dashboard
    renders, and it is the dedupe key. A rule with an unacknowledged event is
    considered "already firing", so repeated scans over the same breach add
    nothing - see scan_and_emit.
    """

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="events")
    triggered_at = models.DateTimeField(default=timezone.now, db_index=True)
    value = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        help_text=(
            "The metric value observed at trigger time, in the SAME units as "
            "rule.threshold - so the two are directly comparable in the feed."
        ),
    )
    message = models.TextField(help_text="Human-readable summary shown in the alert feed.")
    acknowledged = models.BooleanField(default=False)

    class Meta:
        ordering = ["-triggered_at"]
        indexes = [
            models.Index(fields=["acknowledged", "-triggered_at"], name="event_ack_ts_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.triggered_at:%Y-%m-%d %H:%M}] {self.message}"
