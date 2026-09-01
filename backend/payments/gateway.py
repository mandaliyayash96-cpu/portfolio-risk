"""
The only module in the project that imports `razorpay`.

Same containment rule as `accounts/firebase.py` for firebase-admin and
`marketdata/providers.py` for yfinance (architecture rule 4). Everything else
in `payments/` calls `create_order()` and `verify_signature()`, which means the
test suite mocks ONE function to stay off the network, and swapping the
processor later is a change to one file.

WHAT IS SECRET HERE, AND WHAT IS NOT
------------------------------------
`RAZORPAY_KEY_SECRET` signs and verifies payments. It never leaves this
process: it is not returned by any endpoint, not rendered into any template,
and not logged - not even truncated, because the useful half of a secret is
still a secret. `RAZORPAY_KEY_ID` is public by design (the browser's checkout
widget is handed it) and is the only one of the two that any response carries.

WHAT IS AND IS NOT A NETWORK CALL
---------------------------------
`create_order` talks to Razorpay over HTTPS. `verify_signature` does NOT - it
is an HMAC-SHA256 of "order_id|payment_id" keyed by the secret, computed
locally, which is why the whole verification path is testable with no network
and no mock beyond a test key.
"""

import logging

import razorpay
from django.conf import settings
from razorpay.errors import SignatureVerificationError

logger = logging.getLogger(__name__)

#: Razorpay caps `receipt` at 40 characters and rejects anything longer.
RECEIPT_MAX_LENGTH = 40


class RazorpayNotConfigured(RuntimeError):
    """
    No API keys in this process.

    A SERVER fault, so `payments/views.py` turns it into a 503 rather than
    letting a user believe their card was refused.
    """


def is_configured() -> bool:
    """True when both keys are present. Neither value is logged or returned."""
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def public_key_id() -> str:
    """
    The key the browser's checkout widget needs.

    Public by design - it identifies the merchant, it authorises nothing. The
    secret is what turns a payment into a verified one, and it stays here.
    """
    return settings.RAZORPAY_KEY_ID


def _client() -> razorpay.Client:
    """A configured client, or a clear failure. Built per call, not cached."""
    if not is_configured():
        raise RazorpayNotConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in backend/.env."
        )
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_order(*, amount: int, currency: str, receipt: str, notes: dict | None = None) -> dict:
    """
    Create a Razorpay order. The one call in this app that hits the network.

    Args:
        amount: in PAISE (₹9 is 900), matching the API's own unit.
        currency: ISO 4217, "INR".
        receipt: our own reference, echoed back on the order. Truncated to the
            40 characters Razorpay accepts rather than being rejected by it.
        notes: arbitrary key/value pairs stored on the order. Useful in the
            dashboard when reconciling a payment to an account.

    Returns:
        The order as Razorpay describes it - `id`, `amount`, `currency`, ...

    Raises:
        RazorpayNotConfigured: no keys.
        razorpay.errors.*: the gateway refused or could not be reached.
    """
    return _client().order.create(
        {
            "amount": amount,
            "currency": currency,
            "receipt": receipt[:RECEIPT_MAX_LENGTH],
            "notes": notes or {},
            # Razorpay may capture automatically once the customer pays, rather
            # than leaving the funds authorised and waiting for a second call
            # this app has no screen for.
            "payment_capture": 1,
        }
    )


def verify_signature(*, order_id: str, payment_id: str, signature: str) -> bool:
    """
    Is this signature a genuine one for this order and payment?

    THE ONLY THING THAT MAKES A PAYMENT REAL. The browser tells us it paid; a
    browser can say anything. What it cannot do is produce an HMAC of
    "order_id|payment_id" keyed by a secret it has never seen. So this - and
    not the client's word, not the presence of a payment id, not a success
    callback - is what flips a row to `paid`.

    Offline: no request is made. Returns False rather than raising for a bad
    signature, because "this did not verify" is an expected answer here and the
    caller renders it as a 400.

    Raises:
        RazorpayNotConfigured: no keys, so nothing could be checked. NOT the
            same as a failed check, and deliberately not flattened into False.
    """
    client = _client()
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
    except SignatureVerificationError:
        # Logged without the signature, the payment id or any key: a failed
        # verification is worth knowing about, its contents are not.
        logger.warning("Rejected a Razorpay payment whose signature did not verify.")
        return False
    return True
