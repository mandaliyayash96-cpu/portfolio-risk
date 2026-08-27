"""
Evaluator tests: does the right rule fire, on the right side of the threshold?

Almost every test here runs without a database, because `evaluate_rule` is a
pure function of (rule, report). That is the payoff for keeping compute_risk out
of it - the comparison grid, the unit scaling and the sign conventions are all
checked against a literal dict in microseconds, with no prices to fetch and no
Redis to have running.

The three at the bottom that DO use the database are testing `evaluate_rules`,
whose job is exactly the two impure things: reading rules and deciding whether
compute_risk needs calling at all.
"""

from decimal import Decimal

import pytest

from alerts import evaluator
from alerts.evaluator import METRICS, Breach, evaluate_rule, evaluate_rules
from alerts.models import AlertMetric, AlertOperator


# ---------------------------------------------------------------------------
# Metric extraction and unit scaling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        # Percent metrics: report ratio * 100.
        (AlertMetric.VAR_HISTORICAL, 2.31),
        (AlertMetric.MAX_DRAWDOWN, -18.42),
        (AlertMetric.ANNUALIZED_VOLATILITY, 24.15),
        (AlertMetric.CONCENTRATION, 62.0),
        # Unitless metrics: passed through untouched.
        (AlertMetric.HHI, 0.5012),
        (AlertMetric.BETA, 1.24),
    ],
)
def test_metric_value_is_scaled_into_rule_units(report, metric, expected):
    assert METRICS[metric].value_from(report) == pytest.approx(expected)


def test_every_model_choice_has_a_metric_spec():
    """
    The dropdown and the evaluator must not be able to drift apart.

    A choice with no spec is a rule you can save from the admin that silently
    never fires, which is the single worst failure mode this app has: the user
    believes they are being watched and they are not.
    """
    assert {choice.value for choice in AlertMetric} == set(METRICS)


def test_concentration_is_the_largest_weight_not_the_sum(report):
    report["weights"] = {"A": 0.5, "B": 0.3, "C": 0.2}
    assert METRICS[AlertMetric.CONCENTRATION].value_from(report) == pytest.approx(50.0)


def test_concentration_is_none_when_the_report_has_no_weights(report):
    report["weights"] = {}
    assert METRICS[AlertMetric.CONCENTRATION].value_from(report) is None


# ---------------------------------------------------------------------------
# The comparison grid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("operator", "threshold", "breaches"),
    [
        # Observed var_historical is 2.31%.
        (AlertOperator.GT, "2", True),  # 2.31 >  2     -> fires
        (AlertOperator.GT, "2.31", False),  # 2.31 >  2.31  -> holds (strict)
        (AlertOperator.GT, "3", False),  # 2.31 >  3     -> holds
        (AlertOperator.GTE, "2.31", True),  # 2.31 >= 2.31  -> fires (boundary)
        (AlertOperator.GTE, "2.32", False),
        (AlertOperator.LT, "3", True),  # 2.31 <  3     -> fires
        (AlertOperator.LT, "2.31", False),  # strict again
        (AlertOperator.LTE, "2.31", True),  # boundary
        (AlertOperator.LTE, "2", False),
    ],
)
def test_operator_comparison(rule_factory, report, operator, threshold, breaches):
    rule = rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=operator, threshold=threshold
    )
    result = evaluate_rule(rule, report)

    assert (result is not None) is breaches
    if breaches:
        assert isinstance(result, Breach)
        assert result.current_value == pytest.approx(2.31)
        assert result.threshold == pytest.approx(float(Decimal(threshold)))


def test_breach_carries_the_rule_and_metric(rule_factory, report):
    rule = rule_factory(metric=AlertMetric.HHI, operator=AlertOperator.GT, threshold="0.3")
    breach = evaluate_rule(rule, report)

    assert breach is not None
    assert breach.rule is rule
    assert breach.metric == AlertMetric.HHI
    assert breach.current_value == pytest.approx(0.5012)


# ---------------------------------------------------------------------------
# Signs - the convention that makes a rule read like the dashboard
# ---------------------------------------------------------------------------
def test_drawdown_worse_than_the_limit_fires_on_lt(rule_factory, report):
    """-18.42% is a WORSE drawdown than -15%, and worse means numerically lower."""
    rule = rule_factory(
        metric=AlertMetric.MAX_DRAWDOWN, operator=AlertOperator.LT, threshold="-15"
    )
    assert evaluate_rule(rule, report) is not None


