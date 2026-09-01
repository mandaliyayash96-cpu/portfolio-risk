"""
Payment writes, and the definition of an editing round.

WHAT ONE ₹9 BUYS
================
One paid, unconsumed, unexpired Payment is one live grant, and a live grant
permits any number of holdings edits until the ROUND ENDS. A round ends -
`consumed=True` - at whichever of these comes first:

  1. THE USER CLOSES THE PANEL.  `finish_editing_round()`, called by the
     dashboard's Close button. The normal path, and the one the price is
     described in: "₹9 per editing session".

  2. A NEW ORDER IS STARTED.     `open_unlock_order()` retires whatever was
     outstanding before it creates anything. So a reload that leads to a second
     payment can never leave two live grants, and the abandoned one cannot be
     resurrected afterwards.

  3. TWENTY MINUTES PASS.        Enforced by the TTL filter in selectors.py.
     Conditions 1 and 2 are both triggered by the client; this one is not,
     which is what makes "one ₹9 is not reusable forever" true regardless of
     what the browser does, fails to do, or is made to do.

A WRITE DOES NOT CONSUME THE GRANT. That is the whole difference between
charging per ROUND and charging per EDIT: adding three holdings and deleting a
fourth is one ₹9, because it is one visit to the panel.

THE HONEST LIMITATION
=====================
Between a reload and either condition 2 or 3, the grant is still live on the
server. The dashboard locks itself, but the API is the truth, so somebody who
reloads and then calls the endpoint by hand can keep editing for the remainder
of those twenty minutes without paying again. Closing that gap completely means
binding the grant to a nonce that dies with the page - which also means a
mid-round refresh charges ₹9 twice for one round of edits. That was judged the
worse failure: it takes money for nothing. The TTL bounds this one instead.
"""

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from payments.exceptions import (
    InvalidSignatureError,
    PaymentRequiredError,
    PaymentsUnavailableError,
)
from payments.models import Payment, PaymentStatus
from payments.selectors import active_unlock, serialize_payment, unlock_cutoff
from payments import gateway
from common.exceptions import InvalidInputError, NotFoundError

logger = logging.getLogger(__name__)


def _retire(queryset, *, reason: str) -> int:
    """
    Mark a set of grants spent. Returns how many.

    One UPDATE, and `consumed_at` is written with it so a support question -
    "why could I not edit" - has an answer with a time on it. The reason is
    logged rather than stored: a fourth column carrying an enum nobody filters
    on is a column that goes stale.
    """
    count = queryset.update(consumed=True, consumed_at=timezone.now())
    if count:
        logger.info("Retired %s editing grant(s): %s", count, reason)
    return count


def open_unlock_order(user) -> dict:
    """
    Start a round: retire anything outstanding, then create a Razorpay order.

    Returns exactly what the browser's checkout widget needs - the order id,
    the amount in paise, the currency, and the PUBLIC key id. The secret is not
    here, is not in the response, and is not in any log line this call makes.

    Retiring first is condition 2 above. It is deliberately unconditional: if
    the user is asking to buy a round, whatever they were holding is over -
    either they finished it and the close call was lost, or they reloaded and
    the dashboard has already locked itself. Leaving the old grant live would
    let one ₹9 quietly pay for two rounds.

    Raises:
        PaymentsUnavailableError: no keys, or Razorpay would not answer.
    """
    with transaction.atomic():
        _retire(
            Payment.objects.filter(user=user, status=PaymentStatus.PAID, consumed=False),
            reason="a new order was started",
        )

        receipt = f"unlock-{user.pk}-{uuid.uuid4().hex[:10]}"
        try:
            order = gateway.create_order(
                amount=settings.EDITING_UNLOCK_AMOUNT_PAISE,
                currency=settings.EDITING_UNLOCK_CURRENCY,
                receipt=receipt,
                notes={"purpose": "holdings-editing-round", "user_id": str(user.pk)},
            )
        except gateway.RazorpayNotConfigured as exc:
            logger.error("Cannot create an order: Razorpay keys are not configured.")
            raise PaymentsUnavailableError() from exc
        except Exception as exc:  # noqa: BLE001 - any SDK/transport failure
            # Type only. A Razorpay exception can echo the request it was
            # making, and that request is signed with the key.
            logger.error("Razorpay order creation failed: %s", type(exc).__name__)
            raise PaymentsUnavailableError() from exc

        payment = Payment.objects.create(
            user=user,
            razorpay_order_id=order["id"],
            # Trust OUR figures, not the echo: these are what we asked to
            # charge, and the signature check later covers what was paid.
            amount=settings.EDITING_UNLOCK_AMOUNT_PAISE,
            currency=settings.EDITING_UNLOCK_CURRENCY,
            status=PaymentStatus.CREATED,
        )

    logger.info("Created editing-unlock order %s", payment.razorpay_order_id)
    return {
        "order_id": payment.razorpay_order_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "key_id": gateway.public_key_id(),
    }


