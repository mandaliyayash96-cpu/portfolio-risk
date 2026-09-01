"""
Auth API.

Thin, like every other view module here: no ORM, no maths, no envelope
building. The interesting work happened before either view ran - the token was
verified and the identity resolved by `accounts.authentication`, so by the time
a function below executes, `request.user` is a real investor.

Both endpoints return the SAME payload shape (accounts.selectors.
serialize_session), so the frontend stores one object whether it just signed in
or is rehydrating after a page reload.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsAppUser
from accounts.selectors import get_my_portfolio, serialize_session
from accounts.services import start_session


@api_view(["POST"])
@permission_classes([IsAppUser])
def session(request):
    """
    POST /api/auth/session/

    Called by the frontend the moment Firebase confirms the OTP. Verifies the
    ID token (in the authentication class), makes sure the account and its
    portfolio exist, and hands back {user_id, phone, portfolio_id}.

    Headers:
        Authorization: Bearer <firebase_id_token>

    The body is IGNORED - deliberately, and this is the security property the
    whole part turns on. The phone number comes out of the verified token and
    nowhere else, so posting somebody else's number changes nothing.

    Idempotent: the second call from the same number returns the same three
    ids as the first. `first_login` says which it was, so the client can route
    a brand-new investor to onboarding instead of to an empty dashboard.
    """
    # request.auth is the decoded claims dict the authentication class returned.
    claims = request.auth if isinstance(request.auth, dict) else {}
    app_user, portfolio, first_login = start_session(
        request.user.app_user, firebase_uid=claims.get("uid")
    )
    return Response({**serialize_session(app_user, portfolio), "first_login": first_login})


@api_view(["GET"])
@permission_classes([IsAppUser])
def me(request):
    """
    GET /api/auth/me/

    Who the bearer of this token is, and which portfolio is theirs. The
    frontend calls it on boot to decide between the dashboard and the login
    screen, so it is a pure read: no login stamp, no row creation.

    404 {"code": "not_found"} when the account somehow has no portfolio -
    deleted since the last sign-in. The fix is another POST /api/auth/session/,
    which the message says.
    """
    app_user = request.user.app_user
    return Response(serialize_session(app_user, get_my_portfolio(request.user)))
