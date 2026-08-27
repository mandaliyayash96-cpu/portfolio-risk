"""
Alert rules and the events they fire.

Phase 1 stores the configuration only. Phase 6 adds the Celery Beat scan task
that recomputes each active rule's metric and the Channels consumer that pushes
new events to the dashboard.
"""

from django.db import models
from django.utils import timezone

from common.models import MONEY_DECIMAL_PLACES, MONEY_MAX_DIGITS, TimeStampedModel
from portfolio.models import Portfolio


class AlertMetric(models.TextChoices):
    PRICE_MOVE_PCT = "price_move_pct", "Price move %"
    VAR_BREACH = "var_breach", "VaR breach"
    DRAWDOWN_PCT = "drawdown_pct", "Drawdown %"
    CONCENTRATION_PCT = "concentration_pct", "Concentration %"
    STOP_LOSS = "stop_loss", "Stop loss"


class AlertOperator(models.TextChoices):
    GT = "gt", "greater than (>)"
    GTE = "gte", "greater than or equal (>=)"
    LT = "lt", "less than (<)"
    LTE = "lte", "less than or equal (<=)"


class AlertRule(TimeStampedModel):
    """
    "Notify me when <metric> <operator> <threshold> for this portfolio."

    Thresholds are Decimal and unit-dependent: percentages are whole numbers
    (5 means 5%), stop_loss is a price in the portfolio's base currency.
    """

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="alert_rules")
    metric = models.CharField(max_length=32, choices=AlertMetric)
    operator = models.CharField(max_length=3, choices=AlertOperator)
    threshold = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        help_text="Percent as a whole number (5 = 5%); stop_loss is an absolute price.",
    )
    active = models.BooleanField(default=True, db_index=True)
    # TODO Phase 6: optional `ticker` scope so stop_loss / price_move_pct can
    #               target a single holding instead of the whole portfolio.
    # TODO Phase 6: cooldown_minutes, to stop one breach firing on every scan.

    class Meta:
        ordering = ["portfolio", "metric"]
        indexes = [models.Index(fields=["active", "metric"], name="rule_active_metric_idx")]

    def __str__(self) -> str:
        return f"{self.portfolio.name}: {self.get_metric_display()} {self.operator} {self.threshold}"


class AlertEvent(TimeStampedModel):
    """A recorded breach of a rule. Created by the Phase 6 scan task."""

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="events")
    triggered_at = models.DateTimeField(default=timezone.now, db_index=True)
    value = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        help_text="The metric value observed at trigger time.",
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
