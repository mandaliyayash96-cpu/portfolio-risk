"""
`alerts.tasks.scan_all_alerts`.

The scanning itself is Phase 6 and is already covered by test_services.py and
test_evaluator.py. What is new here is the SCHEDULING wrapper, so this file
tests only what the wrapper adds:

  * every portfolio is visited, and one that cannot be measured does not stop
    the rest;
  * an unmeasurable portfolio is a reported skip, NOT a task failure - the
    difference from `manage.py scan_alerts`, which exits non-zero on purpose;
  * the Phase 6 dedupe still holds when the caller is a scheduler firing every
    60 seconds rather than a human running a command once.

Like the marketdata task tests, the task is called as a plain function: no
broker, no worker, no eager-mode juggling.
"""

import pytest

from alerts.models import AlertEvent
from alerts.tasks import scan_all_alerts

pytestmark = pytest.mark.django_db


class TestEmptyCases:
    def test_no_portfolios_at_all(self, db):
        result = scan_all_alerts()

        assert result["portfolios"] == 0
        assert result["scanned"] == 0
        assert result["skipped"] == []

    def test_portfolio_with_no_rules_is_neither_scanned_nor_skipped(self, alert_portfolio):
        """
        Nothing to do is not the same as something went wrong. A portfolio with
        no rules must not appear in `skipped`, or the summary would report a
        problem every minute for a perfectly healthy install.
        """
        result = scan_all_alerts()

        assert result["portfolios"] == 1
        assert result["scanned"] == 0
        assert result["skipped"] == []

    def test_rules_but_no_price_data_is_a_skip_not_a_failure(self, saved_rule_factory):
        """
        The headline behavioural difference from the management command.

        `compute_risk` raises a DomainError for a portfolio holding nothing (or
        holding tickers nobody has fetched). The command turns that into a
        CommandError and a non-zero exit, which is right for a human. On a 60s
        schedule it would be an alarm that is permanently on, so the task
        reports it and succeeds.
        """
        saved_rule_factory()

        result = scan_all_alerts()  # must not raise

        assert result["scanned"] == 0
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"]


class TestScanning:
    """
    These need a portfolio that can actually be measured: holdings, enough
    overlapping price history to clear risk.services.MIN_OBSERVATIONS, and a
    live snapshot to value the positions with.

    Built here rather than borrowed from risk/tests/conftest.py, which is a
    sibling package whose fixtures this directory cannot see - and coupling the
    alert tests to the risk fixtures to save fifteen lines would mean a change
    to the risk suite could break this one for no reason anybody could see.

    IMPORTANT: this portfolio is NOT `alert_portfolio`. That one stays empty and
    unmeasurable, which is what makes the isolation test below a real test -
    one portfolio that works and one that cannot be measured, in the same scan.
    """

    @pytest.fixture
    def measurable(self, db, in_memory_channel_layer):
        """
        A priced portfolio, plus a channel layer that is not Redis.

        The in-memory layer matters: `scan_and_emit` broadcasts, and a suite
        that needed a running Redis to count alerts is a suite nobody runs.
        """
        from datetime import date, timedelta
        from decimal import Decimal

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        from marketdata.models import PriceHistory, PriceSnapshot
        from portfolio.models import Holding, Portfolio

        user = get_user_model().objects.create_user(username="scanned-investor", password="x")
        portfolio = Portfolio.objects.create(user=user, name="Scanned", base_currency="INR")

        # Weekdays only - a crude NSE calendar, and enough of them to clear the
        # 20-observation floor the risk service imposes on a return series.
        days: list[date] = []
        day = date(2026, 1, 5)
        while len(days) < 40:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)

        for ticker, base in (("RELIANCE.NS", 1000), ("TCS.NS", 2000), ("^NSEI", 22000)):
            if ticker != "^NSEI":
                Holding.objects.create(
                    portfolio=portfolio,
                    ticker=ticker,
                    quantity=Decimal("10"),
                    avg_buy_price=Decimal(base),
                    buy_date=days[0],
                )
            PriceHistory.objects.bulk_create(
                [
                    # A deterministic zig-zag: real movement (so volatility is
                    # not zero and the rule below can fire) with no randomness
                    # to make an assertion flap.
                    PriceHistory(
                        ticker=ticker,
                        date=day,
                        close=Decimal(base) + Decimal(index % 7) - Decimal(3),
                    )
                    for index, day in enumerate(days)
                ]
            )
            PriceSnapshot.objects.create(
                ticker=ticker, price=Decimal(base), timestamp=timezone.now()
            )

        return portfolio

    @pytest.fixture
    def firing_rule(self, measurable):
        """A rule any moving portfolio breaches - volatility above zero."""
        from decimal import Decimal

        from alerts.models import AlertMetric, AlertOperator, AlertRule

        return AlertRule.objects.create(
            portfolio=measurable,
            metric=AlertMetric.ANNUALIZED_VOLATILITY,
            operator=AlertOperator.GT,
            threshold=Decimal("0"),
            active=True,
        )

    def test_scans_and_creates_events(self, firing_rule):
        result = scan_all_alerts()

        assert result["scanned"] == 1
        assert result["breached"] >= 1
        assert result["created"] >= 1
        assert AlertEvent.objects.count() == result["created"]

    def test_second_run_suppresses_rather_than_duplicating(self, firing_rule):
        """
        The Phase 6 dedupe, under the condition Phase 8 actually creates: this
        now runs every 60 seconds, so without it a single standing breach would
        write 1,440 identical rows a day and ring the browser every minute.
        """
        first = scan_all_alerts()
        second = scan_all_alerts()

        assert second["created"] == 0
        assert second["suppressed"] >= 1
        assert AlertEvent.objects.count() == first["created"]

    def test_acknowledging_re_arms_the_rule(self, firing_rule):
        """Acknowledging is how you say "tell me again", scheduler or not."""
        scan_all_alerts()
        AlertEvent.objects.update(acknowledged=True)

        result = scan_all_alerts()

        assert result["created"] >= 1

    def test_one_unmeasurable_portfolio_does_not_stop_the_others(
        self, firing_rule, measurable, saved_rule_factory
    ):
        """
        Isolation, which is the whole reason this loop catches per portfolio.
        `saved_rule_factory` hangs its rule on a DIFFERENT, empty portfolio,
        so this scan has one measurable and one unmeasurable portfolio.
        """
        saved_rule_factory()

        result = scan_all_alerts()

        assert result["scanned"] == 1
        assert len(result["skipped"]) == 1
        assert result["created"] >= 1  # the good one still fired


