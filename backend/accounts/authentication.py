"""
DRF authentication against a Firebase phone-auth ID token.

THE TRUST BOUNDARY
------------------
The browser runs the whole OTP dance (Firebase sends the SMS, the user types
the code, Firebase mints an ID token). None of that is trustworthy on its own -
a client can POST any JSON it likes. What makes this safe is one line in
`accounts.firebase.verify_token`: the token's signature is checked against
Google's public certificates using this project's service account, and the
phone number is then read OUT OF THE VERIFIED CLAIMS.

So the rule this module exists to enforce: the phone number NEVER comes from
the request body, a header, or a query parameter. `request.data` is not read
here at all. A client that posts {"phone": "+919999999999"} alongside somebody
else's token gets somebody else's token's identity, which is nobody's.

WHY 401 AND NOT 403
-------------------
DRF downgrades AuthenticationFailed to 403 when the failing authenticator has
no `authenticate_header()` - it reasons that a scheme which cannot say
WWW-Authenticate cannot be retried. `authenticate_header` below returns the
Bearer challenge, which is what keeps every failure in this file a clean 401 in
the standard envelope.
"""

import logging

from rest_framework import status as http_status
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import APIException, AuthenticationFailed

from accounts import firebase

logger = logging.getLogger(__name__)

# WHY `accounts.services` AND `common.exceptions` ARE IMPORTED INSIDE THE
# METHOD BELOW RATHER THAN HERE
# ----------------------------------------------------------------------------
# This module is named in DEFAULT_AUTHENTICATION_CLASSES, and DRF resolves that
# setting the first time `rest_framework.views` is imported - from INSIDE that
# module's own import. `common.exceptions` does `from rest_framework.views
# import exception_handler`, so importing it at module level here re-enters a
# half-initialised rest_framework.views and the whole process dies with a
# circular ImportError before Django has loaded a single app.
#
# `accounts.services` reaches the ORM, which is the second reason: settings can
# be resolved before the app registry is ready.
#
# Deferring both to call time costs one dict lookup per authenticated request
# (the modules are in sys.modules by then) and keeps the import graph acyclic.

#: The scheme this class answers to. Anything else in the Authorization header
#: (Basic, Token) is left for another authenticator to claim.
KEYWORD = "Bearer"


# ---------------------------------------------------------------------------
# Failures.
#
# Each subclass carries its own `default_code`, and `common.exceptions.
# _describe` reads exactly that to fill the envelope's error.code - so the
# client can tell "sign in again" (token_expired) apart from "this is not a
# token" (invalid_token) without parsing prose.
# ---------------------------------------------------------------------------
class InvalidToken(AuthenticationFailed):
    default_code = "invalid_token"
    default_detail = "Invalid authentication token."


class TokenExpired(AuthenticationFailed):
    default_code = "token_expired"
    default_detail = "Authentication token has expired. Sign in again."


class TokenRevoked(AuthenticationFailed):
    default_code = "token_revoked"
    default_detail = "Authentication token has been revoked. Sign in again."


class AccountDisabled(AuthenticationFailed):
    default_code = "account_disabled"
    default_detail = "This account has been disabled."


class PhoneNumberMissing(AuthenticationFailed):
    default_code = "phone_number_missing"
    default_detail = "This token carries no verified phone number."


class FirebaseUnavailable(APIException):
    """
    A 503, not a 401: the caller's credentials were never examined.

    Raised when the Admin SDK has no service account, or cannot reach Google's
    certificate endpoint. Telling a user with a perfectly good token that their
    token is bad would be a lie that costs somebody an afternoon.
    """

    status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "firebase_unavailable"
    default_detail = "Authentication is temporarily unavailable."


class FirebaseAuthentication(BaseAuthentication):
    """
    Authorization: Bearer <firebase_id_token>  ->  (auth.User, decoded claims).

    Returns None - declining, rather than failing - when there is no Bearer
    header at all. That is what lets this class sit in
    DEFAULT_AUTHENTICATION_CLASSES globally during Part 1 without changing a
    single existing endpoint: the AllowAny views keep serving anonymous
    requests exactly as before, and only views with a permission class demand a
    token.

    `request.user` is the Django user wrapped by the AppUser (see
    accounts/models.py for why the identity is a profile rather than a custom
    user model); `request.auth` is the decoded claims dict.
    """

    def authenticate(self, request):
        token = self._bearer_token(request)
        if token is None:
            return None

        claims = self._verify(token)
        phone = (claims.get("phone_number") or "").strip()
        if not phone:
            # An email/Google/anonymous Firebase account reaching a phone-only
            # backend. The token is genuine, so this is not "invalid" - it is
            # the wrong KIND of credential, and saying so is the difference
            # between a five-minute fix and an hour of certificate debugging.
            raise PhoneNumberMissing()

        from accounts.services import resolve_app_user
        from common.exceptions import DomainError

        try:
            app_user = resolve_app_user(phone, firebase_uid=claims.get("uid"))
        except DomainError as exc:
            # normalise_phone rejected the claim (empty, or absurdly long).
            # Surfacing it as a 401 keeps the envelope consistent - a claim we
            # cannot key on is a credential we cannot accept.
            raise InvalidToken(exc.message) from exc

        return (app_user.user, claims)

    def authenticate_header(self, request) -> str:
        """The WWW-Authenticate challenge. Its presence is what makes 401s 401."""
        return KEYWORD + ' realm="api"'

    # -- internals ---------------------------------------------------------
    def _bearer_token(self, request) -> str | None:
        """
        The token out of the Authorization header, or None to decline.

        Returns None for a missing header and for another scheme's header;
        raises only when the header IS ours and is malformed, because "Bearer"
        with nothing after it is a client bug worth reporting rather than an
        anonymous request.
        """
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != KEYWORD.lower().encode():
            return None
        if len(header) == 1:
            raise InvalidToken("Authorization header contained no token.")
        if len(header) > 2:
            raise InvalidToken("Authorization header must be Bearer <token>.")

        try:
            return header[1].decode()
        except UnicodeError as exc:
            raise InvalidToken("Authorization token is not valid UTF-8.") from exc

    def _verify(self, token: str) -> dict:
        """
        Hand the token to Firebase and translate its refusals.

        The order of these excepts is load-bearing: ExpiredIdTokenError and
        RevokedIdTokenError both SUBCLASS InvalidIdTokenError, so catching the
        parent first would report every expiry as a malformed token and send
        the frontend chasing the wrong bug.

        Nothing here logs the token. Not the value, not a prefix of it - it is
        a bearer credential until it expires, and a log file is not a vault.
        """
        try:
            return firebase.verify_token(token)
        except firebase.FirebaseNotConfigured as exc:
            logger.error("Token verification attempted with no Firebase credentials.")
            raise FirebaseUnavailable() from exc
        except firebase.CertificateFetchError as exc:
            # Could not reach Google to fetch the signing certificates. The
            # token may be perfect; we simply cannot tell right now.
            logger.error("Could not fetch Firebase certificates: %s", type(exc).__name__)
            raise FirebaseUnavailable() from exc
        except firebase.RevokedIdTokenError as exc:
            raise TokenRevoked() from exc
        except firebase.ExpiredIdTokenError as exc:
            raise TokenExpired() from exc
        except firebase.UserDisabledError as exc:
            raise AccountDisabled() from exc
        except firebase.InvalidIdTokenError as exc:
            raise InvalidToken() from exc
        except ValueError as exc:
            # verify_id_token raises a bare ValueError for input that is not a
            # JWT at all ("", "abc", a JSON blob), before the signature check.
            raise InvalidToken() from exc
