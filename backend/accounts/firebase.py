"""
The only module in the project that imports `firebase_admin`.

Same containment rule market data lives under (architecture rule 4: nothing
outside `marketdata/` imports yfinance). Everything else in `accounts/` talks to
Firebase through `verify_token()` and the exception aliases re-exported below,
so swapping the identity provider later is a change to one file, and so the
test suite has exactly ONE seam to patch:

    mock.patch("accounts.firebase.verify_token", ...)

Callers must reach it as `firebase.verify_token(...)` rather than importing the
name directly, or that patch would not be seen.

WHAT IS AND IS NOT A SECRET HERE
--------------------------------
The service-account JSON at settings.FIREBASE_CREDENTIALS holds a private key.
Its PATH is logged (it is a filename in the repo directory and it is the single
most useful thing to see when startup fails); its CONTENTS never are, and no ID
token is ever logged either - a Firebase ID token is a bearer credential, so a
log line carrying one is a log line that can sign somebody in.
"""

import logging
from pathlib import Path

import firebase_admin
from django.conf import settings
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

logger = logging.getLogger(__name__)


class FirebaseNotConfigured(RuntimeError):
    """
    The Admin SDK has no credentials, so no token can be verified.

    A SERVER fault, not a client one - which is why authentication.py turns
    this into a 503 rather than a 401. Answering "your token is invalid" when
    the truth is "this deployment forgot its service account" sends whoever is
    debugging it to the wrong side of the wire.
    """


# Re-exported so `accounts.authentication` can name the failure modes without
# importing firebase_admin itself.
#
# ORDER MATTERS TO CALLERS: ExpiredIdTokenError and RevokedIdTokenError are
# both SUBCLASSES of InvalidIdTokenError, so an `except` on the parent placed
# first would swallow them and report an expired token as a malformed one.
ExpiredIdTokenError = firebase_auth.ExpiredIdTokenError
RevokedIdTokenError = firebase_auth.RevokedIdTokenError
InvalidIdTokenError = firebase_auth.InvalidIdTokenError
UserDisabledError = firebase_auth.UserDisabledError
CertificateFetchError = firebase_auth.CertificateFetchError


def credentials_path() -> Path | None:
    """
    Absolute path to the service-account JSON, or None if unset.

    A relative FIREBASE_CREDENTIALS is resolved against BASE_DIR (backend/),
    which is where the file actually lives and what the .env holds - a bare
    filename. Resolving it here rather than at read time means `manage.py` run
    from any working directory finds the same file.
    """
    raw = (getattr(settings, "FIREBASE_CREDENTIALS", "") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


def is_initialised() -> bool:
    """True once a firebase_admin app exists in this process."""
    try:
        firebase_admin.get_app()
    except ValueError:
        return False
    return True


def init_firebase() -> bool:
    """
    Initialise the Admin SDK once per process. Returns True when usable.

    Called from `AccountsConfig.ready()`, so it runs at startup for runserver,
    daphne, celery and pytest alike - and is idempotent, because `ready()` can
    fire more than once (the autoreloader, a second WSGI worker thread) and
    firebase_admin raises on a duplicate default app.

    NEVER RAISES. A missing or unreadable credentials file must not stop the
    process from booting: the risk API, the admin and the whole test suite work
    without Firebase, and only the auth endpoints need it. The failure is
    logged at startup and surfaces again as a 503 on the first request that
    actually tries to verify a token.
    """
    if is_initialised():
        return True

    path = credentials_path()
    if path is None:
        logger.warning(
            "Firebase disabled: FIREBASE_CREDENTIALS is not set in backend/.env. "
            "Phone authentication will answer 503 until it is."
        )
        return False
    if not path.exists():
        logger.warning("Firebase disabled: no service-account file at %s", path)
        return False

    try:
        firebase_admin.initialize_app(credentials.Certificate(str(path)))
    except Exception as exc:  # noqa: BLE001 - startup must survive any of them
        # Type + message only. Never the exception's context, which for a
        # malformed certificate can quote the file it was parsing.
        logger.error("Firebase failed to initialise from %s: %s", path, type(exc).__name__)
        return False

    logger.info("Firebase initialised from %s", path.name)
    return True


def verify_token(id_token: str) -> dict:
    """
    Verify a Firebase ID token and return its decoded claims.

    Signature, expiry, audience and issuer are all checked by the SDK against
    Google's public certificates - this is the whole reason the backend holds a
    service account at all. The claims dict it returns (uid, phone_number, ...)
    is the ONLY trustworthy statement about who is calling.

    Raises:
        FirebaseNotConfigured: no credentials in this process.
        firebase_admin.auth.*: the token was rejected. See the aliases above.
    """
    if not is_initialised() and not init_firebase():
        raise FirebaseNotConfigured(
            "Firebase Admin SDK is not initialised; check FIREBASE_CREDENTIALS."
        )
    return firebase_auth.verify_id_token(id_token)
