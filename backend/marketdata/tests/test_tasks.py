"""
`marketdata.tasks.refresh_all_prices`.

Three things are worth proving here and nothing else is:

  1. It fetches the right SET of tickers - held plus the benchmark, deduped.
  2. It is IDEMPOTENT, because beat will run it every minute forever.
  3. It DEGRADES rather than raises, and a failed fetch leaves the last good
     prices in place. That is the "never crashes the worker, never corrupts
     stored prices" requirement, and it is the only reason this file is long.

What is NOT tested here: that fetch_live and fetch_history do their jobs. They
are the existing services and have their own contract; re-asserting it through
the task would just make this file fail twice for one bug.
"""

from datetime import date
from decimal import Decimal

import pytest

from marketdata.models import PriceHistory, PriceSnapshot
from marketdata.tasks import refresh_all_prices, tickers_to_refresh

pytestmark = pytest.mark.django_db


class TestTickerList:
    def test_held_tickers_plus_benchmark(self, held, benchmark):
        assert tickers_to_refresh() == ["RELIANCE.NS", "TCS.NS", benchmark]

    def test_benchmark_is_not_duplicated_when_also_held(self, holding_factory, benchmark):
        """The benchmark can be a position too - it must still be fetched once."""
        holding_factory(benchmark)
        holding_factory("TCS.NS")

        assert tickers_to_refresh().count(benchmark) == 1

    def test_benchmark_alone_when_nothing_is_held(self, db, benchmark):
        """
        An empty database still fetches the benchmark.

        Not a curiosity: beta needs the benchmark series to exist BEFORE the
        first holding is added, or the first risk report after that add has
        nothing to regress against.
        """
        assert tickers_to_refresh() == [benchmark]

    def test_no_tickers_at_all_is_handled(self, db, settings):
        settings.DEFAULT_BENCHMARK_TICKER = ""
        assert tickers_to_refresh() == []


class TestHappyPath:
    def test_writes_live_prices_and_history(self, held, benchmark, working_provider):
        result = refresh_all_prices()

        assert result["ok"] is True
        assert result["tickers"] == 3
        assert result["live_fetched"] == 3
        assert result["errors"] == {}

        for ticker in [*held, benchmark]:
            assert PriceSnapshot.objects.filter(ticker=ticker).exists()
            assert PriceHistory.objects.filter(ticker=ticker).exists()

    def test_empty_database_is_a_clean_no_op(self, db, settings):
        settings.DEFAULT_BENCHMARK_TICKER = ""

        result = refresh_all_prices()

        assert result["ok"] is True
        assert result["skipped"] == "no tickers"
        assert PriceSnapshot.objects.count() == 0

    def test_is_idempotent(self, held, benchmark, working_provider):
        """
        Beat fires this every 60 seconds forever, so a second run must not
        duplicate anything: PriceSnapshot is unique per ticker and PriceHistory
        upserts on (ticker, date).
        """
        refresh_all_prices()
        snapshots, history = PriceSnapshot.objects.count(), PriceHistory.objects.count()

        refresh_all_prices()

        assert PriceSnapshot.objects.count() == snapshots
        assert PriceHistory.objects.count() == history

    def test_history_rows_are_reported(self, held, benchmark, working_provider):
        result = refresh_all_prices(days=3)

        # 3 tickers x 3 days, and `rows` counts what was written this run.
        assert result["history_written"] == 9
        assert result["history_failed"] == 0


