"""
The two auth endpoints, end to end through DRF.

Everything below goes through the real URL conf, the real authentication class
and the real envelope - only `accounts.firebase.verify_token` is mocked (see
conftest). So these tests fail if the routing, the permission class, the
exception handler or the envelope shape regress, not just the service layer.
"""

import pytest
from django.urls import reverse

from accounts import firebase
from accounts.models import AppUser
from accounts.tests.conftest import OTHER_PHONE, PHONE, TOKEN, claims
from portfolio.models import Portfolio

pytestmark = pytest.mark.django_db

SESSION_URL = "/api/auth/session/"
ME_URL = "/api/auth/me/"


def body(response) -> dict:
    """The envelope's `data` half, or the `error` half on a failure."""
    return response.json()


# ---------------------------------------------------------------------------
# No token at all.
# ---------------------------------------------------------------------------
class TestMissingToken:
    def test_session_without_a_token_is_401(self, api):
        response = api.post(SESSION_URL, {})

        assert response.status_code == 401
        payload = body(response)
        assert payload["success"] is False
        assert payload["data"] is None
        assert payload["error"]["code"] == "not_authenticated"

    def test_me_without_a_token_is_401(self, api):
        response = api.get(ME_URL)

        assert response.status_code == 401
        assert body(response)["error"]["code"] == "not_authenticated"

    def test_401_carries_the_bearer_challenge(self, api):
        """
        Without WWW-Authenticate, DRF quietly answers 403 instead of 401 and the
        frontend's "token expired -> re-login" branch never fires.
        """
        response = api.get(ME_URL)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_no_account_is_created_by_an_unauthenticated_call(self, api, counts):
        api.post(SESSION_URL, {"phone": PHONE})

        assert counts() == (0, 0)

    def test_another_schemes_header_is_declined_not_accepted(self, api):
        """A Token/Basic header is not ours; the request stays anonymous."""
        api.credentials(HTTP_AUTHORIZATION="Token abcdef")

        assert api.get(ME_URL).status_code == 401


# ---------------------------------------------------------------------------
# A token Firebase rejects.
# ---------------------------------------------------------------------------
class TestRejectedToken:
    @pytest.mark.parametrize(
        ("error", "expected_code"),
        [
            (firebase.InvalidIdTokenError("malformed"), "invalid_token"),
            (firebase.ExpiredIdTokenError("expired", cause=None), "token_expired"),
            (firebase.RevokedIdTokenError("revoked"), "token_revoked"),
            (ValueError("not a JWT at all"), "invalid_token"),
        ],
    )
    def test_rejection_is_a_401_envelope(self, api, verify, error, expected_code):
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        with verify(side_effect=error):
            response = api.get(ME_URL)

        assert response.status_code == 401
        payload = body(response)
        assert payload["success"] is False
        assert payload["data"] is None
        assert payload["error"]["code"] == expected_code

    def test_expiry_is_not_reported_as_malformed(self, api, verify):
        """
        ExpiredIdTokenError SUBCLASSES InvalidIdTokenError, so an except block
        in the wrong order silently turns "sign in again" into "your client is
        broken". This is the test that catches that reordering.
        """
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        with verify(side_effect=firebase.ExpiredIdTokenError("expired", cause=None)):
            response = api.get(ME_URL)

        assert body(response)["error"]["code"] == "token_expired"

    def test_a_rejected_token_creates_nothing(self, api, verify, counts):
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        with verify(side_effect=firebase.InvalidIdTokenError("malformed")):
            api.post(SESSION_URL, {})

        assert counts() == (0, 0)

    def test_a_token_without_a_phone_number_is_401(self, api, verify, counts):
        """An email or anonymous Firebase account reaching a phone-only backend."""
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        with verify(return_value={"uid": "u1", "email": "someone@example.com"}):
            response = api.post(SESSION_URL, {})

        assert response.status_code == 401
        assert body(response)["error"]["code"] == "phone_number_missing"
        assert counts() == (0, 0)

    def test_bearer_with_no_token_is_401(self, api):
        api.credentials(HTTP_AUTHORIZATION="Bearer")

        response = api.get(ME_URL)

        assert response.status_code == 401
        assert body(response)["error"]["code"] == "invalid_token"

    def test_firebase_without_credentials_is_503_not_401(self, api, verify):
        """
        A server that forgot its service account must not tell the user their
        token is bad - that sends whoever is debugging to the wrong machine.
        """
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

        with verify(side_effect=firebase.FirebaseNotConfigured("no creds")):
            response = api.post(SESSION_URL, {})

        assert response.status_code == 503
        assert body(response)["error"]["code"] == "firebase_unavailable"


