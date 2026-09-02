"""
The simulated broker import: POST /api/portfolio/<id>/import-broker/.

WHAT IS WORTH TESTING ABOUT A MOCK
----------------------------------
Not the sample data - asserting that Zerodha's table contains RELIANCE.NS would
just restate `portfolio.brokers` back to itself, and would have to be edited
every time the demo data is tuned. What is worth testing is everything the mock
is wired INTO, because none of that is simulated:

  * the ₹9 gate is in front of it, exactly like every other holdings write;
  * the rows land as real Holding rows, through the same validation;
  * it upserts on ticker, so aggregating two brokers that report the same scrip
    leaves one consolidated position - which is the feature's whole claim;
  * an unknown broker is a 400 envelope and writes nothing.

So the assertions below are about SHAPE and BEHAVIOUR: that each broker returns
a non-empty set of holdings that actually appear in the portfolio afterwards,
not that any particular ticker is among them.

THE STUB PROVIDER ONLY KNOWS FOUR SYMBOLS
-----------------------------------------
`stub_provider_setting` points the process at a feed that prices RELIANCE.NS,
TCS.NS, INFY.NS and HDFCBANK.NS and raises for everything else. The sample
portfolios deliberately contain more than that, so most imports here produce
rows that SAVED and carry a price warning. That is the same outcome a real
unknown symbol has, and asserting the rows still land is the point - a warning
must never be a failure.
"""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from portfolio.brokers import BROKER_SAMPLES, SUPPORTED_BROKERS
from portfolio.models import Holding

pytestmark = pytest.mark.django_db


def broker_url(portfolio_id: int) -> str:
    return f"/api/portfolio/{portfolio_id}/import-broker/"


def tickers_in(portfolio) -> set[str]:
    return set(portfolio.holdings.values_list("ticker", flat=True))


def sample_tickers(broker: str) -> set[str]:
    """What the preset table claims, read from the table rather than retyped."""
    return {row["ticker"] for row in BROKER_SAMPLES[broker]["holdings"]}


class TestEachBrokerImports:
    @pytest.mark.parametrize("broker", SUPPORTED_BROKERS)
    def test_returns_that_brokers_holdings(
        self, editor, portfolio, broker, stub_provider_setting
    ):
        response = editor.post(broker_url(portfolio.pk), {"broker": broker}, format="json")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["error"] is None

        data = body["data"]
        assert data["broker"] == broker
        assert data["broker_label"] == BROKER_SAMPLES[broker]["label"]
        # The response says out loud that this was not a real broker call.
        assert data["simulated"] is True
        assert data["total_rows"] > 0
        assert data["skipped"] == 0
        assert len(data["results"]) == data["total_rows"]

        # The statement itself comes back, not just what became of it: the
        # endpoint's contract is "here is what the broker reported, and here is
        # what I did with it".
        reported = data["holdings"]
        assert len(reported) == data["total_rows"]
        assert {row["ticker"] for row in reported} == sample_tickers(broker)
        assert all(row["quantity"] and row["avg_buy_price"] for row in reported)

    @pytest.mark.parametrize("broker", SUPPORTED_BROKERS)
    def test_imported_holdings_appear_in_the_portfolio(
        self, editor, portfolio, broker, stub_provider_setting
    ):
        expected = sample_tickers(broker)

        response = editor.post(broker_url(portfolio.pk), {"broker": broker}, format="json")

        assert response.status_code == 200
        assert response.json()["data"]["added"] == len(expected)
        assert tickers_in(portfolio) == expected

    def test_the_rows_land_through_the_normal_validation(
        self, editor, portfolio, stub_provider_setting
    ):
        """
        A spot check that these are ordinary Holdings, not a parallel shape.

        Quantity and price are stored as the Decimals the manual form would
        have produced, and buy_date survives - which is what proves the import
        went through `_validated_fields` rather than around it.
        """
        editor.post(broker_url(portfolio.pk), {"broker": "zerodha"}, format="json")

        sample = BROKER_SAMPLES["zerodha"]["holdings"][0]
        saved = Holding.objects.get(portfolio=portfolio, ticker=sample["ticker"])
        # Decimal equality is numeric, so the column's trailing zeros do not
        # have to be written out here.
        assert saved.quantity == Decimal(sample["quantity"])
        assert saved.avg_buy_price == Decimal(sample["avg_buy_price"])
        assert saved.buy_date.isoformat() == sample["buy_date"]
        assert saved.sector == sample["sector"]
        assert saved.asset_type == sample["asset_type"]

    def test_a_symbol_the_feed_cannot_price_still_saves_with_a_warning(
        self, editor, portfolio, stub_provider_setting
    ):
        """
        Most sample tickers are unknown to the stub. They must still be stored,
        each carrying the reason it has no prices yet - the same contract the
        manual form has for a typo'd symbol.
        """
        response = editor.post(broker_url(portfolio.pk), {"broker": "upstox"}, format="json")

        data = response.json()["data"]
        assert data["added"] == len(sample_tickers("upstox"))
        assert data["price_fetch"]["attempted"] is True
        warned = [row for row in data["results"] if row["warning"]]
        assert warned, "expected the unpriceable sample symbols to carry a warning"
        # Warned or not, every row is in the database.
        assert tickers_in(portfolio) == sample_tickers("upstox")


