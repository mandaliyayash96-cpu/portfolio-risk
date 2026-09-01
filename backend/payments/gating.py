"""
The gate other apps call.

One function, in its own module so that `portfolio/views.py` imports the POLICY
rather than the payments service layer - the holdings views should not know
what a Payment row looks like, only that editing is gated and who may pass.

Two refusals, and the difference matters to the dashboard:

    401 not_authenticated  - nobody is signed in. Sign in first.
    402 payment_required   - signed in, no live grant. Pay ₹9.
"""

from rest_framework.exceptions import NotAuthenticated

from payments.services import require_unlock


def require_editing_unlock(request):
    """
    Refuse a holdings write unless the caller has paid for this round.

    Reads never call this. Viewing the dashboard - the risk report, the charts,
    the holdings table - is free and stays free; only the three endpoints that
    CHANGE a portfolio are gated.

    Returns the live grant so a caller can log against it; the common case
    ignores the return value.

    Raises:
        NotAuthenticated: 401. Editing is tied to an account because a grant is
            tied to an account - there is nobody to have paid otherwise.
        PaymentRequiredError: 402, from `payments.services.require_unlock`.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        raise NotAuthenticated("Sign in to edit your holdings.")
    return require_unlock(user)
