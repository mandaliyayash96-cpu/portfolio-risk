"""
Fixtures for the AI insight tests.

NOTHING HERE TOUCHES THE NETWORK
--------------------------------
Every test patches `insights.gemini.generate` - the one function in the
project that calls Gemini - and asserts on what happens around it: what the
prompt says, how the answer is parsed, what a refusal looks like on the wire.
The model's own behaviour is not under test and cannot be, deterministically.

`insights.services` reaches the seam as `gemini.generate(...)` rather than
importing the name, which is what makes the patch below visible to it - the
same arrangement `accounts.authentication` has with `firebase.verify_token`.

The portfolio fixtures are borrowed from risk/tests/conftest.py rather than
rebuilt: the insights service consumes `compute_risk`, so the sample data that
exercises that is exactly the sample data that exercises this.
"""

import json
from contextlib import contextmanager
from unittest import mock

import pytest
from rest_framework.test import APIClient

from accounts.services import resolve_app_user
from risk.tests.conftest import BENCHMARK, make_history, make_snapshot

PHONE = "+919876543210"

#: A well-formed answer, one insight per category, none of them advice.
SAMPLE_INSIGHTS = [
    {
        "category": "gain",
        "text": "RELIANCE.NS is up 20% on its average cost, the largest gain in the portfolio.",
    },
    {
        "category": "loss",
        "text": "TCS.NS sits 8% below its average cost, the only position currently underwater.",
    },
    {
        "category": "risk",
        "text": "The maximum drawdown of -12% means the portfolio fell 12% from its peak at one point in the window.",
    },
    {
        "category": "diversification",
        "text": "With two holdings at 60/40 the portfolio behaves mostly like its largest position.",
    },
]


def gemini_json(insights=None) -> str:
    """What a real `generate()` hands back: the JSON document, as text."""
    return json.dumps({"insights": SAMPLE_INSIGHTS if insights is None else insights})


@pytest.fixture(autouse=True)
def gemini_settings(settings):
    """
    A key and a model name for every test here, and none of them real.

    Autouse so no test can accidentally exercise the `not_configured` path
    when it meant to test something else. The value never reaches a request
    because `generate` is always patched.
    """
    settings.GEMINI_API_KEY = "test-key-not-real"
    settings.GEMINI_MODEL = "gemini-test-model"
    settings.GEMINI_TIMEOUT_SECONDS = 1.0
    return settings


@pytest.fixture
def fake_gemini():
    """
    Patch the one call that would hit the network.

    Yields the mock so a test can read the PROMPT that was sent, which is how
    "the model is told observations-only" gets proved rather than assumed.
    """

    @contextmanager
    def _patch(return_value=None, side_effect=None):
        with mock.patch("insights.gemini.generate") as generate:
            if side_effect is not None:
                generate.side_effect = side_effect
            else:
                generate.return_value = gemini_json() if return_value is None else return_value
            yield generate

    return _patch


@pytest.fixture
def app_user(db):
    """A signed-in investor, with the portfolio Part 1 auto-creates."""
    return resolve_app_user(PHONE)


@pytest.fixture
def api(app_user) -> APIClient:
    """A client authenticated as `app_user` - force_authenticate, as payments does."""
    client = APIClient()
    client.force_authenticate(user=app_user.user)
    return client


@pytest.fixture
def anon() -> APIClient:
    return APIClient()


@pytest.fixture
def my_funded_portfolio(app_user):
    """
    The signed-in user's OWN portfolio, funded like risk's `funded_portfolio`.

    Two positions with a live price and 90 days of history each, plus the
    benchmark. Costs are set so one position is up and one is down, which is
    what gives the prompt a gain and a loss to describe:

        RELIANCE.NS  100 x 1000 = 100,000  cost 100 x  800 =  80,000  (+25%)
        TCS.NS        50 x 2000 = 100,000  cost  50 x 2200 = 110,000  (-9.1%)
    """
    from datetime import date
    from decimal import Decimal

    from portfolio.models import Holding

    portfolio = app_user.user.portfolios.order_by("pk").first()
    for ticker, quantity, cost in (
        ("RELIANCE.NS", "100", "800.0000"),
        ("TCS.NS", "50", "2200.0000"),
    ):
        Holding.objects.create(
            portfolio=portfolio,
            ticker=ticker,
            quantity=Decimal(quantity),
            avg_buy_price=Decimal(cost),
            buy_date=date(2026, 1, 5),
        )

    make_history("RELIANCE.NS", seed=1, base=100.0)
    make_history("TCS.NS", seed=2, base=200.0)
    make_history(BENCHMARK, seed=3, base=22000.0, vol=0.008)
    make_snapshot("RELIANCE.NS", "1000.0000")
    make_snapshot("TCS.NS", "2000.0000")
    make_snapshot(BENCHMARK, "22500.0000")
    return portfolio


@pytest.fixture
def with_dead_ticker(my_funded_portfolio):
    """The same portfolio plus one holding that has no price data at all."""
    from datetime import date
    from decimal import Decimal

    from portfolio.models import Holding

    Holding.objects.create(
        portfolio=my_funded_portfolio,
        ticker="DEADCO.NS",
        quantity=Decimal("20"),
        avg_buy_price=Decimal("50.0000"),
        buy_date=date(2026, 1, 5),
    )
    return my_funded_portfolio
