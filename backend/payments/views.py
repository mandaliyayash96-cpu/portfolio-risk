"""
Payments API.

Thin, like every other view module here: read the body, call a service, return
the dict. Three endpoints and no ids in any of them - every one addresses
"whoever holds this token", exactly as /api/auth/ does.

All three require a signed-in investor. A payment belongs to an account, and
`IsAppUser` is what makes `request.user` one - so an anonymous caller gets 401
and an admin session 403, neither of which reaches a service.
"""

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsAppUser
from payments.selectors import user_has_unlock
from payments.services import finish_editing_round, open_unlock_order, verify_unlock_payment


@api_view(["POST"])
@permission_classes([IsAppUser])
def create_order(request):
    """
    POST /api/payments/order/

    Start an editing round. Creates a ₹9 Razorpay order and returns what the
    browser's checkout widget needs:

        {"order_id": "order_...", "amount": 900, "currency": "INR",
         "key_id": "rzp_test_..."}

    `key_id` is the PUBLIC key - the widget cannot open without it. The secret
    is never in this response, and nothing is charged by this call: it creates
    an intent, and only a verified signature turns that into an unlock.

    Takes no body. The amount is a server-side constant, deliberately: an
    amount from the client is an amount the client can set to zero.

    Any grant the user was still holding is retired here - see the module
    docstring in services.py, condition 2.
    """
    return Response(open_unlock_order(request.user))


@api_view(["POST"])
@permission_classes([IsAppUser])
def verify(request):
    """
    POST /api/payments/verify/

    Body: {razorpay_order_id, razorpay_payment_id, razorpay_signature} -
    exactly what Razorpay's checkout handler hands the page.

    Verifies the signature against our key secret and, only then, marks the
    payment paid and the editing round open. A client that posts "I paid"
    without a valid signature gets a 400 and no unlock - the signature is the
    only thing here that cannot be forged by the caller.

    Returns the payment, plus `unlocked` so the dashboard has one boolean to
    branch on rather than inferring it from a status string.
    """
    body = request.data if isinstance(request.data, dict) else {}
    payment = verify_unlock_payment(
        request.user,
        order_id=body.get("razorpay_order_id"),
        payment_id=body.get("razorpay_payment_id"),
        signature=body.get("razorpay_signature"),
    )
    return Response({**payment, "unlocked": user_has_unlock(request.user)})


@api_view(["POST"])
@permission_classes([IsAppUser])
def finish(request):
    """
    POST /api/payments/finish/

    End the editing round - the user closed the panel. Consumes the live grant,
    so the next round of edits needs another ₹9.

    Idempotent and safe to call with nothing outstanding: the dashboard calls
    it both from the Close button and on unmount, and one round can easily
    produce both.
    """
    return Response(finish_editing_round(request.user))
