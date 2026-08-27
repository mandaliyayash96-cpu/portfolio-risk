"""
Root URL configuration.

Phase 1 exposed the admin plus a health endpoint proving the
{success, data, error} envelope is wired end to end; Phase 4 adds the risk
report at /api/risk/<portfolio_id>/, Phase 5 the Markowitz suggestion at
/api/rebalance/<portfolio_id>/ and Phase 6 the alert rule config at
/api/alerts/.

Only HTTP is described here. The Phase 6 WebSocket feed is a different protocol
with a different router - see config/asgi.py and alerts/routing.py.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("common.urls")),
    # TODO Phase 2: path("api/marketdata/", include("marketdata.urls"))
    # TODO Phase 4: path("api/portfolios/", include("portfolio.urls"))
    path("api/", include("risk.urls")),  # /api/risk/<id>/ and /api/rebalance/<id>/
    # Rule config and acknowledgement. The alert FEED is not here - it is a
    # WebSocket, routed by alerts/routing.py through config/asgi.py.
    path("api/alerts/", include("alerts.urls")),
]

admin.site.site_header = "Portfolio Risk Admin"
admin.site.site_title = "Portfolio Risk"
admin.site.index_title = "Investor Portfolio Monitoring & Risk Management"
