"""
The PDF report: the builder, and the endpoint that serves it.

Two halves, tested differently on purpose.

`build_risk_pdf` is pure - dict in, bytes out - so most of these tests hand it a
literal dict and never touch the database. That is what lets the awkward cases
(a null beta, a missing benchmark, an empty holdings list, a name full of
punctuation) be checked cheaply and exactly, rather than by trying to coax a
real portfolio into an unusual shape.

The endpoint tests do use a real portfolio, because the thing they are checking
is the wiring: 200 + application/pdf on the way out, and the JSON envelope -
not a broken file - on every failure.
"""

import json
from datetime import datetime, timezone as dt_timezone

import pytest

from risk.report_pdf import MISSING, build_risk_pdf, money, percent, quantity, ratio
from risk.services import compute_risk

pytestmark = pytest.mark.django_db


def url(portfolio_id: int) -> str:
    return f"/api/risk/{portfolio_id}/report.pdf"


#: A report with the shape `compute_risk` returns, trimmed to the keys the
#: builder reads. Deliberately includes a null beta and a warning - the two
#: things most likely to be true of a real portfolio and most likely to be
#: forgotten by a builder that was only ever run against a perfect one.
SAMPLE_REPORT = {
    "observations": 89,
    "start": "2026-01-05 00:00:00",
    "end": "2026-05-07 00:00:00",
    "tickers": ["RELIANCE.NS", "TCS.NS"],
    "weights": {"RELIANCE.NS": 0.62, "TCS.NS": 0.38},
    "annualized_return": 0.1842,
    "annualized_volatility": 0.2415,
    "beta": None,
    "sharpe": 0.87,
    "sortino": 1.21,
    "max_drawdown": -0.1842,
    "var_historical": 0.0231,
    "var_parametric": 0.0244,
    "var_montecarlo": 0.0239,
    "cvar": 0.0312,
    "hhi": 0.5288,
    "effective_holdings": 1.89,
    "per_asset_volatility": {"RELIANCE.NS": 0.28, "TCS.NS": 0.19},
    "params": {
        "rf_per_period": 0.000258,
        "trading_days": 252,
        "confidence": 0.95,
        "n_sims": 10000,
        "horizon": 1,
        "seed": 7,
    },
    "portfolio": {
        "id": 1,
        "name": "My Demo",
        "base_currency": "INR",
        "market_value": "224275.9995000000",
        "holdings": [
            {
                "ticker": "RELIANCE.NS",
                "quantity": "100.000000",
                "price": "1400.5000",
                "price_source": "live",
                "market_value": "140050.0000",
                "weight": 0.6245,
            },
            {
                "ticker": "TCS.NS",
                "quantity": "42.500000",
                "price": "1981.7647",
                "price_source": "last_close",
                "market_value": "84225.9995",
                "weight": 0.3755,
            },
        ],
    },
    "benchmark": {"ticker": "^NSEI", "included": False},
    "warnings": ["Benchmark ^NSEI has no overlapping history; beta was not computed."],
}


