"""
Admin for the phone identities.

Read-mostly on purpose. Accounts are created by a verified token, never by
hand: typing a phone number in here would produce an identity that no token
maps to, and editing an existing one would hand somebody else's portfolio to a
different number. So the phone is editable only on the (rare) add form and the
uid is never editable at all.
"""

from django.contrib import admin

from accounts.models import AppUser


@admin.register(AppUser)
class AppUserAdmin(admin.ModelAdmin):
    list_display = ("phone_number", "user", "portfolio_count", "last_login_at", "created_at")
    search_fields = ("phone_number", "user__username")
    ordering = ("-created_at",)
    readonly_fields = ("firebase_uid", "created_at", "updated_at", "last_login_at")
    raw_id_fields = ("user",)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        return (*self.readonly_fields, "phone_number")

    @admin.display(description="Portfolios")
    def portfolio_count(self, obj: AppUser) -> int:
        return obj.user.portfolios.count()