# ---------------------------------------------------------------------------
# The happy path: a first login.
# ---------------------------------------------------------------------------
class TestFirstLogin:
    def test_creates_the_user_and_their_portfolio_and_returns_the_ids(self, signed_in, counts):
        with signed_in() as (api, _):
            response = api.post(SESSION_URL, {})

        assert response.status_code == 200
        payload = body(response)
        assert payload["success"] is True

        data = payload["data"]
        app_user = AppUser.objects.get(phone_number=PHONE)
        portfolio = Portfolio.objects.get(user=app_user.user)

        assert data["user_id"] == app_user.pk
        assert data["phone"] == PHONE
        assert data["portfolio_id"] == portfolio.pk
        assert data["first_login"] is True
        assert counts() == (1, 1)

    def test_the_new_portfolio_is_named_and_denominated_correctly(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})

        portfolio = Portfolio.objects.get(user__app_user__phone_number=PHONE)
        assert portfolio.name == "My Portfolio"
        assert portfolio.base_currency == "INR"

    def test_the_token_from_the_header_is_what_gets_verified(self, signed_in):
        with signed_in(token="header-token-value") as (api, verify_token):
            api.post(SESSION_URL, {})

        verify_token.assert_called_once_with("header-token-value")

    def test_the_login_is_stamped(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})

        assert AppUser.objects.get(phone_number=PHONE).last_login_at is not None

    def test_the_wrapped_django_user_cannot_be_password_logged_in(self, signed_in):
        """
        The phone is the only way in. A usable password on these rows would be
        a second, weaker door into somebody's portfolio.
        """
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})

        assert AppUser.objects.get(phone_number=PHONE).user.has_usable_password() is False


# ---------------------------------------------------------------------------
# The property this whole part exists for.
# ---------------------------------------------------------------------------
class TestOnlyTheTokensPhoneIsTrusted:
    def test_a_phone_number_in_the_body_is_ignored(self, signed_in, counts):
        """
        The client posts somebody else's number alongside its own valid token.
        The account created must be the TOKEN's, and only the token's.
        """
        with signed_in(phone=PHONE) as (api, _):
            response = api.post(SESSION_URL, {"phone": OTHER_PHONE, "phone_number": OTHER_PHONE})

        assert body(response)["data"]["phone"] == PHONE
        assert not AppUser.objects.filter(phone_number=OTHER_PHONE).exists()
        assert counts() == (1, 1)

    def test_a_phone_header_is_ignored(self, api, verify):
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}", HTTP_X_PHONE_NUMBER=OTHER_PHONE)

        with verify(return_value=claims(phone=PHONE)):
            response = api.post(SESSION_URL, {})

        assert body(response)["data"]["phone"] == PHONE

    def test_two_different_tokens_are_two_different_accounts(self, signed_in, counts):
        with signed_in(phone=PHONE, uid="uid-a") as (api, _):
            first = api.post(SESSION_URL, {})
        with signed_in(phone=OTHER_PHONE, uid="uid-b") as (api, _):
            second = api.post(SESSION_URL, {})

        assert body(first)["data"]["portfolio_id"] != body(second)["data"]["portfolio_id"]
        assert counts() == (2, 2)


