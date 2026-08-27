"""
Fixtures for the alerts tests.

The whole point of the split between `alerts.evaluator` and `alerts.services` is
that the interesting logic - which rules fire, and whether a firing rule should
write a second row - is testable without prices, without a market data provider
and without Redis. Nothing here starts a server or opens a socket.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from alerts.models import AlertEvent, AlertMetric, AlertOperator, AlertRule
from portfolio.models import Portfolio

#: A risk report as `compute_risk` returns one, trimmed to the keys the
#: evaluator reads. Values are RATIOS, exactly as the engine emits them, so the
#: percent scaling in alerts.evaluator.METRICS is exercised rather than
#: side-stepped:
#:
#:      var_historical          0.0231  ->    2.31 %
#:      max_drawdown           -0.1842  ->  -18.42 %
#:      annualized_volatility   0.2415  ->   24.15 %
#:      concentration (max w)   0.6200  ->   62.00 %
#:      hhi                     0.5012  ->    0.5012   (unitless)
#:      beta                    1.2400  ->    1.24     (unitless)
SAMPLE_REPORT = {
    "var_historical": 0.0231,
    "max_drawdown": -0.1842,
    "annualized_volatility": 0.2415,
    "hhi": 0.5012,
    "beta": 1.24,
    "weights": {"RELIANCE.NS": 0.62, "TCS.NS": 0.38},
}


@pytest.fixture
def report() -> dict:
    """A fresh copy per test, so a test mutating it cannot leak into the next."""
    return {**SAMPLE_REPORT, "weights": dict(SAMPLE_REPORT["weights"])}


@pytest.fixture
def rule_factory():
    """
    Build UNSAVED AlertRules.

    Unsaved on purpose: `evaluate_rule` is pure, so the comparison grid needs no
    database at all, and a test that touches one is a test that could fail for
    reasons unrelated to the comparison it is checking.
    """

    def _make(
        metric: str = AlertMetric.VAR_HISTORICAL,
        operator: str = AlertOperator.GT,
        threshold: str = "2",
        **kwargs,
    ) -> AlertRule:
        return AlertRule(
            metric=metric,
            operator=operator,
            threshold=Decimal(threshold),
            **kwargs,
        )

    return _make


@pytest.fixture
def alert_portfolio(db) -> Portfolio:
    """A saved portfolio for the tests that do need rows."""
    user = get_user_model().objects.create_user(username="alerts-investor", password="x")
    return Portfolio.objects.create(user=user, name="Alerting", base_currency="INR")


@pytest.fixture
def saved_rule_factory(alert_portfolio):
    """Persisted rules, for the service-layer tests."""

    def _make(
        metric: str = AlertMetric.VAR_HISTORICAL,
        operator: str = AlertOperator.GT,
        threshold: str = "2",
        active: bool = True,
    ) -> AlertRule:
        return AlertRule.objects.create(
            portfolio=alert_portfolio,
            metric=metric,
            operator=operator,
            threshold=Decimal(threshold),
            active=active,
        )

    return _make


@pytest.fixture
def in_memory_channel_layer(settings):
    """
    Swap Redis for the in-process layer.

    Assigning through pytest-django's `settings` fixture fires Django's
    setting_changed signal, and Channels' ChannelLayerManager listens for it and
    drops its cached backends - so this takes effect for code that has already
    imported `get_channel_layer`, and is undone at teardown.
    """
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }
    return settings.CHANNEL_LAYERS


@pytest.fixture
def open_event_factory(saved_rule_factory):
    """An unacknowledged event standing against a rule - the dedupe precondition."""

    def _make(rule=None, value: str = "2.3100", message: str = "already firing") -> AlertEvent:
        return AlertEvent.objects.create(
            rule=rule or saved_rule_factory(),
            value=Decimal(value),
            message=message,
        )

    return _make
