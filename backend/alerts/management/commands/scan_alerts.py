"""
Manual alert scan: the Phase 6 way to fire the pipeline without Celery.

    python manage.py scan_alerts
    python manage.py scan_alerts --portfolio 1
    python manage.py scan_alerts --dry-run

Every scanned portfolio is measured with `risk.services.compute_risk` - the same
call behind GET /api/risk/<id>/ - so an alert can never disagree with the number
on the dashboard. New breaches become AlertEvent rows and are pushed to any
socket watching that portfolio, which is the visible half: run this with the
dashboard open and the feed updates while the command is still printing.

TODO Phase-later: Celery Beat calls alerts.services.scan_and_emit on a schedule
(every 60s alongside the price poll). This command stays for local debugging,
exactly as `fetch_prices` did after the poll task landed.

ONE PORTFOLIO'S FAILURE IS NOT THE SCAN'S FAILURE
-------------------------------------------------
A portfolio with no holdings, or one holding a ticker nobody has fetched prices
for, raises a DomainError out of compute_risk. Caught per portfolio and reported
as a skip: with several portfolios configured, the first unfetched ticker must
not stop the other portfolios being measured. The exit code still reflects it -
a scan that could not measure everything it was asked to has not fully
succeeded, and a scheduler needs to be able to see that.
"""

from django.core.management.base import BaseCommand, CommandError

from alerts.evaluator import evaluate_rules
from alerts.selectors import active_rules
from alerts.services import scan_and_emit
from common.exceptions import DomainError
from portfolio.models import Portfolio


class Command(BaseCommand):
    help = "Evaluate every active alert rule and push new breaches to the dashboard."

    def add_arguments(self, parser):
        parser.add_argument(
            "--portfolio",
            type=int,
            default=None,
            help="Scan only this portfolio id (default: every portfolio).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would fire without writing events or pushing them.",
        )

    def handle(self, *args, **options):
        portfolio_id = options["portfolio"]
        dry_run = options["dry_run"]

        portfolios = self._resolve_portfolios(portfolio_id)
        if not portfolios:
            self.stdout.write(self.style.WARNING("No portfolios to scan."))
            return

        total_created = 0
        total_breached = 0
        total_suppressed = 0
        skipped: list[tuple[int, str]] = []

        for portfolio in portfolios:
            label = f"[{portfolio.pk}] {portfolio.name}"

            rule_count = active_rules(portfolio.pk).count()
            if rule_count == 0:
                # Said out loud rather than passed over in silence: "no alerts
                # fired" and "no rules exist to fire" look identical in the feed
                # and are very different problems.
                self.stdout.write(f"{label}: no active rules, skipped.")
                continue

            try:
                if dry_run:
                    breaches = evaluate_rules(portfolio.pk)
                    total_breached += len(breaches)
                    self._report_dry_run(label, rule_count, breaches)
                    continue

                result = scan_and_emit(portfolio.pk)
            except DomainError as exc:
                # The four data errors compute_risk raises, each of which
                # already carries a message naming its own fix.
                skipped.append((portfolio.pk, exc.message))
                self.stderr.write(self.style.WARNING(f"{label}: {exc.message}"))
                continue

            total_breached += result["breached"]
            total_created += result["created"]
            total_suppressed += result["suppressed"]
            self._report_scan(label, rule_count, result)

        self._report_totals(
            dry_run=dry_run,
            portfolios=len(portfolios),
            breached=total_breached,
            created=total_created,
            suppressed=total_suppressed,
            skipped=skipped,
        )

    # -- helpers -------------------------------------------------------------
    def _resolve_portfolios(self, portfolio_id: int | None) -> list[Portfolio]:
        if portfolio_id is None:
            return list(Portfolio.objects.order_by("pk"))

        portfolio = Portfolio.objects.filter(pk=portfolio_id).first()
        if portfolio is None:
            # CommandError, not a printed warning: an explicit --portfolio for
            # an id that does not exist is a typo, and exiting non-zero is how
            # the caller finds out.
            raise CommandError(f"Portfolio {portfolio_id} does not exist.")
        return [portfolio]

    def _report_dry_run(self, label: str, rule_count: int, breaches: list) -> None:
        if not breaches:
            self.stdout.write(f"{label}: {rule_count} rule(s), nothing breached.")
            return
        self.stdout.write(
            self.style.WARNING(f"{label}: {len(breaches)} of {rule_count} rule(s) breached")
        )
        for breach in breaches:
            self.stdout.write(f"    would fire - {breach.message}")

    def _report_scan(self, label: str, rule_count: int, result: dict) -> None:
        created, suppressed = result["created"], result["suppressed"]

        if not result["breached"]:
            self.stdout.write(f"{label}: {rule_count} rule(s), nothing breached.")
            return

        summary = f"{label}: {result['breached']} of {rule_count} rule(s) breached"
        if suppressed:
            summary += f", {suppressed} already open"
        self.stdout.write(self.style.WARNING(summary))

        for event in result["events"]:
            self.stdout.write(self.style.ERROR(f"    fired  - {event['message']}"))

        if created and result["broadcast"] < created:
            # Stored but not delivered. Worth its own line: the dashboard will
            # look wrong until someone reloads it, and Redis is the reason.
            self.stderr.write(
                self.style.WARNING(
                    f"    {created - result['broadcast']} event(s) saved but not "
                    "pushed - is Redis reachable at settings.REDIS_URL?"
                )
            )

    def _report_totals(
        self,
        *,
        dry_run: bool,
        portfolios: int,
        breached: int,
        created: int,
        suppressed: int,
        skipped: list[tuple[int, str]],
    ) -> None:
        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run: {breached} breach(es) across {portfolios} portfolio(s). "
                    "Nothing written, nothing pushed."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Scanned {portfolios} portfolio(s): {breached} breach(es), "
                    f"{created} new event(s), {suppressed} suppressed as already open."
                )
            )

        if skipped:
            self.stderr.write(
                self.style.WARNING(f"{len(skipped)} portfolio(s) could not be measured.")
            )
            raise CommandError(
                "Scan finished with "
                f"{len(skipped)} unmeasurable portfolio(s): "
                f"{', '.join(str(pk) for pk, _ in skipped)}."
            )
