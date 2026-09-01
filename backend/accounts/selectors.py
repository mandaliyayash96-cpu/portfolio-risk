"""
Account reads, and the seam that scopes the rest of the API to its caller.

Architecture rule 1: views never touch the ORM. `resolve_portfolio_id` is the
one function the risk / rebalance / performance / holdings views call to answer
"whose portfolio is this request about", so the answer lives in one place and
Part 3 can tighten it without editing six views.
"""

from common.exceptions import NotFoundError
from portfolio.models import Portfolio


def find_my_portfolio(user) -> Portfolio | None:
    """
    The user's own portfolio, or None. Never raises.

    Lowest pk when there are several: today every account has exactly one, and
    when multi-portfolio arrives this becomes "the default one" rather than
    changing meaning under the endpoints that call it.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return Portfolio.objects.filter(user=user).order_by("pk").first()


def get_my_portfolio(user) -> Portfolio:
    """
    The signed-in investor's own portfolio.

    Raises:
        NotFoundError: the account has no portfolio. Rare by construction -
            `accounts.services` creates one at first login and re-creates one
            on every /api/auth/session/ - so this means it was deleted since,
            and the client's fix is to call /api/auth/session/ again.
    """
    portfolio = find_my_portfolio(user)
    if portfolio is None:
        raise NotFoundError("This account has no portfolio yet. POST /api/auth/session/ first.")
    return portfolio


def is_app_user(user) -> bool:
    """
    True when `user` is an investor identity rather than some other login.

    The distinction matters because `auth.User` also holds the superuser who
    logs into /admin. That session is genuinely authenticated, but it is not an
    investor and it owns no portfolio, so it must NOT be scoped away from the
    portfolio id in the URL - that would break the admin's browsable API, and
    Django admin work, for no security gain.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    # A reverse OneToOne raises RelatedObjectDoesNotExist (a subclass of both
    # AppUser.DoesNotExist and AttributeError) when the row is absent, which is
    # exactly what hasattr() tests for - and unlike a try/except Exception it
    # cannot quietly hide an unrelated database error.
    return hasattr(user, "app_user")


def resolve_portfolio_id(request, url_portfolio_id: int) -> int:
    """
    Which portfolio a data endpoint should actually read.

    Signed in with a Firebase token  ->  THEIR portfolio, whatever the URL says.
    Anything else                    ->  the id in the URL, unchanged.

    The fallback is what keeps portfolio 1 curl-able while the frontend is
    still being built (Part 2), and it is also the whole of the remaining
    exposure: an anonymous caller can still read any portfolio by id.

    TODO Part 3 (enforce auth): add IsAuthenticated to the risk, rebalance,
    performance and holdings views, drop the fallback below, and have this
    function raise NotAuthenticated instead of trusting the URL. At that point
    the portfolio id in those URLs becomes decorative and can be removed.
    """
    if is_app_user(getattr(request, "user", None)):
        return get_my_portfolio(request.user).pk
    return url_portfolio_id


def serialize_session(app_user, portfolio: Portfolio) -> dict:
    """
    The identity payload both auth endpoints return.

    One shape from both, so the frontend stores the same object whether it just
    signed in (POST /session/) or is rehydrating a reload (GET /me/).

    `user_id` is the AppUser's id - the app-level identity - not the wrapped
    auth.User's. Nothing outside this app should need to know that the wrapped
    row exists.
    """
    return {
        "user_id": app_user.pk,
        "phone": app_user.phone_number,
        "portfolio_id": portfolio.pk,
        "portfolio_name": portfolio.name,
        "base_currency": portfolio.base_currency,
        "created_at": app_user.created_at.isoformat() if app_user.created_at else None,
        "last_login_at": app_user.last_login_at.isoformat() if app_user.last_login_at else None,
    }
