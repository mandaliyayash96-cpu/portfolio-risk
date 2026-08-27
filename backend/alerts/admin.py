"""
Admin registrations for the alerts app.

Rules are configured here; events are written by the Phase 6 scan task and are
listed read-only apart from the acknowledge action.
"""

from django.contrib import admin

from alerts.models import AlertEvent, AlertRule


class AlertEventInline(admin.TabularInline):
    model = AlertEvent
    extra = 0
    fields = ("triggered_at", "value", "message", "acknowledged")
    readonly_fields = ("triggered_at", "value", "message")
    ordering = ("-triggered_at",)
    show_change_link = True


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("portfolio", "metric", "operator", "threshold", "active", "event_count")
    list_filter = ("active", "metric", "operator", "portfolio")
    search_fields = ("portfolio__name",)
    autocomplete_fields = ("portfolio",)
    list_editable = ("active",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (AlertEventInline,)
    list_select_related = ("portfolio",)

    @admin.display(description="Events")
    def event_count(self, obj: AlertRule) -> int:
        return obj.events.count()


@admin.register(AlertEvent)
class AlertEventAdmin(admin.ModelAdmin):
    list_display = ("triggered_at", "rule", "value", "acknowledged", "message")
    list_filter = ("acknowledged", "triggered_at", "rule__metric")
    search_fields = ("message", "rule__portfolio__name")
    autocomplete_fields = ("rule",)
    date_hierarchy = "triggered_at"
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("rule", "rule__portfolio")
    actions = ("mark_acknowledged",)

    @admin.action(description="Mark selected events acknowledged")
    def mark_acknowledged(self, request, queryset):
        updated = queryset.update(acknowledged=True)
        self.message_user(request, f"{updated} event(s) acknowledged.")
