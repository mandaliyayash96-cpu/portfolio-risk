"""
Payment reads (architecture rule 1: views never touch the ORM).

The one question this module exists to answer, asked on every holdings write:
does this user hold a live unlock right now.
"""

from django.conf import settings
from django.utils import timezone

from payments.models import Payment, PaymentStatus


def unlock_cutoff():
    """
    The oldest `paid_at` that still counts as a live grant.

    A grant is twenty minutes of editing, not a licence. The other two ways a
    round ends (the user closes the panel; a new order is started) are both
    triggered by the CLIENT - this one is not, which is what makes "one ₹9
    cannot be reused forever" true no matter what the client does or fails to
    do. A browser closed mid-round leaves a grant that dies on its own.
    """
    return timezone.now() - settings.EDITING_UNLOCK_TTL


def active_unlock(user) -> Payment | None:
    """
    The user's live unlock, or None.

    Live means all four of: paid, not consumed, not expired, and theirs. The
    oldest live one is returned so a user who somehow holds two spends them in
    the order they were bought.

    A pure read - it never retires the expired rows it filters out. Retiring is
    a write, and writes live in services.py; `open_unlock_order` and
    `finish_editing_round` both do it, so stale rows are tidied on the paths
    that already touch the table.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    return (
        Payment.objects.filter(
            user=user,
            status=PaymentStatus.PAID,
            consumed=False,
            paid_at__gte=unlock_cutoff(),
        )
        .order_by("paid_at")
        .first()
    )


def user_has_unlock(user) -> bool:
    """True when the user may edit holdings right now."""
    return active_unlock(user) is not None


def serialize_payment(payment: Payment) -> dict:
    """
    One payment as JSON.

    `amount` is emitted as an integer number of paise, the same unit it is
    stored and charged in - see the note in models.py. No key, public or
    secret, appears here.
    """
    return {
        "order_id": payment.razorpay_order_id,
        "payment_id": payment.razorpay_payment_id or None,
        "amount": payment.amount,
        "currency": payment.currency,
        "status": payment.status,
        "consumed": payment.consumed,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }
