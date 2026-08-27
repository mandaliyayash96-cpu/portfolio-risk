"""
Admin registrations for the portfolio app.

Tuned for hand-building a demo portfolio quickly: holdings and transactions are
editable inline from the portfolio page.
"""

from django.contrib import admin

from portfolio.models import Holding, Portfolio, Transaction


class HoldingInline(admin.TabularInline):
    model = Holding
    extra = 1
    fields = ("ticker", "asset_type", "sector", "quantity", "avg_buy_price", "buy_date")
    show_change_link = True


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    fields = ("timestamp", "ticker", "side", "quantity", "price")
    ordering = ("-timestamp",)
    show_change_link = True


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "base_currency", "holding_count", "cost_basis", "created_at")
    list_filter = ("base_currency", "created_at")
    search_fields = ("name", "user__username", "user__email")
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (HoldingInline, TransactionInline)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user").prefetch_related("holdings")

    @admin.display(description="Holdings")
    def holding_count(self, obj: Portfolio) -> int:
        return obj.holdings.count()

    @admin.display(description="Cost basis")
    def cost_basis(self, obj: Portfolio):
        # TODO Phase 4: annotate this in a selector instead of summing in Python.
        return obj.total_cost_basis


@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = (
        "ticker",
        "portfolio",
        "asset_type",
        "sector",
        "quantity",
        "avg_buy_price",
        "cost_basis",
        "buy_date",
    )
    list_filter = ("asset_type", "sector", "buy_date", "portfolio")
    search_fields = ("ticker", "sector", "portfolio__name")
    autocomplete_fields = ("portfolio",)
    date_hierarchy = "buy_date"
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("portfolio",)

    @admin.display(description="Cost basis")
    def cost_basis(self, obj: Holding):
        return obj.cost_basis


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "portfolio", "ticker", "side", "quantity", "price", "gross_value")
    list_filter = ("side", "timestamp", "portfolio")
    search_fields = ("ticker", "portfolio__name")
    autocomplete_fields = ("portfolio",)
    date_hierarchy = "timestamp"
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("portfolio",)

    @admin.display(description="Gross value")
    def gross_value(self, obj: Transaction):
        return obj.gross_value
