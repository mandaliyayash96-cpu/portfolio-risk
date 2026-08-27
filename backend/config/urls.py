"""
Root URL configuration.

Phase 1 exposes the admin plus a single health endpoint that proves the
{success, data, error} envelope is wired end to end.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("common.urls")),
    # TODO Phase 2: path("api/marketdata/", include("marketdata.urls"))
    # TODO Phase 4: path("api/portfolios/", include("portfolio.urls"))
    # TODO Phase 4: path("api/risk/", include("risk.urls"))
    # TODO Phase 6: path("api/alerts/", include("alerts.urls"))
]

admin.site.site_header = "Portfolio Risk Admin"
admin.site.site_title = "Portfolio Risk"
admin.site.index_title = "Investor Portfolio Monitoring & Risk Management"
