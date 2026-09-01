"""
The payment endpoints, end to end through DRF.

The signature check is the load-bearing test in this file. Everything else -
order creation, the envelope, who owns which order - is plumbing around one
question: can a client that did not pay make the server believe it did.
"""

import pytest

from payments.models import Payment, PaymentStatus
from payments.tests.conftest import FAKE_ORDER_ID, TEST_KEY_ID, sign

pytestmark = pytest.mark.django_db

ORDER_URL = "/api/payments/order/"
VERIFY_URL = "/api/payments/verify/"
FINISH_URL = "/api/payments/finish/"

PAYMENT_ID = "pay_TESTFAKE0001"


def body(response) -> dict:
    return response.json()


class TestCreateOrder:
    def test_returns_the_order_and_the_public_key(self, api, fake_order):
        with fake_order():
            response = api.post(ORDER_URL, {})

        assert response.status_code == 200
        data = body(response)["data"]
        assert data["order_id"] == FAKE_ORDER_ID
        assert data["amount"] == 900
        assert data["currency"] == "INR"
        assert data["key_id"] == TEST_KEY_ID

    def test_saves_a_created_payment_for_this_user(self, api, fake_order, app_user):
        with fake_order():
            api.post(ORDER_URL, {})

        payment = Payment.objects.get(razorpay_order_id=FAKE_ORDER_ID)
        assert payment.user_id == app_user.user_id
        assert payment.status == PaymentStatus.CREATED
        assert payment.amount == 900
        assert payment.paid_at is None

    def test_the_response_never_carries_the_secret(self, api, fake_order, razorpay_keys):
        with fake_order():
            response = api.post(ORDER_URL, {})

        assert razorpay_keys.RAZORPAY_KEY_SECRET not in response.content.decode()

    def test_the_amount_charged_is_ours_not_the_clients(self, api, fake_order):
        """A client-supplied amount is a client-supplied price."""
        with fake_order() as create:
            api.post(ORDER_URL, {"amount": 1, "currency": "USD"})

        assert create.call_args.kwargs["amount"] == 900
        assert create.call_args.kwargs["currency"] == "INR"

    def test_a_new_order_retires_an_outstanding_grant(self, api, fake_order, paid_unlock):
        """
        Condition 2 of "when does a round end". Without this, a reload that led
        to a second payment would leave the user holding two live grants and
        one ₹9 would quietly have bought two rounds.
        """
        standing = paid_unlock()

        with fake_order():
            api.post(ORDER_URL, {})

        standing.refresh_from_db()
        assert standing.consumed is True
        assert standing.consumed_at is not None

    def test_anonymous_callers_are_401(self, anon, fake_order):
        with fake_order():
            response = anon.post(ORDER_URL, {})

        assert response.status_code == 401
        assert Payment.objects.count() == 0

    def test_missing_keys_are_503_not_a_payment_failure(self, api, razorpay_keys):
        razorpay_keys.RAZORPAY_KEY_ID = ""
        razorpay_keys.RAZORPAY_KEY_SECRET = ""

        response = api.post(ORDER_URL, {})

        assert response.status_code == 503
        assert body(response)["error"]["code"] == "payments_unavailable"