def verify_unlock_payment(user, *, order_id, payment_id, signature) -> dict:
    """
    Turn a signed callback into a live grant.

    The client posts what Razorpay's checkout handed it. None of it is trusted:
    the order must be one WE created FOR THIS USER, and the signature must be a
    genuine HMAC of "order|payment" under our secret. Only then does the row
    become paid, and only a paid row unlocks anything.

    Idempotent. Checkout can fire its handler twice, and a second call with the
    same valid payload returns the same already-paid row rather than resetting
    `paid_at` - which would silently extend a round that is already running.

    Raises:
        InvalidInputError: a field is missing.
        NotFoundError: no such order for this user. Deliberately the same
            answer for "never existed" and "belongs to somebody else".
        InvalidSignatureError: the signature did not verify (400).
        PaymentsUnavailableError: no keys, so nothing could be checked (503).
    """
    order_id = (order_id or "").strip()
    payment_id = (payment_id or "").strip()
    signature = (signature or "").strip()
    if not (order_id and payment_id and signature):
        raise InvalidInputError(
            "razorpay_order_id, razorpay_payment_id and razorpay_signature are all required."
        )

    try:
        payment = Payment.objects.get(razorpay_order_id=order_id, user=user)
    except Payment.DoesNotExist as exc:
        raise NotFoundError("No such payment order for this account.") from exc

    if payment.is_paid:
        return serialize_payment(payment)

    try:
        verified = gateway.verify_signature(
            order_id=order_id, payment_id=payment_id, signature=signature
        )
    except gateway.RazorpayNotConfigured as exc:
        raise PaymentsUnavailableError() from exc

    if not verified:
        payment.status = PaymentStatus.FAILED
        payment.save(update_fields=["status", "updated_at"])
        raise InvalidSignatureError()

    payment.razorpay_payment_id = payment_id
    payment.status = PaymentStatus.PAID
    payment.paid_at = timezone.now()
    payment.consumed = False
    payment.save(
        update_fields=["razorpay_payment_id", "status", "paid_at", "consumed", "updated_at"]
    )

    logger.info("Editing unlock paid for order %s", order_id)
    return serialize_payment(payment)


def finish_editing_round(user) -> dict:
    """
    End the round: condition 1.

    Called when the user closes the holdings panel. Consumes every live grant
    they hold - normally exactly one - so the next round costs another ₹9.

    Safe to call when there is nothing to consume, which matters because the
    dashboard calls it on unmount as well as on the Close button, and those can
    both happen for one round.
    """
    consumed = _retire(
        Payment.objects.filter(user=user, status=PaymentStatus.PAID, consumed=False),
        reason="the user closed the editing panel",
    )
    return {"consumed": consumed, "unlocked": False}


def require_unlock(user) -> Payment:
    """
    The gate. Every holdings WRITE calls this first.

    Returns the live grant, so a caller that wants to log which round an edit
    belonged to can. Does NOT consume it - see the module docstring.

    Also retires expired grants on the way past. Cheap (it only fires when
    there is something stale), and it keeps `consumed` honest in the database
    rather than only in the query that reads it.

    Raises:
        PaymentRequiredError: 402, with the message the dashboard turns into
            its "Unlock editing ₹9" button.
    """
    _retire(
        Payment.objects.filter(
            user=user,
            status=PaymentStatus.PAID,
            consumed=False,
            paid_at__lt=unlock_cutoff(),
        ),
        reason="the twenty-minute editing window expired",
    )

    unlock = active_unlock(user)
    if unlock is None:
        raise PaymentRequiredError()
    return unlock
