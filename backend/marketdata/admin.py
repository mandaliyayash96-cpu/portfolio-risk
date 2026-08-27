"""
Admin registrations for the marketdata app.

These tables are machine-written from Phase 2 onward, but stay editable here so
price rows can be seeded by hand while the provider is still being built.
"""

from django.contrib import admin

from marketdata.models import PriceHistory, PriceSnapshot


@admin.register(PriceSnapshot)
class PriceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("ticker", "price", "timestamp", "created_at")
    list_filter = ("ticker", "timestamp")
    search_fields = ("ticker",)
    date_hierarchy = "timestamp"
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-timestamp",)


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("ticker", "date", "close", "created_at")
    list_filter = ("ticker", "date")
    search_fields = ("ticker",)
    date_hierarchy = "date"
    readonly_fields = ("created_at", "updated_at")
    ordering = ("ticker", "-date")
