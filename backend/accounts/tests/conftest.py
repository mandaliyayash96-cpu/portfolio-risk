"""
Fixtures for the auth tests.

NOTHING HERE TOUCHES THE NETWORK, AND NOTHING NEEDS A SERVICE ACCOUNT
---------------------------------------------------------------------
Verifying a real Firebase ID token means an RSA signature check against
certificates fetched from Google, with a token that expires in an hour. None of
that is testable in CI, and none of it is OUR logic - it is one library call.

So the suite patches exactly one seam, `accounts.firebase.verify_token`, and
asserts on everything downstream of it: which claims are trusted, which are
ignored, what gets created, and what a rejection looks like on the wire. That
is the same shape `portfolio/tests/conftest.py` uses for the market data feed.

`accounts.authentication` reaches the seam as `firebase.verify_token(...)`
rather than importing the name, which is what makes the patch below visible to
it - a `from ... import verify_token` at the top of that module would bind the
real function before any test could replace it.
"""

from contextlib import contextmanager
from unittest import mock

import pytest
from rest_framework.test import APIClient

from accounts.models import AppUser
from portfolio.models import Portfolio

#: The number every test signs in with, in the E.164 form Firebase emits.
PHONE = "+919876543210"
OTHER_PHONE = "+919000000001"

#: Whatever the client sends as its bearer credential. Its VALUE is irrelevant
#: to every test here: the mocked verifier never looks at it, which is itself
#: the point - the backend trusts the claims it gets back, not the string.
TOKEN = "any-opaque-id-token"


def claims(phone: str = PHONE, uid: str = "firebase-uid-1", **extra) -> dict:
    """
    A decoded ID token, trimmed to the claims this backend reads.

    A real one carries a dozen more (iss, aud, auth_time, firebase.*); they are
    left out because nothing here reads them, and a fixture that pretends to be
    a complete token invites tests that assert on fiction.
    """
    return {"uid": uid, "phone_number": phone, **extra}


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def verify():
    """
    Patch the Firebase verifier for one test.

    Yields the mock, so a test can assert what the backend handed to Firebase -
    which is how "the token from the header, and nothing else" is proved.
    """

    @contextmanager
    def _patch(return_value=None, side_effect=None):
        with mock.patch("accounts.firebase.verify_token") as verify_token:
            if side_effect is not None:
                verify_token.side_effect = side_effect
            else:
                verify_token.return_value = return_value if return_value is not None else claims()
            yield verify_token

    return _patch


@pytest.fixture
def signed_in(api, verify):
    """
    A client whose Authorization header verifies to `phone`.

    Returned as a context manager rather than a plain client because the patch
    has to stand for the duration of the REQUEST, not just its setup - the
    token is verified inside the view's dispatch.
    """

    @contextmanager
    def _client(phone: str = PHONE, uid: str = "firebase-uid-1", token: str = TOKEN):
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        with verify(return_value=claims(phone=phone, uid=uid)) as verify_token:
            yield api, verify_token
        api.credentials()

    return _client


@pytest.fixture
def other_investors_portfolio(db) -> Portfolio:
    """
    Somebody else's portfolio, with a LOW id.

    Low on purpose: the scoping tests need a portfolio whose id a signed-in
    caller might plausibly guess or have hard-coded (portfolio 1 is in every
    curl example in RUN.md), so that "the URL said 1, the token said me"
    resolves the way the docstrings claim.
    """
    from django.contrib.auth import get_user_model

    owner = get_user_model().objects.create_user(username="someone-else", password="x")
    return Portfolio.objects.create(user=owner, name="Not Yours", base_currency="INR")


@pytest.fixture
def counts():
    """Row counts, for the assertions about not creating duplicates."""

    def _counts() -> tuple[int, int]:
        return AppUser.objects.count(), Portfolio.objects.count()

    return _counts
