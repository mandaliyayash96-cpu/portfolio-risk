"""Permission classes for the account endpoints."""

from rest_framework.permissions import BasePermission

from accounts.selectors import is_app_user


class IsAppUser(BasePermission):
    """
    Requires an investor identity - a request carrying a verified phone number.

    Stricter than IsAuthenticated on purpose. A superuser with an /admin
    session cookie satisfies IsAuthenticated but has no AppUser and no
    portfolio, and /api/auth/me/ has nothing true to say about them; without
    this they would reach the view and fail on a missing profile.

    An ANONYMOUS request still gets 401 rather than 403, because DRF's
    `permission_denied` upgrades a denial to NotAuthenticated when no
    authenticator succeeded and a WWW-Authenticate challenge is available -
    which FirebaseAuthentication provides.
    """

    message = "A verified phone number is required."

    def has_permission(self, request, view) -> bool:
        return is_app_user(request.user)
