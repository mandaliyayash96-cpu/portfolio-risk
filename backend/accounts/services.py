"""
Account writes: turn a verified phone number into a user with a portfolio.

Architecture rule 1 - the authentication class and the views below do no ORM
work; everything that creates or mutates a row is here, and every function is
idempotent, because the same phone number arrives on every single request the
signed-in client makes.
"""

import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import PHONE_MAX_LENGTH, AppUser
from common.exceptions import InvalidInputError
from portfolio.models import Portfolio

logger = logging.getLogger(__name__)

#: Every new investor gets exactly one portfolio with this name. It is also
#: half of Portfolio's (user, name) unique constraint, which is what makes
#: `ensure_portfolio` safe to call on every session request.
DEFAULT_PORTFOLIO_NAME = "My Portfolio"


def normalise_phone(phone_number: str | None) -> str:
    """
    Trim and length-check the token's phone claim.

    Deliberately NOT a reformatter. Firebase emits E.164 and that string is the
    unique key: "improving" it here (stripping a +, adding a country code)
    would mean the same person hashing to two different AppUsers the day the
    normalisation rule changes.
    """
    phone = (phone_number or "").strip()
    if not phone or len(phone) > PHONE_MAX_LENGTH:
        raise InvalidInputError("A valid phone number is required.")
    return phone


def _unique_username(phone: str) -> str:
    """
    A free `auth.User.username` for this number.

    The phone number itself, normally: Django's default username validator
    accepts digits and "+", and a readable username is worth a great deal in
    the admin. A collision is only possible against a pre-existing hand-made
    account, so the fallback is a short suffix rather than a scheme everyone
    has to read.
    """
    User = get_user_model()
    if not User.objects.filter(username=phone).exists():
        return phone
    return f"{phone}-{uuid.uuid4().hex[:6]}"[: User._meta.get_field("username").max_length]


@transaction.atomic
def _create_app_user(phone: str, firebase_uid: str) -> AppUser:
    """
    First login: the auth.User, the AppUser and the portfolio, or none of them.

    Atomic because a half-built identity is worse than no identity - an AppUser
    whose portfolio creation failed would answer /api/auth/me/ with a 404
    forever, and nothing would ever retry it.
    """
    user = get_user_model().objects.create_user(username=_unique_username(phone))
    # create_user with no password calls set_unusable_password(), so these rows
    # cannot be signed into with a password at /admin - the phone is the only
    # way in, which is the point.
    app_user = AppUser.objects.create(user=user, phone_number=phone, firebase_uid=firebase_uid)
    ensure_portfolio(app_user)
    return app_user


def resolve_app_user(phone_number: str | None, firebase_uid: str | None = None) -> AppUser:
    """
    The AppUser for a verified phone number, creating it on first sight.

    Called from `FirebaseAuthentication` on EVERY authenticated request, so the
    returning-user path is one SELECT and no writes. `last_login_at` and the
    uid are refreshed by `start_session` instead - a write on every request
    would be a write on every request.

    `phone_number` must come from a verified token. There is no code path in
    this project that passes it a value read off a request body.
    """
    phone = normalise_phone(phone_number)
    uid = (firebase_uid or "").strip()

    try:
        return AppUser.objects.select_related("user").get(phone_number=phone)
    except AppUser.DoesNotExist:
        pass

    try:
        app_user = _create_app_user(phone, uid)
    except IntegrityError:
        # Two first-ever requests from the same new number raced (the frontend
        # calls /session/ and /me/ back to back). The unique constraint on
        # phone_number decided the winner; re-read its row rather than
        # returning a second identity.
        return AppUser.objects.select_related("user").get(phone_number=phone)

    logger.info("Created account for a new phone number (app_user=%s)", app_user.pk)
    return app_user


def ensure_portfolio(app_user: AppUser) -> Portfolio:
    """
    The investor's own portfolio, created if it is not there yet.

    Idempotent via Portfolio's (user, name) unique constraint: a second call
    finds the row rather than adding "My Portfolio" twice. Returns the EXISTING
    portfolio for a returning user even if they renamed it, because the lookup
    below is by user first - see `get_my_portfolio`.
    """
    from accounts.selectors import find_my_portfolio

    existing = find_my_portfolio(app_user.user)
    if existing is not None:
        return existing

    portfolio, _ = Portfolio.objects.get_or_create(
        user=app_user.user,
        name=DEFAULT_PORTFOLIO_NAME,
        defaults={"base_currency": getattr(settings, "DEFAULT_BASE_CURRENCY", "INR")},
    )
    return portfolio


def start_session(
    app_user: AppUser, firebase_uid: str | None = None
) -> tuple[AppUser, Portfolio, bool]:
    """
    What POST /api/auth/session/ does once the token has verified.

    Guarantees the portfolio exists (a returning user whose portfolio was
    deleted gets a fresh one here rather than a 404 on every report), records
    the login, and refreshes the stored uid. This is the only write path that
    runs per-LOGIN rather than per-request.

    Returns (app_user, portfolio, first_login). `first_login` is read from
    last_login_at BEFORE it is stamped, so it means exactly "this number has
    never completed a session before" - the signal the dashboard needs to send
    somebody to onboarding instead of to an empty portfolio.
    """
    first_login = app_user.last_login_at is None
    portfolio = ensure_portfolio(app_user)

    updates = {"last_login_at": timezone.now()}
    uid = (firebase_uid or "").strip()
    if uid and uid != app_user.firebase_uid:
        updates["firebase_uid"] = uid

    for field, value in updates.items():
        setattr(app_user, field, value)
    # updated_at is auto_now, so it is written whatever `update_fields` says
    # only if it is listed - name it explicitly.
    app_user.save(update_fields=[*updates, "updated_at"])

    return app_user, portfolio, first_login