class TestAggregation:
    def test_two_brokers_consolidate_into_one_portfolio(
        self, editor, portfolio, stub_provider_setting
    ):
        """The claim the feature is making, asserted directly."""
        editor.post(broker_url(portfolio.pk), {"broker": "zerodha"}, format="json")
        editor.post(broker_url(portfolio.pk), {"broker": "groww"}, format="json")

        expected = sample_tickers("zerodha") | sample_tickers("groww")
        assert tickers_in(portfolio) == expected
        # The union is smaller than the sum, because these two overlap - if that
        # ever stops being true the test below is the one that still matters.
        assert Holding.objects.filter(portfolio=portfolio).count() == len(expected)

    def test_a_scrip_held_at_two_brokers_is_one_position(
        self, editor, portfolio, stub_provider_setting
    ):
        overlap = sample_tickers("zerodha") & sample_tickers("icici")
        assert overlap, "the sample data is supposed to overlap - see brokers.py"

        editor.post(broker_url(portfolio.pk), {"broker": "zerodha"}, format="json")
        second = editor.post(broker_url(portfolio.pk), {"broker": "icici"}, format="json")

        # The shared tickers report as updated, not added.
        assert second.json()["data"]["updated"] == len(overlap)
        for ticker in overlap:
            assert Holding.objects.filter(portfolio=portfolio, ticker=ticker).count() == 1

    def test_importing_the_same_broker_twice_changes_nothing(
        self, editor, portfolio, stub_provider_setting
    ):
        editor.post(broker_url(portfolio.pk), {"broker": "groww"}, format="json")
        before = Holding.objects.filter(portfolio=portfolio).count()

        again = editor.post(broker_url(portfolio.pk), {"broker": "groww"}, format="json")

        data = again.json()["data"]
        assert (data["added"], data["updated"]) == (0, before)
        assert Holding.objects.filter(portfolio=portfolio).count() == before


class TestTheGate:
    """
    402 and 401 are asserted here as well as in payments/tests/test_gate.py.

    Deliberate duplication: this endpoint is new, and the one thing that would
    make it a security problem rather than a demo is shipping it without the
    gate the other three writes have. That is worth a test living next to the
    endpoint it guards.
    """

    def test_import_without_an_unlock_is_402(self, investor, portfolio, stub_provider_setting):
        # Signed in, but no paid round: the `editor` fixture is exactly this
        # client plus the ₹9 grant, so this is the one variable changing.
        signed_in = APIClient()
        signed_in.force_authenticate(user=investor)

        response = signed_in.post(broker_url(portfolio.pk), {"broker": "zerodha"}, format="json")

        assert response.status_code == 402
        assert response.json()["error"]["code"] == "payment_required"
        assert Holding.objects.count() == 0

    def test_an_anonymous_import_is_401(self, api, portfolio, stub_provider_setting):
        response = api.post(broker_url(portfolio.pk), {"broker": "zerodha"}, format="json")

        assert response.status_code == 401
        assert Holding.objects.count() == 0

    def test_one_unlock_covers_several_brokers(
        self, editor, portfolio, stub_provider_setting
    ):
        """Aggregation would be unusable if each broker cost its own ₹9."""
        for broker in SUPPORTED_BROKERS:
            response = editor.post(broker_url(portfolio.pk), {"broker": broker}, format="json")
            assert response.status_code == 200, broker


class TestBadInput:
    @pytest.mark.parametrize(
        "payload",
        [
            {"broker": "robinhood"},
            {"broker": ""},
            {"broker": None},
            {},
        ],
    )
    def test_an_unknown_broker_is_a_400_envelope(self, editor, portfolio, payload):
        response = editor.post(broker_url(portfolio.pk), payload, format="json")

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_input"
        assert error["details"]["supported"] == list(SUPPORTED_BROKERS)
        assert Holding.objects.count() == 0

    def test_a_bare_json_array_body_does_not_crash(self, editor, portfolio):
        response = editor.post(broker_url(portfolio.pk), ["zerodha"], format="json")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"

    def test_the_slug_is_case_and_space_insensitive(
        self, editor, portfolio, stub_provider_setting
    ):
        response = editor.post(
            broker_url(portfolio.pk), {"broker": "  Zerodha "}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["data"]["broker"] == "zerodha"


class TestTheMockIsIsolated:
    """
    Guards on the seam itself, so the demo cannot rot into something dishonest.
    """

    def test_importing_does_not_mutate_the_preset_table(
        self, editor, portfolio, stub_provider_setting
    ):
        """
        `fetch_broker_holdings` deep-copies. Without that, the first import of
        the process could rewrite the sample every later one returns - a bug
        that would only ever show up on a long-lived worker.
        """
        before = {broker: sample_tickers(broker) for broker in SUPPORTED_BROKERS}

        for broker in SUPPORTED_BROKERS:
            editor.post(broker_url(portfolio.pk), {"broker": broker}, format="json")

        assert {broker: sample_tickers(broker) for broker in SUPPORTED_BROKERS} == before

    def test_every_sample_is_non_empty_and_uses_nse_symbols(self):
        """
        A cheap check on the demo data itself: the point of the feature is that
        four brokers show four believable, different portfolios.
        """
        seen = {}
        for broker, entry in BROKER_SAMPLES.items():
            tickers = sample_tickers(broker)
            assert tickers, f"{broker} has no sample holdings"
            assert entry["label"], f"{broker} has no display label"
            assert all(ticker.endswith(".NS") for ticker in tickers), broker
            # No two brokers may be the same list, or the demo shows one
            # portfolio four times.
            assert tickers not in seen.values(), f"{broker} duplicates another broker"
            seen[broker] = tickers
