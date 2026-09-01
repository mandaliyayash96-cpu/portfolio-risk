"""
Root URL configuration.

Phase 1 exposed the admin plus a health endpoint proving the
{success, data, error} envelope is wired end to end; Phase 4 adds the risk
report at /api/risk/<portfolio_id>/, Phase 5 the Markowitz suggestion at
/api/rebalance/<portfolio_id>/, Phase 6 the alert rule config at /api/alerts/
and Phase 8 user-side holdings entry at /api/portfolio/<portfolio_id>/holdings/
- the first write path that is not the admin. Part 1 of the auth work adds
/api/auth/, which is what turns a verified phone number into a user and a
portfolio of their own.

Only HTTP is described here. The Phase 6 WebSocket feed is a different protocol
with a different router - see config/asgi.py and alerts/routing.py.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("common.urls")),
    # Phone auth. /api/auth/session/ is what the frontend calls the instant
    # Firebase confirms an OTP; /api/auth/me/ is the boot-time "who am I".
    # Neither takes an id - both address whoever holds the token.
    path("api/auth/", include("accounts.urls")),
    # TODO Phase 2: path("api/marketdata/", include("marketdata.urls"))
    # Holdings entry from the dashboard: add one by hand, bulk-load a CSV, or
    # delete a row. The portfolio LIST endpoint is still to come - these are
    # addressed by an id the client already has.
    path("api/portfolio/", include("portfolio.urls")),
    # /api/risk/<id>/, /api/rebalance/<id>/ and /api/performance/<id>/
    path("api/", include("risk.urls")),
    # Rule config and acknowledgement. The alert FEED is not here - it is a
    # WebSocket, routed by alerts/routing.py through config/asgi.py.
    path("api/alerts/", include("alerts.urls")),
]

admin.site.site_header = "Portfolio Risk Admin"
admin.site.site_title = "Portfolio Risk"
admin.site.index_title = "Investor Portfolio Monitoring & Risk Management"
