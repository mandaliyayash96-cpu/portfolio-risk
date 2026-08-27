"""
Manual market-data fetch: the Phase 2 way to exercise the provider without
Celery.

    python manage.py fetch_prices
    python manage.py fetch_prices --tickers RELIANCE.NS,TCS.NS --days 60
    python manage.py fetch_prices --skip-history
    python manage.py fetch_prices --no-benchmark

The benchmark (settings.DEFAULT_BENCHMARK_TICKER) is always fetched alongside
the held tickers, because Phase 3 cannot compute beta without it.

TODO Phase 6: Celery Beat calls marketdata.services.fetch_live /
fetch_history directly on a schedule; this command stays for local debugging.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from common.exceptions import ProviderError
from marketdata.services import DEFAULT_HISTORY_DAYS, fetch_history, fetch_live
from portfolio.selectors import get_all_held_tickers


class Command(BaseCommand):
    help = "Fetch live prices and daily history for every ticker held in a portfolio."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tickers",
            default="",
            help="Comma-separated symbols to fetch instead of the held ones.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_HISTORY_DAYS,
            help=f"Trading days of history per ticker (default {DEFAULT_HISTORY_DAYS}).",
        )
        parser.add_argument(
            "--skip-history",
            action="store_true",
            help="Fetch live prices only.",
        )
        parser.add_argument(
            "--skip-live",
            action="store_true",
            help="Fetch history only.",
        )
        parser.add_argument(
            "--no-benchmark",
            action="store_true",
            help=f"Skip the benchmark ({settings.DEFAULT_BENCHMARK_TICKER}), which is "
            "otherwise always fetched because Phase 3 needs it for beta.",
        )

    def handle(self, *args, **options):
        held = get_all_held_tickers()
        tickers = self._resolve_tickers(options, held)
        if not tickers:
            self.stdout.write(
                self.style.WARNING(
                    "No tickers to fetch. Add a Holding in the admin, or pass --tickers."
                )
            )
            return
        if not held and not (options["tickers"] or "").strip():
            self.stdout.write(
                self.style.WARNING(
                    "No holdings yet — fetching the benchmark only. "
                    "Add a Holding in the admin, or pass --tickers."
                )
            )

        days = max(int(options["days"]), 1)
        self.stdout.write(f"Fetching {len(tickers)} ticker(s): {', '.join(tickers)}")

        live = None
        if not options["skip_live"]:
            self.stdout.write("\nLive prices...")
            live = fetch_live(tickers)

        history: dict[str, object] = {}
        if not options["skip_history"]:
            self.stdout.write(f"\nDaily history ({days} trading days)...")
            for ticker in tickers:
                try:
                    history[ticker] = fetch_history(ticker, days)
                except ProviderError as exc:
                    history[ticker] = exc.message

        self._print_summary(tickers, live, history)

    # -- helpers ------------------------------------------------------------
    def _resolve_tickers(self, options, held: list[str]) -> list[str]:
        """
        What to fetch: the explicit --tickers list if given, otherwise every
        held ticker, plus the benchmark unless --no-benchmark was passed.

        The benchmark is included by default because risk metrics (beta above
        all) need its return series aligned with the portfolio's.
        """
        raw = (options["tickers"] or "").strip()
        explicit = [t.strip().upper() for t in raw.split(",") if t.strip()]
        tickers = explicit or list(held)

        if not options["no_benchmark"]:
            benchmark = (settings.DEFAULT_BENCHMARK_TICKER or "").strip().upper()
            if benchmark:
                tickers.append(benchmark)

        # De-duplicate, preserving order: the benchmark may also be a holding,
        # and --tickers can repeat a symbol.
        return list(dict.fromkeys(tickers))

    #: Column widths for the summary table.
    _COLS = (16, 14, 18, 26)

    def _print_summary(self, tickers, live, history) -> None:
        ticker_w, price_w, rows_w, range_w = self._COLS
        width = sum(self._COLS)

        self.stdout.write("\n" + "-" * width)
        self.stdout.write(
            f"{'TICKER':<{ticker_w}}{'LIVE PRICE':>{price_w}}"
            f"{'HISTORY ROWS':>{rows_w}}{'RANGE':>{range_w}}"
        )
        self.stdout.write("-" * width)

        failures = 0
        for ticker in tickers:
            price_cell = "-"
            if live is not None:
                if ticker in live.prices:
                    price_cell = f"{live.prices[ticker]:,}"
                else:
                    price_cell = "FAILED"
                    failures += 1

            rows_cell, range_cell = "-", ""
            result = history.get(ticker)
            if isinstance(result, str):  # error message
                rows_cell, range_cell = "FAILED", ""
                failures += 1
            elif result is not None:
                rows_cell = f"{result.rows} ({result.created} new)"
                range_cell = f"{result.first_date} - {result.last_date}"

            self.stdout.write(
                f"{ticker:<{ticker_w}}{price_cell:>{price_w}}"
                f"{rows_cell:>{rows_w}}{range_cell:>{range_w}}"
            )

        self.stdout.write("-" * width)

        # Labelled by stage: one bad ticker can fail both the live and the
        # history fetch, and seeing it listed twice unlabelled reads like a bug.
        errors: list[str] = []
        if live is not None:
            errors += [f"{ticker} (live): {message}" for ticker, message in live.errors.items()]
        errors += [
            f"{ticker} (history): {result}"
            for ticker, result in history.items()
            if isinstance(result, str)
        ]
        if errors:
            self.stdout.write(self.style.ERROR(f"\n{len(errors)} failure(s):"))
            for line in errors:
                self.stdout.write(self.style.ERROR(f"  - {line}"))

        if failures == 0:
            self.stdout.write(self.style.SUCCESS("\nAll tickers fetched."))
