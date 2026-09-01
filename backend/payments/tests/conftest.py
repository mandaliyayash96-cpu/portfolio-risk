"""
Fixtures for the payment tests.

NOTHING HERE TOUCHES THE NETWORK, AND THE SIGNATURE IS REAL
-----------------------------------------------------------
Two very different things are being faked, and only one of them is a mock:

  * ORDER CREATION is an HTTPS call to Razorpay, so `payments.gateway.create_order`
    is patched. There is no way to make a real order in a test, and nothing in
    our code is being skipped by faking it - we send an amount and store the id
    that comes back.

  * SIGNATURE VERIFICATION is NOT mocked, because it is not a network call. It
    is an HMAC-SHA256 of "order_id|payment_id" keyed by the secret, computed
    locally by the Razorpay SDK. So the tests set a known test secret, compute
    the signature the same way Razorpay would, and let the real verification
    code run against it. `sign()` below is the checkout widget's half.

That split is the point: the one piece of this feature that decides whether
money was really taken is exercised for real, by the same library call
production uses.
"""

import hashlib
import hmac
from contextlib import contextmanager
from unittest import mock

import pytest
from rest_framework.test import APIClient

from accounts.services import resolve_app_user
from payments.models import Payment, PaymentStatus

#: The key pair every test in this package runs under. Test-mode shaped, and
#: entirely made up - it never leaves the process and matches nothing real.
TEST_KEY_ID = "rzp_test_fake_key_id"
TEST_KEY_SECRET = "fake_test_key_secret_do_not_use"

PHONE = "+919876543210"
OTHER_PHONE = "+919000000001"

#: What a patched `create_order` hands back, trimmed to the fields we read.
FAKE_ORDER_ID = "order_TESTFAKE0001"


def sign(order_id: str, payment_id: str, secret: str = TEST_KEY_SECRET) -> str:
    """
    The signature Razorpay's checkout would hand the browser.

    Exactly the construction the SDK verifies: HMAC-SHA256 over
    "<order_id>|<payment_id>", hex-digested, keyed by the API secret. Written
    out here rather than borrowed from the SDK so that the test would still
    fail if the SDK's verification ever stopped checking what it claims to.
    """
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


@pytest.fixture(autouse=True)
def razorpay_keys(settings):
    """
    Point the whole process at the test key pair, for every test here.

    Autouse because a test that accidentally ran against empty keys would get
    a 503 and could be misread as a failing gate. Assigned through
    pytest-django's `settings` fixture, so it is undone at teardown.
    """
    settings.RAZORPAY_KEY_ID = TEST_KEY_ID
    settings.RAZORPAY_KEY_SECRET = TEST_KEY_SECRET
    return settings


@pytest.fixture
def app_user(db):
    """A signed-in investor, with the portfolio Part 1 auto-creates."""
    return resolve_app_user(PHONE)


@pytest.fixture
def other_app_user(db):
    """Somebody else, for the "this order is not yours" test."""
    return resolve_app_user(OTHER_PHONE)


@pytest.fixture
def api(app_user) -> APIClient:
    """
    A client authenticated as `app_user`.

    force_authenticate rather than a mocked Firebase token: these tests are
    about PAYMENT, and routing them through token verification as well would
    make a broken signature check look like a broken login. accounts' own
    tests cover the token path.
    """
    client = APIClient()
    client.force_authenticate(user=app_user.user)
    return client


@pytest.fixture
def anon() -> APIClient:
    return APIClient()


@pytest.fixture
def fake_order():
    """
    Patch the one call that would hit the network.

    Yields the mock so a test can assert WHAT we asked Razorpay to charge -
    which is how "the amount is ours, not the client's" gets proved.
    """

    @contextmanager
    def _patch(order_id: str = FAKE_ORDER_ID, **extra):
        with mock.patch("payments.gateway.create_order") as create:
            create.return_value = {
                "id": order_id,
                "amount": 900,
                "currency": "INR",
                "status": "created",
                **extra,
            }
            yield create

    return _patch


@pytest.fixture
def paid_unlock(app_user):
    """
    A live grant, as a verified payment would have left one.

    Built through the model rather than by driving the endpoints, so the tests
    that are about the GATE do not fail for reasons belonging to checkout.
    """

    def _make(user=None, **overrides):
        from django.utils import timezone

        fields = {
            "user": user or app_user.user,
            "razorpay_order_id": f"order_PAID{Payment.objects.count():04d}",
            "razorpay_payment_id": "pay_TESTFAKE0001",
            "amount": 900,
            "currency": "INR",
            "status": PaymentStatus.PAID,
            "paid_at": timezone.now(),
            "consumed": False,
        }
        fields.update(overrides)
        return Payment.objects.create(**fields)

    return _make
