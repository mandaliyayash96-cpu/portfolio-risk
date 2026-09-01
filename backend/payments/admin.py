"""
Admin for payments.

Entirely read-only. Every field here is either set by Razorpay or derived from
a signature check, and a payment a human typed in is a payment nobody made -
being able to hand out a `paid` row from this screen would make the signature
check decorative. Refunds and disputes belong in the Razorpay dashboard, which
is the system of record for the money.
"""

from django.contrib import admin

from payments.models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "razorpay_order_id",
        "user",
        "status",
        "rupees",
        "consumed",
        "paid_at",
        "created_at",
    )
    list_filter = ("status", "consumed", "currency")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "user__username")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Amount", ordering="amount")
    def rupees(self, obj: Payment) -> str:
        """Paise are what we store; rupees are what a person reads."""
        return f"{obj.currency} {obj.amount / 100:.2f}"
