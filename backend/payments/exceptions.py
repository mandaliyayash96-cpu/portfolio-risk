"""
Payment-specific domain errors.

Defined here rather than in common/exceptions.py because they are this app's
vocabulary, not the project's - nothing outside `payments/` raises them, and
`custom_exception_handler` renders any DomainError subclass without needing to
know it exists. Every one arrives as the standard
{"success": false, "data": null, "error": {...}} envelope.
"""

from rest_framework import status as http_status

from common.exceptions import DomainError


class PaymentRequiredError(DomainError):
    """
    The action needs a paid, live unlock and the caller has none.

    402 rather than 403: the caller IS allowed to do this, and knows how to
    become allowed. 403 would say "not for you", which is wrong and gives the
    client nothing to act on - whereas 402 plus this code is precisely what the
    dashboard turns into an "Unlock editing ₹9" button.
    """

    code = "payment_required"
    status_code = http_status.HTTP_402_PAYMENT_REQUIRED
    message = "Editing is locked. Unlock a round of edits for ₹9 to continue."


class InvalidSignatureError(DomainError):
    """
    The signature did not verify, so no payment happened as far as we know.

    A 400, not a 401 or a 402: the request was well-formed and authenticated,
    and its CONTENT was wrong. Nothing is marked paid, and the message says
    nothing about which half of the signature failed - an attacker gets no
    oracle out of this.
    """

    code = "invalid_signature"
    status_code = http_status.HTTP_400_BAD_REQUEST
    message = "Payment signature could not be verified. Nothing has been charged as paid."


class PaymentsUnavailableError(DomainError):
    """
    Razorpay is not configured, or would not answer.

    503 and NOT an invalid-signature 400: the difference between "your payment
    is bad" and "we cannot take payments right now" is the difference between
    the user retrying forever and somebody looking at the server.
    """

    code = "payments_unavailable"
    status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    message = "Payments are temporarily unavailable. Please try again shortly."
