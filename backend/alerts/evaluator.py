"""
Rule evaluation: risk report in, breaches out.

This module is the "-ish" in pure-ish. `evaluate_rule` and everything below it
are ordinary functions over a dict and a model instance - no ORM query, no
network, no clock - which is why the whole comparison surface is tested without
a database or a running Redis (alerts/tests/test_evaluator.py). Only
`evaluate_rules` touches the world, and it does so through exactly two calls:
`compute_risk` for the numbers and `active_rules` for the rules.

REUSE, NOT REIMPLEMENTATION
---------------------------
The metric values come from `risk.services.compute_risk` - the same function
behind GET /api/risk/<id>/. Nothing here recomputes a VaR or a drawdown. That
matters more than it saves: an alert that fired on a number the dashboard never
showed would be worse than no alert, and the only way to guarantee they agree
is for there to be one calculation. `AlertMetric` values are literally keys of
that report, so the lookup is a dict access.

UNITS
-----
The report speaks RATIOS (0.0231 for a 2.31% VaR). Rules are written in whole
percent for the four percent-style metrics, because "-15" is what an investor
means by a 15% drawdown limit and "0.15" is not. The scaling between the two
lives in `METRICS` below and nowhere else - `AlertEvent.value` is stored already
scaled, so a stored event and its rule are always directly comparable and the
dashboard never has to know this conversion exists.

SIGNS
-----
Kept as the engine produces them, never absolute-valued:

    max_drawdown     negative   (-0.1842 is an 18.42% fall from the peak)
    var_historical   positive   (0.0231 is a 2.31% loss at 95% confidence)

So "alert me if drawdown gets worse than 15%" is `max_drawdown lt -15`, and
"alert me if VaR exceeds 2%" is `var_historical gt 2`. Both read the same
direction as the number on screen, which is the point.
"""

from __future__ import annotations

import logging
import operator as _operator
from dataclasses import dataclass
from typing import Callable

from alerts.models import AlertMetric, AlertOperator, AlertRule
from alerts.selectors import active_rules
from risk.services import compute_risk

logger = logging.getLogger(__name__)

#: A metric the report gives as a ratio but a rule states in whole percent.
PERCENT = "percent"
#: A metric that is already dimensionless: comparing it raw is correct.
UNITLESS = "unitless"


@dataclass(frozen=True)
class MetricSpec:
    """
    How to get one metric out of a risk report, and what unit it comes in.

    `extract` returns None - not 0.0 - when the report cannot supply the metric,
    which is a real case: `beta` is null whenever no benchmark history was
    stored. A missing metric must never be treated as a breach, and must never
    be treated as a pass either; the rule is simply not evaluated this scan.
    """

    label: str
    unit: str
    extract: Callable[[dict], float | None]

    def value_from(self, report: dict) -> float | None:
        """The metric in RULE units: scaled to percent, or left alone."""
        raw = self.extract(report)
        if raw is None:
            return None
        return float(raw) * 100.0 if self.unit == PERCENT else float(raw)

    def format(self, value: float) -> str:
        """Render a rule-unit value the way the alert message should read it."""
        return f"{value:.2f}%" if self.unit == PERCENT else f"{value:.2f}"


def _largest_weight(report: dict) -> float | None:
    """
    The biggest single position, as a fraction of the portfolio.

    Derived rather than read: the report exposes `hhi` for concentration, but
    HHI is a whole-portfolio dispersion number. "How much is riding on one
    name?" is a different question with a different answer - a portfolio of one
    40% holding and sixty 1% holdings has a modest HHI and an alarming top
    weight - so both are offered as separate metrics.
    """
    weights = report.get("weights") or {}
    values = [weight for weight in weights.values() if weight is not None]
    return max(values) if values else None


#: The single source of truth for what a rule can watch, where the value comes
#: from and what unit the threshold is written in. Adding a metric is a matter
#: of adding a choice in `AlertMetric` and a row here; nothing else changes.
METRICS: dict[str, MetricSpec] = {
    AlertMetric.VAR_HISTORICAL: MetricSpec(
        label="VaR (historical)",
        unit=PERCENT,
        extract=lambda report: report.get("var_historical"),
    ),
    AlertMetric.MAX_DRAWDOWN: MetricSpec(
        label="Max drawdown",
        unit=PERCENT,
        extract=lambda report: report.get("max_drawdown"),
    ),
    AlertMetric.ANNUALIZED_VOLATILITY: MetricSpec(
        label="Volatility (annualised)",
        unit=PERCENT,
        extract=lambda report: report.get("annualized_volatility"),
    ),
    AlertMetric.HHI: MetricSpec(
        label="Concentration (HHI)",
        unit=UNITLESS,
        extract=lambda report: report.get("hhi"),
    ),
    AlertMetric.CONCENTRATION: MetricSpec(
        label="Largest holding weight",
        unit=PERCENT,
        extract=_largest_weight,
    ),
    AlertMetric.BETA: MetricSpec(
        label="Beta vs benchmark",
        unit=UNITLESS,
        # None whenever the benchmark had no stored history. The report says so
        # in report["warnings"]; here it simply means "not evaluable today".
        extract=lambda report: report.get("beta"),
    ),
}