def test_drawdown_inside_the_limit_holds(rule_factory, report):
    rule = rule_factory(
        metric=AlertMetric.MAX_DRAWDOWN, operator=AlertOperator.LT, threshold="-25"
    )
    assert evaluate_rule(rule, report) is None


def test_drawdown_is_not_absolute_valued(rule_factory, report):
    """
    A `gt 15` drawdown rule must NOT fire on -18.42%.

    Worth pinning: absolute-valuing the drawdown somewhere in the pipeline would
    make this pass, and would also invert every drawdown rule anyone writes.
    """
    rule = rule_factory(
        metric=AlertMetric.MAX_DRAWDOWN, operator=AlertOperator.GT, threshold="15"
    )
    assert evaluate_rule(rule, report) is None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def test_message_states_the_value_the_direction_and_the_threshold(rule_factory, report):
    rule = rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    breach = evaluate_rule(rule, report)

    assert breach.message == "VaR (historical) is 2.31%, above the 2.00% threshold."


def test_unitless_messages_carry_no_percent_sign(rule_factory, report):
    rule = rule_factory(metric=AlertMetric.BETA, operator=AlertOperator.GT, threshold="1.1")
    breach = evaluate_rule(rule, report)

    assert breach.message == "Beta vs benchmark is 1.24, above the 1.10 threshold."
    assert "%" not in breach.message


# ---------------------------------------------------------------------------
# Metrics that cannot be evaluated - never a breach, never a silent pass
# ---------------------------------------------------------------------------
def test_null_beta_does_not_fire(rule_factory, report):
    """
    No benchmark history means beta is null in the report.

    A rule watching it must be skipped, not fired. `beta > 1.1` against a null
    is not true, and treating a missing measurement as a breach would page
    somebody every scan for as long as the benchmark is unfetched.
    """
    report["beta"] = None
    rule = rule_factory(metric=AlertMetric.BETA, operator=AlertOperator.GT, threshold="1.1")

    assert evaluate_rule(rule, report) is None


def test_null_beta_does_not_fire_a_less_than_rule_either(rule_factory, report):
    """The other direction, where "missing means zero" would look like a breach."""
    report["beta"] = None
    rule = rule_factory(metric=AlertMetric.BETA, operator=AlertOperator.LT, threshold="0.5")

    assert evaluate_rule(rule, report) is None


def test_unknown_metric_is_skipped_not_raised(rule_factory, report):
    rule = rule_factory(metric="sharpe_ratio", operator=AlertOperator.GT, threshold="1")
    assert evaluate_rule(rule, report) is None


def test_unknown_operator_is_skipped_not_raised(rule_factory, report):
    rule = rule_factory(operator="ne", threshold="2")
    assert evaluate_rule(rule, report) is None


# ---------------------------------------------------------------------------
# evaluate_rules - the impure half
# ---------------------------------------------------------------------------
def test_evaluate_rules_reads_active_rules_and_returns_breaches(
    db, saved_rule_factory, report
):
    breaching = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL, operator=AlertOperator.GT, threshold="2"
    )
    saved_rule_factory(
        metric=AlertMetric.ANNUALIZED_VOLATILITY, operator=AlertOperator.GT, threshold="90"
    )

    breaches = evaluate_rules(breaching.portfolio_id, report=report)

    assert [breach.rule.pk for breach in breaches] == [breaching.pk]


def test_inactive_rules_are_not_evaluated(db, saved_rule_factory, report):
    rule = saved_rule_factory(
        metric=AlertMetric.VAR_HISTORICAL,
        operator=AlertOperator.GT,
        threshold="2",
        active=False,
    )
    assert evaluate_rules(rule.portfolio_id, report=report) == []


def test_no_rules_means_compute_risk_is_never_called(
    db, alert_portfolio, monkeypatch
):
    """
    A portfolio nobody has written a rule for must cost one query, not a report.

    compute_risk runs a 10,000-path Monte Carlo. Scanning every portfolio on a
    schedule would be pointlessly expensive if it built a report first and asked
    whether anyone cared second.
    """

    def explode(*args, **kwargs):
        raise AssertionError("compute_risk must not be called when there are no rules")

    monkeypatch.setattr(evaluator, "compute_risk", explode)

    assert evaluate_rules(alert_portfolio.pk) == []