class TestVerify:
    def _order(self, api, fake_order):
        with fake_order():
            api.post(ORDER_URL, {})
        return FAKE_ORDER_ID

    def test_a_correctly_signed_payload_marks_the_payment_paid(self, api, fake_order):
        order_id = self._order(api, fake_order)

        response = api.post(
            VERIFY_URL,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": sign(order_id, PAYMENT_ID),
            },
            format="json",
        )

        assert response.status_code == 200
        data = body(response)["data"]
        assert data["status"] == "paid"
        assert data["unlocked"] is True

        payment = Payment.objects.get(razorpay_order_id=order_id)
        assert payment.status == PaymentStatus.PAID
        assert payment.razorpay_payment_id == PAYMENT_ID
        assert payment.paid_at is not None
        assert payment.consumed is False

    def test_a_bad_signature_is_rejected_and_unlocks_nothing(self, api, fake_order):
        """The whole feature in one test: saying you paid is not paying."""
        order_id = self._order(api, fake_order)

        response = api.post(
            VERIFY_URL,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": "0" * 64,
            },
            format="json",
        )

        assert response.status_code == 400
        assert body(response)["error"]["code"] == "invalid_signature"

        payment = Payment.objects.get(razorpay_order_id=order_id)
        assert payment.status == PaymentStatus.FAILED
        assert payment.paid_at is None

    def test_a_signature_for_a_different_payment_id_is_rejected(self, api, fake_order):
        """
        The signature covers "order|payment", so a genuine signature lifted
        from another payment must not verify against this one.
        """
        order_id = self._order(api, fake_order)

        response = api.post(
            VERIFY_URL,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": sign(order_id, "pay_SOMETHING_ELSE"),
            },
            format="json",
        )

        assert response.status_code == 400
        assert body(response)["error"]["code"] == "invalid_signature"

    def test_a_signature_made_with_the_wrong_secret_is_rejected(self, api, fake_order):
        order_id = self._order(api, fake_order)

        response = api.post(
            VERIFY_URL,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": sign(order_id, PAYMENT_ID, secret="not-our-secret"),
            },
            format="json",
        )

        assert response.status_code == 400

    def test_somebody_elses_order_is_a_404(self, api, fake_order, other_app_user):
        """Even with a perfect signature: the order has to be yours."""
        order_id = self._order(api, fake_order)
        Payment.objects.filter(razorpay_order_id=order_id).update(user=other_app_user.user)

        response = api.post(
            VERIFY_URL,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": sign(order_id, PAYMENT_ID),
            },
            format="json",
        )

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "missing", ["razorpay_order_id", "razorpay_payment_id", "razorpay_signature"]
    )
    def test_a_missing_field_is_a_400_not_a_500(self, api, fake_order, missing):
        order_id = self._order(api, fake_order)
        payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": PAYMENT_ID,
            "razorpay_signature": sign(order_id, PAYMENT_ID),
        }
        payload.pop(missing)

        response = api.post(VERIFY_URL, payload, format="json")

        assert response.status_code == 400
        assert body(response)["error"]["code"] == "invalid_input"

    def test_verifying_twice_does_not_extend_the_round(self, api, fake_order):
        """
        Checkout can fire its handler twice. The second call must return the
        same row rather than re-stamping paid_at, which would silently restart
        a twenty-minute window that is already half spent.
        """
        order_id = self._order(api, fake_order)
        payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": PAYMENT_ID,
            "razorpay_signature": sign(order_id, PAYMENT_ID),
        }

        api.post(VERIFY_URL, payload, format="json")
        first = Payment.objects.get(razorpay_order_id=order_id).paid_at
        response = api.post(VERIFY_URL, payload, format="json")

        assert response.status_code == 200
        assert Payment.objects.get(razorpay_order_id=order_id).paid_at == first

    def test_anonymous_callers_are_401(self, anon):
        response = anon.post(VERIFY_URL, {}, format="json")

        assert response.status_code == 401


class TestFinish:
    def test_consumes_the_live_grant(self, api, paid_unlock):
        grant = paid_unlock()

        response = api.post(FINISH_URL, {})

        assert response.status_code == 200
        assert body(response)["data"] == {"consumed": 1, "unlocked": False}
        grant.refresh_from_db()
        assert grant.consumed is True

    def test_is_safe_with_nothing_outstanding(self, api):
        """Called from the Close button AND on unmount; both can fire for one round."""
        response = api.post(FINISH_URL, {})

        assert response.status_code == 200
        assert body(response)["data"]["consumed"] == 0

    def test_does_not_touch_another_users_grant(self, api, paid_unlock, other_app_user):
        theirs = paid_unlock(user=other_app_user.user)

        api.post(FINISH_URL, {})

        theirs.refresh_from_db()
        assert theirs.consumed is False