#: Comparison per operator. The stdlib functions are exactly the semantics the
#: choice labels promise, so there is nothing to get wrong by hand.
COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    AlertOperator.GT: _operator.gt,
    AlertOperator.GTE: _operator.ge,
    AlertOperator.LT: _operator.lt,
    AlertOperator.LTE: _operator.le,
}

#: How each operator reads inside a sentence about a breach.
_RELATION: dict[str, str] = {
    AlertOperator.GT: "above",
    AlertOperator.GTE: "at or above",
    AlertOperator.LT: "below",
    AlertOperator.LTE: "at or below",
}


@dataclass(frozen=True)
class Breach:
    """
    One rule that is currently violated.

    Carries the rule itself rather than just its id, so callers can write the
    event and address the channel group without going back to the database.

    Attributes:
        rule: the AlertRule that fired.
        metric: the AlertMetric value, i.e. the report key.
        current_value: the observed metric IN RULE UNITS (percent-scaled where
            the metric is percent-style), so it compares directly against
            `threshold` and is what gets stored on AlertEvent.value.
        threshold: the rule's threshold as a float, same units.
        message: one human sentence, ready for the feed.
    """

    rule: AlertRule
    metric: str
    current_value: float
    threshold: float
    message: str


def evaluate_rule(rule: AlertRule, report: dict) -> Breach | None:
    """
    Compare one rule against one report. Pure: no I/O, no ORM, no clock.

    Args:
        rule: an AlertRule. Need not be saved - the tests pass unsaved
            instances, which is the cheapest way to cover the comparison grid.
        report: a `compute_risk` result, or any dict carrying the same keys.

    Returns:
        A Breach when the rule is violated, None when it holds.

        None is also returned when the metric cannot be evaluated at all - an
        unknown metric string, or a value the report gives as null (beta with
        no benchmark). Both are logged rather than raised: one unevaluable rule
        must not abort a scan that has five other rules to check, and silently
        pretending the rule passed is not the same as knowing it did.
    """
    spec = METRICS.get(rule.metric)
    if spec is None:
        logger.warning(
            "Rule %s watches unknown metric %r; skipping. Known metrics: %s",
            rule.pk,
            rule.metric,
            ", ".join(sorted(METRICS)),
        )
        return None

    compare = COMPARISONS.get(rule.operator)
    if compare is None:
        logger.warning("Rule %s has unknown operator %r; skipping.", rule.pk, rule.operator)
        return None

    value = spec.value_from(report)
    if value is None:
        logger.info(
            "Rule %s (%s) not evaluated: the report has no value for this "
            "metric. See report['warnings'].",
            rule.pk,
            rule.metric,
        )
        return None

    threshold = float(rule.threshold)
    if not compare(value, threshold):
        return None

    return Breach(
        rule=rule,
        metric=str(rule.metric),
        current_value=value,
        threshold=threshold,
        message=(
            f"{spec.label} is {spec.format(value)}, "
            f"{_RELATION[rule.operator]} the {spec.format(threshold)} threshold."
        ),
    )


def evaluate_rules(portfolio_id: int, *, report: dict | None = None) -> list[Breach]:
    """
    Every active rule on one portfolio, checked against its current risk report.

    Args:
        portfolio_id: which portfolio to measure.
        report: an already-computed risk report, used instead of calling
            `compute_risk`. Two callers want this: the tests, which supply a
            literal dict and so need neither prices nor a database, and any
            future code path holding a fresh report that should not pay for the
            Monte Carlo leg a second time.

    Returns:
        The violated rules, in the order the rules were read. Empty when
        nothing is breached - and also empty when the portfolio has no active
        rules, in which case `compute_risk` is never called at all. That
        short-circuit is deliberate: scanning a portfolio nobody has written a
        rule for should cost one indexed query, not a Monte Carlo run.

    Raises:
        Whatever `compute_risk` raises when it has to be called: NotFoundError,
        EmptyPortfolioError, MissingPriceDataError, InsufficientHistoryError.
        These are deliberately NOT swallowed - "we could not measure this
        portfolio" is a different fact from "this portfolio is fine", and the
        scan command reports them per portfolio rather than letting a data gap
        read as an all-clear.
    """
    rules = list(active_rules(portfolio_id))
    if not rules:
        return []

    if report is None:
        report = compute_risk(portfolio_id)

    breaches = [
        breach for rule in rules if (breach := evaluate_rule(rule, report)) is not None
    ]
    logger.debug(
        "Portfolio %s: %s of %s active rule(s) breached.",
        portfolio_id,
        len(breaches),
        len(rules),
    )
    return breaches