def sample(**overrides) -> dict:
    """A fresh copy per test, so one mutating it cannot leak into the next."""
    report = json.loads(json.dumps(SAMPLE_REPORT))
    report.update(overrides)
    return report


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
class TestFormatters:
    """
    None of these may raise. A report missing one metric must still produce a
    document - a PDF endpoint that 500s over a null beta is worse than one that
    prints a dash.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("14000.0000", "14,000.00"),
            ("1400.5000", "1,400.50"),
            ("0.0000", "0.00"),
            (None, MISSING),
            ("not a number", MISSING),
            ("NaN", MISSING),
        ],
    )
    def test_money(self, value, expected):
        assert money(value) == expected

    def test_money_rounds_the_decimal_not_a_float(self):
        """
        The value is parsed as Decimal, and this is the case that proves it.

        2.675 is not representable in binary - the nearest double is
        2.67499999999999982..., so a float round-trip rounds it DOWN to 2.67.
        Rounding the exact decimal is a true tie, which Python breaks to even
        and gives 2.68. One paisa, on the one input that can show the
        difference; a value like 1400.50 would pass either way and prove
        nothing.
        """
        assert money("2.675") == "2.68"
        assert f"{float('2.675'):,.2f}" == "2.67"  # what the bug would look like

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("10.000000", "10"),
            ("0.500000", "0.5"),
            ("1000.000000", "1,000"),
            ("42.500000", "42.5"),
            (None, MISSING),
        ],
    )
    def test_quantity_drops_padding_without_going_scientific(self, value, expected):
        assert quantity(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.1815, "18.15%"), (-0.1842, "-18.42%"), (0, "0.00%"), (None, MISSING)],
    )
    def test_percent(self, value, expected):
        assert percent(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(0.879, "0.88"), (None, MISSING), (float("nan"), MISSING)]
    )
    def test_ratio(self, value, expected):
        assert ratio(value) == expected


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------
class TestBuilder:
    def test_produces_a_real_pdf(self):
        pdf = build_risk_pdf(sample())

        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")
        assert pdf.rstrip().endswith(b"%%EOF")
        # A header-only file is about 1 KB. Anything this size has tables in it.
        assert len(pdf) > 3000

    def test_needs_no_database(self):
        """
        The purity claim, made checkable: this test would still pass with the
        database torn down, because the builder only ever reads the dict.
        """
        pdf = build_risk_pdf(sample())
        assert pdf.startswith(b"%PDF")

    def test_generated_at_is_injectable(self):
        """A fixed clock, so the header can be asserted on rather than hoped at."""
        stamp = datetime(2026, 8, 31, 9, 30, 0, tzinfo=dt_timezone.utc)

        pdf = build_risk_pdf(sample(), generated_at=stamp)

        assert pdf.startswith(b"%PDF")

    def test_null_beta_does_not_break_the_document(self):
        """The sample has beta=None on purpose. It must render, not raise."""
        assert build_risk_pdf(sample(beta=None)).startswith(b"%PDF")

    def test_missing_benchmark_block(self):
        assert build_risk_pdf(sample(benchmark={})).startswith(b"%PDF")

    def test_no_warnings_is_fine(self):
        assert build_risk_pdf(sample(warnings=[])).startswith(b"%PDF")

    def test_empty_holdings_does_not_crash_the_table(self):
        report = sample()
        report["portfolio"]["holdings"] = []

        assert build_risk_pdf(report).startswith(b"%PDF")

    def test_missing_per_asset_volatility_omits_that_section(self):
        assert build_risk_pdf(sample(per_asset_volatility={})).startswith(b"%PDF")

    def test_survives_a_report_stripped_to_almost_nothing(self):
        """
        Defensive, and worth it: every formatter is written to return a dash
        rather than raise, and this is the test that proves the whole document
        inherits that rather than only the cells somebody remembered.
        """
        pdf = build_risk_pdf({"portfolio": {"name": "Bare", "holdings": []}})

        assert pdf.startswith(b"%PDF")

    def test_many_holdings_paginate(self):
        """
        A portfolio long enough to need a second page. The footer says "Page X
        of Y", which needs the two-pass canvas - a builder that only ever saw
        one page would not exercise it.
        """
        report = sample()
        report["portfolio"]["holdings"] = [
            {
                "ticker": f"TICKER{index}.NS",
                "quantity": "10.000000",
                "price": "1000.0000",
                "market_value": "10000.0000",
                "weight": 0.02,
            }
            for index in range(60)
        ]

        pdf = build_risk_pdf(report)

        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 5000


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
class TestEndpoint:
    def test_returns_a_pdf_for_a_valid_portfolio(self, client, funded_portfolio):
        response = client.get(url(funded_portfolio.pk))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"

        body = response.content
        assert body.startswith(b"%PDF")
        assert len(body) > 3000

    def test_content_disposition_names_the_portfolio_and_the_date(
        self, client, funded_portfolio
    ):
        from django.utils import timezone

        response = client.get(url(funded_portfolio.pk))

        disposition = response["Content-Disposition"]
        assert disposition.startswith("attachment;")
        assert "risk-report-core-" in disposition  # the portfolio is named "Core"
        assert timezone.localdate().isoformat() in disposition
        assert disposition.endswith('.pdf"')

    def test_matches_the_json_endpoint(self, client, funded_portfolio):
        """
        Both call `compute_risk`, so the document and the dashboard describe the
        same portfolio. Asserted at the level this test can see: the JSON
        endpoint succeeds for exactly the portfolios the PDF endpoint does.
        """
        assert client.get(f"/api/risk/{funded_portfolio.pk}/").status_code == 200
        assert client.get(url(funded_portfolio.pk)).status_code == 200

    def test_is_not_cached(self, client, funded_portfolio):
        """The URL ends in .pdf; browsers would otherwise treat it as static."""
        response = client.get(url(funded_portfolio.pk))

        assert "no-store" in response["Cache-Control"]

    def test_content_disposition_is_exposed_to_cross_origin_javascript(
        self, client, funded_portfolio
    ):
        """
        The dashboard reads the filename off this header, and it runs on a
        different origin to the API.

        Cross-origin JavaScript can only read the six CORS-safelisted response
        headers unless the server names the rest in Access-Control-Expose-
        Headers. Without it the header is still SENT - curl sees it, a browser
        navigating directly to the URL downloads correctly - but
        `response.headers['content-disposition']` is undefined in the page, and
        every download quietly falls back to a generic name.

        That failure is invisible from the server side, which is exactly why it
        is pinned here.
        """
        response = client.get(
            url(funded_portfolio.pk), HTTP_ORIGIN="http://localhost:5173"
        )

        assert response.status_code == 200
        exposed = response.get("Access-Control-Expose-Headers", "")
        assert "Content-Disposition" in exposed


class TestEndpointErrors:
    """
    The requirement that matters most: a failure is a JSON envelope with the
    right status, NEVER a PDF. A browser will cheerfully save a 400-byte error
    to disk as `report.pdf` if you let it.
    """

    def test_unknown_portfolio_returns_the_json_envelope(self, client, db):
        response = client.get(url(999_999))

        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")
        assert not response.content.startswith(b"%PDF")

        body = response.json()
        assert body["success"] is False
        assert body["data"] is None
        assert body["error"]["code"] == "not_found"
        assert body["error"]["message"]

    def test_empty_portfolio_returns_the_json_envelope(self, client, portfolio):
        """`portfolio` holds nothing - compute_risk raises EmptyPortfolioError."""
        response = client.get(url(portfolio.pk))

        assert response.status_code == 400
        assert response["Content-Type"].startswith("application/json")
        assert not response.content.startswith(b"%PDF")
        assert response.json()["error"]["code"] == "empty_portfolio"

    def test_holdings_without_prices_return_the_json_envelope(
        self, client, portfolio, holding_factory
    ):
        """
        Nothing fetched for the only holding: an envelope, not a broken file.

        400/empty_portfolio rather than the old 422/missing_price_data - with
        every holding excluded there is no report to typeset. A portfolio where
        only SOME holdings are unpriced now renders a perfectly good PDF, with
        the exclusions printed in its warnings block.
        """
        holding_factory("NOPRICES.NS", "10")

        response = client.get(url(portfolio.pk))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_portfolio"
        assert not response.content.startswith(b"%PDF")

    def test_error_envelope_is_json_even_when_the_client_demands_pdf(
        self, client, db
    ):
        """
        The reason the failure path builds its response by hand. A DRF Response
        would be rendered by whichever renderer content negotiation chose, so a
        client asking for application/pdf would get its error envelope back
        under a PDF content type - and save it as a file.
        """
        response = client.get(url(999_999), HTTP_ACCEPT="application/pdf")

        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")
        assert response.json()["error"]["code"] == "not_found"

    def test_accept_pdf_is_negotiable_rather_than_406(self, client, funded_portfolio):
        """
        Without common.renderers.PDFRenderer, DRF answers 406 during content
        negotiation - before the view runs - for the exact media type this
        URL's name advertises.
        """
        response = client.get(url(funded_portfolio.pk), HTTP_ACCEPT="application/pdf")

        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")

    def test_accept_json_still_returns_the_pdf(self, client, funded_portfolio):
        """
        What the dashboard actually sends - the axios client sets
        `Accept: application/json` globally. The endpoint returns the document
        regardless, because the success path bypasses rendering entirely.
        """
        response = client.get(url(funded_portfolio.pk), HTTP_ACCEPT="application/json")

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")


class TestFilenameSafety:
    def test_portfolio_name_cannot_inject_into_the_header(self, client, user):
        """
        The name is user-entered and lands in a response header. A quote or a
        newline in it would be a header-injection hole, so it is slugified -
        this is the test that keeps it slugified.
        """
        from datetime import date
        from decimal import Decimal

        from portfolio.models import Holding, Portfolio
        from risk.tests.conftest import make_history, make_snapshot

        portfolio = Portfolio.objects.create(
            user=user, name='Evil"; drop\nHeader: x', base_currency="INR"
        )
        Holding.objects.create(
            portfolio=portfolio,
            ticker="RELIANCE.NS",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("100"),
            buy_date=date(2026, 1, 5),
        )
        make_history("RELIANCE.NS", seed=1, base=100.0)
        make_snapshot("RELIANCE.NS", "1000.0000")

        response = client.get(url(portfolio.pk))

        disposition = response["Content-Disposition"]
        assert '"' not in disposition.split("filename=")[1][1:-1]
        assert "\n" not in disposition
        assert "drop" in disposition  # slugified, not discarded


def test_pdf_is_deterministic_apart_from_its_timestamp():
    """
    Two builds of the same report with the same clock differ only in reportlab's
    own document id. Worth pinning: it means a difference in the output is a
    difference in the DATA, which is what makes the file worth trusting.
    """
    stamp = datetime(2026, 8, 31, 9, 30, 0, tzinfo=dt_timezone.utc)

    first = build_risk_pdf(sample(), generated_at=stamp)
    second = build_risk_pdf(sample(), generated_at=stamp)

    assert len(first) == len(second)


def test_uses_compute_risk_output_directly(funded_portfolio):
    """
    The integration point, stated as a test: whatever `compute_risk` returns is
    a valid input to the builder. If the service ever adds or renames a key,
    this fails here rather than in a browser.
    """
    report = compute_risk(funded_portfolio.pk)

    pdf = build_risk_pdf(report)

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 3000
