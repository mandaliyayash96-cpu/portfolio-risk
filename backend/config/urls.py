"""
Root URL configuration.

Phase 1 exposed the admin plus a health endpoint proving the
{success, data, error} envelope is wired end to end; Phase 4 adds the risk
report at /api/risk/<portfolio_id>/ and Phase 5 the Markowitz
suggestion at /api/rebalance/<portfolio_id>/.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("common.urls")),
    # TODO Phase 2: path("api/marketdata/", include("marketdata.urls"))
    # TODO Phase 4: path("api/portfolios/", include("portfolio.urls"))
    path("api/", include("risk.urls")),  # /api/risk/<id>/ and /api/rebalance/<id>/
    # TODO Phase 6: path("api/alerts/", include("alerts.urls"))
]

admin.site.site_header = "Portfolio Risk Admin"
admin.site.site_title = "Portfolio Risk"
admin.site.index_title = "Investor Portfolio Monitoring & Risk Management"
