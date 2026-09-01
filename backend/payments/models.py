"""
Payments: one ₹9 charge buys one round of edits.

WHAT A ROW HERE MEANS
---------------------
A Payment is an UNLOCK GRANT and its receipt at once. It is created when the
user asks to edit (status=created, nothing charged yet), becomes `paid` when a
Razorpay signature verifies, and is `consumed` when that round of editing ends.

    created ──signature verifies──> paid ──round ends──> paid + consumed
       └───────────────abandoned / never paid───────────> created (dead)

One PAID, UNCONSUMED, UNEXPIRED row is exactly one live grant. See
`payments/services.py` for the three ways a round ends; the short version is
that a grant is not a subscription and cannot outlive its twenty minutes.

WHY `amount` IS AN INTEGER AND NOT A DECIMAL
--------------------------------------------
common/MONEY.md says money is Decimal, and this is the one place that is
deliberately not. `amount` is PAISE - a count of the smallest indivisible unit,
which is exactly what the Razorpay API speaks and what its signature covers.
₹9 is the integer 900, and there is no fractional paisa for a rounding error to
hide in. Storing 9.00 here would mean converting on every call to and from the
gateway, and a conversion is the only way this number can go wrong.
"""

from django.conf import settings
from django.db import models

from common.models import TimeStampedModel

#: Razorpay ids are short opaque strings ("order_Nq1a2B3c4D5e6F"); 64 is slack.
RAZORPAY_ID_MAX_LENGTH = 64


class PaymentStatus(models.TextChoices):
    CREATED = "created", "Created"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"


class Payment(TimeStampedModel):
    """One attempt to buy one editing round."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    razorpay_order_id = models.CharField(
        max_length=RAZORPAY_ID_MAX_LENGTH,
        unique=True,
        db_index=True,
        help_text="The order this row was created for. Unique: one order, one row.",
    )
    razorpay_payment_id = models.CharField(
        max_length=RAZORPAY_ID_MAX_LENGTH,
        blank=True,
        default="",
        help_text="Set only once a signature has verified. Empty means unpaid.",
    )
    amount = models.PositiveIntegerField(help_text="In paise. ₹9 is 900.")
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=8, choices=PaymentStatus, default=PaymentStatus.CREATED, db_index=True
    )
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the signature verified. The clock the 20-minute grant runs on.",
    )
    consumed = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True once the editing round this paid for has ended.",
    )
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When, and - with services.py - implicitly why.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The gate's query, on every write: "does this user hold a live
            # grant". Ordered as the filter is - user first, then the two flags.
            models.Index(
                fields=["user", "status", "consumed"], name="payment_user_status_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.razorpay_order_id} ({self.status})"

    @property
    def is_paid(self) -> bool:
        return self.status == PaymentStatus.PAID
