"""
Identity: the phone number Firebase verified, and the Django user it maps to.

WHY THIS IS A PROFILE AND NOT `AUTH_USER_MODEL`
-----------------------------------------------
The obvious shape for "a user keyed by phone number" is a custom user model
with USERNAME_FIELD = "phone_number". It is the wrong shape HERE, for one
reason: `AUTH_USER_MODEL` cannot be swapped once migrations have run.
`portfolio`, `alerts` and `django.contrib.admin` all carry migrations with a
dependency on `auth.User` already, and changing it now means throwing away
db.sqlite3 - including portfolio 1, which the rest of the build is tested
against.

So AppUser is a PROFILE: `phone_number` is the identity Firebase asserts, and
each one owns exactly one `auth.User` row created alongside it. That keeps
`Portfolio.user -> settings.AUTH_USER_MODEL` untouched, keeps DRF's
`IsAuthenticated` and `request.user.is_authenticated` working with no shims,
and leaves the door open to a real custom user model at the next migration
reset. `FirebaseAuthentication` sets `request.user` to the wrapped Django user;
`request.user.app_user` walks back to the phone number.
"""

from django.conf import settings
from django.db import models

from common.models import TimeStampedModel

#: E.164 with room to spare ("+919876543210" is 13). Firebase always hands back
#: the E.164 form, so this is generous rather than tight.
PHONE_MAX_LENGTH = 20

#: Firebase uids are 28 chars today; the field is sized for a provider that
#: disagrees rather than for the current one.
FIREBASE_UID_MAX_LENGTH = 128


class AppUser(TimeStampedModel):
    """
    One verified phone number, one Django user, one auto-created portfolio.

    Rows here are created ONLY by `accounts.services.resolve_app_user`, only
    from a phone number that came out of a verified Firebase ID token. Nothing
    in this app accepts a phone number from a request body.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_user",
        help_text="The auth.User row that owns this identity's portfolios.",
    )
    phone_number = models.CharField(
        max_length=PHONE_MAX_LENGTH,
        unique=True,
        db_index=True,
        help_text="E.164, exactly as the verified Firebase token spelled it.",
    )
    firebase_uid = models.CharField(
        max_length=FIREBASE_UID_MAX_LENGTH,
        blank=True,
        default="",
        # NOT unique, deliberately. The phone number is the key: if an account
        # is deleted in the Firebase console and the same number signs in
        # again, Firebase issues a NEW uid for the SAME person - and a unique
        # constraint here would lock that user out of their own portfolio
        # rather than quietly re-pointing at their history.
        help_text="Latest Firebase uid seen for this number. Diagnostic, not a key.",
    )
    last_login_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set by POST /api/auth/session/, not by every authenticated request.",
    )

    class Meta:
        ordering = ["phone_number"]
        verbose_name = "app user"
        verbose_name_plural = "app users"

    def __str__(self) -> str:
        return self.phone_number