class TestFailureContainment:
    def test_unexpected_exception_on_one_portfolio_is_contained(
        self, saved_rule_factory, monkeypatch
    ):
        """
        A DomainError is expected and handled. Anything else must be too - a
        task that dies on an unforeseen exception stops every later portfolio
        in the same run, every minute, silently.
        """
        import alerts.tasks as tasks

        saved_rule_factory()

        def boom(portfolio_id, **kwargs):
            raise RuntimeError("something nobody predicted")

        monkeypatch.setattr(tasks, "scan_and_emit", boom)

        result = scan_all_alerts()  # must not raise

        assert result["scanned"] == 0
        assert "RuntimeError" in result["skipped"][0]["reason"]


class TestRegistration:
    def test_task_is_registered_under_its_scheduled_name(self):
        from config.celery import app

        app.loader.import_default_modules()
        assert "alerts.scan_all_alerts" in app.tasks

    def test_scan_is_scheduled_after_the_price_refresh(self, settings):
        """
        The offset is the contract between the two jobs: the scan must be
        published with a countdown, or it measures the previous minute's
        prices. Asserted here because it is a one-word setting that would
        otherwise be silently droppable in a refactor.
        """
        entry = settings.CELERY_BEAT_SCHEDULE["scan-all-alerts"]

        assert entry["options"]["countdown"] == settings.ALERT_SCAN_OFFSET_SECONDS
        assert entry["options"]["countdown"] > 0


class TestManagementCommandStillWorks:
    """Requirement 8: `manage.py scan_alerts` is untouched and still behaves."""

    def test_command_runs_and_reports(self, alert_portfolio):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("scan_alerts", stdout=out, stderr=StringIO())

        assert "No portfolios to scan." not in out.getvalue()
        assert "no active rules" in out.getvalue()

    def test_command_still_exits_non_zero_on_an_unmeasurable_portfolio(
        self, saved_rule_factory
    ):
        """
        The command's contract is deliberately DIFFERENT from the task's: a
        human asking for a scan needs a non-zero exit when it could not be
        done. Pinned here so the task's softer behaviour cannot quietly leak
        into the command.
        """
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        saved_rule_factory()

        with pytest.raises(CommandError):
            call_command("scan_alerts", stdout=StringIO(), stderr=StringIO())