# ---------------------------------------------------------------------------
# Returning users.
# ---------------------------------------------------------------------------
class TestReturningUser:
    def test_a_second_session_reuses_the_same_user_and_portfolio(self, signed_in, counts):
        with signed_in() as (api, _):
            first = api.post(SESSION_URL, {})
        with signed_in() as (api, _):
            second = api.post(SESSION_URL, {})

        assert body(first)["data"]["user_id"] == body(second)["data"]["user_id"]
        assert body(first)["data"]["portfolio_id"] == body(second)["data"]["portfolio_id"]
        assert counts() == (1, 1)

    def test_the_second_session_is_not_a_first_login(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})
        with signed_in() as (api, _):
            second = api.post(SESSION_URL, {})

        assert body(second)["data"]["first_login"] is False

    def test_a_new_firebase_uid_for_the_same_number_keeps_the_portfolio(
        self, signed_in, counts
    ):
        """
        Deleting the account in the Firebase console and signing in again issues
        a NEW uid for the SAME number. The phone is the key, so the investor
        must land back on their own portfolio rather than a fresh empty one.
        """
        with signed_in(uid="uid-before") as (api, _):
            first = api.post(SESSION_URL, {})
        with signed_in(uid="uid-after") as (api, _):
            second = api.post(SESSION_URL, {})

        assert body(first)["data"]["portfolio_id"] == body(second)["data"]["portfolio_id"]
        assert counts() == (1, 1)
        assert AppUser.objects.get(phone_number=PHONE).firebase_uid == "uid-after"

    def test_a_renamed_portfolio_is_not_duplicated(self, signed_in, counts):
        """
        `ensure_portfolio` looks up by USER, not by name - otherwise renaming
        "My Portfolio" would silently grow a second one on the next login.
        """
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})
        portfolio = Portfolio.objects.get(user__app_user__phone_number=PHONE)
        portfolio.name = "Retirement"
        portfolio.save(update_fields=["name"])

        with signed_in() as (api, _):
            response = api.post(SESSION_URL, {})

        assert body(response)["data"]["portfolio_id"] == portfolio.pk
        assert counts() == (1, 1)

    def test_session_is_recreated_if_the_portfolio_was_deleted(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})
        Portfolio.objects.all().delete()

        with signed_in() as (api, _):
            response = api.post(SESSION_URL, {})

        assert response.status_code == 200
        assert Portfolio.objects.filter(user__app_user__phone_number=PHONE).count() == 1


# ---------------------------------------------------------------------------
# /api/auth/me/
# ---------------------------------------------------------------------------
class TestMe:
    def test_returns_the_caller_and_their_portfolio(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})
            response = api.get(ME_URL)

        data = body(response)["data"]
        portfolio = Portfolio.objects.get(user__app_user__phone_number=PHONE)

        assert response.status_code == 200
        assert data["phone"] == PHONE
        assert data["portfolio_id"] == portfolio.pk
        assert data["portfolio_name"] == portfolio.name

    def test_me_alone_is_enough_to_create_the_account(self, signed_in, counts):
        """
        The frontend is supposed to call /session/ first, but a page reload with
        a live token may reach /me/ on its own. Authentication resolves the
        identity either way, so this must not 404.
        """
        with signed_in() as (api, _):
            response = api.get(ME_URL)

        assert response.status_code == 200
        assert counts() == (1, 1)

    def test_me_does_not_stamp_a_login(self, signed_in):
        """/me/ is a pure read - it is called on every boot and every reload."""
        with signed_in() as (api, _):
            api.get(ME_URL)

        assert AppUser.objects.get(phone_number=PHONE).last_login_at is None

    def test_me_404s_when_the_portfolio_is_gone(self, signed_in):
        with signed_in() as (api, _):
            api.post(SESSION_URL, {})
            Portfolio.objects.all().delete()
            response = api.get(ME_URL)

        assert response.status_code == 404
        assert body(response)["error"]["code"] == "not_found"

    def test_an_admin_session_is_not_an_investor(self, api, django_user_model):
        """
        A superuser logged into /admin is authenticated but owns no phone
        identity. 403, not a 500 on a missing profile and not somebody's data.
        """
        django_user_model.objects.create_superuser(username="root", password="pw", email="")
        api.login(username="root", password="pw")

        assert api.get(ME_URL).status_code == 403
