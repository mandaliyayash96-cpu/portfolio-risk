"""
Scheduled market-data refresh (Phase 8).

This module adds NO fetching logic. `marketdata.services.fetch_live` and
`fetch_history` already know how to talk to the provider, how to upsert, and
how to fail; this is the thing that calls them on a timer. `manage.py
fetch_prices` calls exactly the same two functions and is unchanged - the
command is now one of two callers rather than the only one, which is why a
bug fixed in the services is fixed for both.

WHY THIS TASK NEVER RAISES
--------------------------
It returns a summary dict on every path, including total failure, and lets the
schedule be the retry. Three reasons, in order of how much they matter:

  1. yfinance failures are overwhelmingly rate limits and transient network
     errors. Celery's answer to a raised exception is a retry, and retrying
     into a rate limit is how a poll turns into a ban. The next beat tick is
     60 seconds away and carries fresher data than any retry would.
  2. A raised task is a FAILURE in the result backend and in flower. Reserve
     that for something a human should act on - "Yahoo was briefly unhappy at
     14:32" is not it, and a wall of red hides the failure that is.
  3. The worker must keep consuming. A task that raises does not kill a worker,
     but a task that raises on a schedule turns the log into noise nobody reads.

The summary it returns says exactly what happened per ticker, and anything that
went wrong is logged at WARNING or ERROR with the ticker in the message.

WHY A FAILED REFRESH CANNOT CORRUPT PRICES
------------------------------------------
Nothing here deletes. `fetch_live` writes a PriceSnapshot row only for tickers
the provider actually answered for, and `fetch_history` raises before it writes
anything when the frame comes back empty. So a poll that fails entirely leaves
every stored price exactly as the last good poll left it, and the dashboard
keeps showing the last close with its "last close" tag rather than a blank.
That is the same degradation `manage.py fetch_prices` has always had.

IDEMPOTENT
----------
Running this twice in a row is indistinguishable from running it once:
PriceSnapshot is unique on ticker (updated in place, latest wins) and
PriceHistory is unique on (ticker, date), which `fetch_history` upserts with
`update_conflicts=True`. That is what makes it safe for beat to fire it every
minute forever, and safe to fire by hand while beat is running.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from common.exceptions import ProviderError
from marketdata.services import DEFAULT_HISTORY_DAYS, fetch_history, fetch_live
from portfolio.selectors import get_all_held_tickers

logger = logging.getLogger(__name__)


def tickers_to_refresh() -> list[str]:
    """
    Every symbol the scheduled poll should fetch: all held tickers, plus the
    benchmark.

    The benchmark is not optional. `risk.engine` cannot compute beta without a
    return series to regress against, so a refresh that skipped it would leave
    the dashboard's beta card empty within a day of the last manual fetch.

    Mirrors the default path of `manage.py fetch_prices` (its `_resolve_tickers`
    with no flags). Deliberately not shared with that command: the command's
    version also has to honour --tickers and --no-benchmark, and folding four
    lines of set arithmetic into one function with two callers and two flag
    sets would be harder to read than either copy.
    """
    tickers = list(get_all_held_tickers())

    benchmark = (getattr(settings, "DEFAULT_BENCHMARK_TICKER", "") or "").strip().upper()
    if benchmark:
        tickers.append(benchmark)

    # De-duplicate, preserving order - the benchmark is often also a holding.
    return list(dict.fromkeys(tickers))


@shared_task(name="marketdata.refresh_all_prices")
def refresh_all_prices(days: int = DEFAULT_HISTORY_DAYS) -> dict:
    """
    Fetch live prices and daily history for every held ticker + the benchmark.

    Scheduled by settings.CELERY_BEAT_SCHEDULE every PRICE_REFRESH_SECONDS.
    Safe to call by hand at any time (see RUN.md).

    Args:
        days: trading days of history per ticker. The default matches the
            management command, so a scheduled refresh and a manual one write
            the same window.

    Returns:
        A summary dict - never an exception:

            {"ok", "tickers", "live_fetched", "live_failed", "history_written",
             "history_failed", "errors", "skipped"}

        `ok` is True when nothing failed at all. `errors` maps
        "TICKER (stage)" to the provider's message, so one symbol failing its
        history but not its live price reads as the two separate facts it is.
    """
    tickers = tickers_to_refresh()
    if not tickers:
        # Not an error, and not worth a warning every 60 seconds: an empty
        # database is the normal state of a fresh checkout.
        logger.debug("refresh_all_prices: nothing held and no benchmark configured.")
        return {
            "ok": True,
            "tickers": 0,
            "live_fetched": 0,
            "live_failed": 0,
            "history_written": 0,
            "history_failed": 0,
            "errors": {},
            "skipped": "no tickers",
        }

    days = max(int(days), 1)
    errors: dict[str, str] = {}

    # -- live prices -------------------------------------------------------
    # fetch_live collects per-ticker failures rather than raising them, so this
    # try only catches the whole-feed failures: a misconfigured provider, or an
    # unexpected exception from a transport library breaking the ProviderError
    # contract.
    live_fetched = live_failed = 0
    try:
        live = fetch_live(tickers)
        live_fetched, live_failed = live.fetched, live.failed
        for ticker, message in live.errors.items():
            errors[f"{ticker} (live)"] = message
    except ProviderError as exc:
        logger.warning("refresh_all_prices: live fetch unavailable - %s", exc.message)
        errors["* (live)"] = exc.message
        live_failed = len(tickers)
    except Exception as exc:  # noqa: BLE001 - see this module's docstring
        logger.exception("refresh_all_prices: unexpected failure fetching live prices")
        errors["* (live)"] = f"{exc.__class__.__name__}: {exc}"
        live_failed = len(tickers)

    # -- history -----------------------------------------------------------
    # One ticker at a time, each isolated. fetch_history DOES raise per ticker
    # (single-symbol call, the caller's error to handle) which is precisely why
    # the loop catches rather than the batch.
    history_written = history_failed = 0
    for ticker in tickers:
        try:
            result = fetch_history(ticker, days)
        except ProviderError as exc:
            logger.warning("refresh_all_prices: history failed for %s - %s", ticker, exc.message)
            errors[f"{ticker} (history)"] = exc.message
            history_failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one symbol must not sink the poll
            logger.exception("refresh_all_prices: unexpected failure on history for %s", ticker)
            errors[f"{ticker} (history)"] = f"{exc.__class__.__name__}: {exc}"
            history_failed += 1
            continue

        history_written += result.rows

    summary = {
        "ok": not errors,
        "tickers": len(tickers),
        "live_fetched": live_fetched,
        "live_failed": live_failed,
        "history_written": history_written,
        "history_failed": history_failed,
        "errors": errors,
        "skipped": None,
    }

    if errors:
        # One line at WARNING with the count, details already logged above. A
        # task that runs every minute must not write a paragraph every minute.
        logger.warning(
            "refresh_all_prices: %s of %s ticker(s) had a failure; stored prices "
            "for those are unchanged.",
            history_failed + live_failed,
            len(tickers),
        )
    else:
        logger.info(
            "refresh_all_prices: %s ticker(s), %s live price(s), %s history row(s).",
            len(tickers),
            live_fetched,
            history_written,
        )
    return summary