class TestGracefulDegradation:
    """
    The requirement in full: a fetch that fails logs and exits cleanly, never
    raises out of the task, and never damages what is already stored.
    """

    def test_one_bad_ticker_does_not_stop_the_others(
        self, holding_factory, benchmark, flaky_provider
    ):
        holding_factory("RELIANCE.NS")
        holding_factory("BROKEN.NS")

        result = refresh_all_prices()

        assert result["ok"] is False
        # The good ones landed.
        assert PriceSnapshot.objects.filter(ticker="RELIANCE.NS").exists()
        assert PriceHistory.objects.filter(ticker=benchmark).exists()
        # The bad one is reported, at both stages, named separately.
        assert "BROKEN.NS (live)" in result["errors"]
        assert "BROKEN.NS (history)" in result["errors"]
        assert not PriceSnapshot.objects.filter(ticker="BROKEN.NS").exists()

    def test_dead_feed_returns_a_summary_instead_of_raising(
        self, held, benchmark, dead_provider
    ):
        """
        The whole feed is down. The task must still RETURN - a raised task on a
        60s schedule is a retry storm against something already failing.
        """
        result = refresh_all_prices()

        assert result["ok"] is False
        assert result["live_fetched"] == 0
        assert result["history_failed"] == 3
        assert result["errors"]

    def test_dead_feed_leaves_previously_stored_prices_untouched(
        self, held, benchmark, settings
    ):
        """
        "Keep last good." A failed poll must not blank the dashboard - the
        stored close stays, and the UI keeps showing it with its "last close"
        tag rather than nothing at all.
        """
        from marketdata.tests.conftest import DEAD_PROVIDER, WORKING_PROVIDER

        settings.MARKET_DATA_PROVIDER = WORKING_PROVIDER
        refresh_all_prices()
        good_price = PriceSnapshot.objects.get(ticker="RELIANCE.NS").price
        good_rows = PriceHistory.objects.count()

        settings.MARKET_DATA_PROVIDER = DEAD_PROVIDER
        result = refresh_all_prices()

        assert result["ok"] is False
        assert PriceSnapshot.objects.get(ticker="RELIANCE.NS").price == good_price
        assert PriceHistory.objects.count() == good_rows

    def test_misconfigured_provider_does_not_raise(self, held, benchmark, settings):
        """
        A dotted path that does not import raises ImproperlyConfiguredProvider
        (a ProviderError) from get_provider - not from a ticker. It must be
        caught in exactly the same way, or one bad deploy takes the worker down
        every 60 seconds.
        """
        settings.MARKET_DATA_PROVIDER = "marketdata.providers.NoSuchProvider"

        result = refresh_all_prices()

        assert result["ok"] is False
        assert "* (live)" in result["errors"]

    def test_unexpected_exception_is_contained(self, held, benchmark, settings, monkeypatch):
        """
        The provider contract says only ProviderError escapes. If a transport
        library ever breaks that promise, the task still has to survive it -
        this is the guard that would otherwise be untested until production.
        """
        import marketdata.tasks as tasks

        def boom(*args, **kwargs):
            raise RuntimeError("transport library did something unexpected")

        monkeypatch.setattr(tasks, "fetch_live", boom)
        monkeypatch.setattr(tasks, "fetch_history", boom)

        result = refresh_all_prices()

        assert result["ok"] is False
        assert result["history_failed"] == 3

    def test_partial_failure_still_writes_what_worked(
        self, holding_factory, benchmark, flaky_provider
    ):
        """A poll is not all-or-nothing: what could be fetched is committed."""
        holding_factory("BROKEN.NS")
        holding_factory("TCS.NS")

        refresh_all_prices()

        assert PriceHistory.objects.filter(ticker="TCS.NS").exists()
        assert not PriceHistory.objects.filter(ticker="BROKEN.NS").exists()


class TestRegistration:
    """
    Celery-side wiring. Cheap to check, and the failure it catches - a beat
    entry naming a task that is not registered - is one where beat runs
    happily and nothing ever executes.
    """

    def test_task_is_registered_under_its_scheduled_name(self):
        from config.celery import app

        app.loader.import_default_modules()
        assert "marketdata.refresh_all_prices" in app.tasks

    def test_beat_schedule_points_at_a_real_task(self, settings):
        from config.celery import app

        app.loader.import_default_modules()
        for entry in settings.CELERY_BEAT_SCHEDULE.values():
            assert entry["task"] in app.tasks, f"{entry['task']} is scheduled but not registered"


class TestManagementCommandStillWorks:
    """
    Requirement 8: the manual command keeps working, because the task did not
    take anything away from it - both call the same two services.
    """

    def test_fetch_prices_command_runs_unchanged(self, held, benchmark, working_provider):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("fetch_prices", stdout=out, stderr=StringIO())

        assert "All tickers fetched." in out.getvalue()
        assert PriceSnapshot.objects.count() == 3

    def test_command_and_task_write_the_same_rows(self, held, benchmark, working_provider):
        from io import StringIO

        from django.core.management import call_command

        call_command("fetch_prices", stdout=StringIO(), stderr=StringIO())
        after_command = set(
            PriceHistory.objects.values_list("ticker", "date", "close")
        )

        refresh_all_prices()
        after_task = set(PriceHistory.objects.values_list("ticker", "date", "close"))

        assert after_task == after_command


def test_history_rows_are_upserted_not_duplicated(held, benchmark, working_provider):
    """
    The idempotency guarantee at the row level: the same (ticker, date) written
    twice is one row with a refreshed close, which is what makes a 60s schedule
    safe against a database that grows forever.
    """
    refresh_all_prices()
    PriceHistory.objects.filter(ticker="TCS.NS").update(close=Decimal("1.0000"))

    refresh_all_prices()

    assert PriceHistory.objects.filter(ticker="TCS.NS", date=date(2026, 1, 5)).count() == 1
    assert PriceHistory.objects.get(
        ticker="TCS.NS", date=date(2026, 1, 5)
    ).close != Decimal("1.0000")
