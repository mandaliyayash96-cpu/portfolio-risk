"""
Risk endpoints, mounted at /api/ by config/urls.py.

Both paths are spelled in full here rather than sharing a prefix, because they
are different resources computed from one service module - /api/risk/ is the
measurement, /api/rebalance/ is the suggestion, /api/performance/ is the same
window with its time axis kept instead of reduced to scalars, and
/api/risk/<id>/report.pdf is the measurement as a document.
"""

from django.urls import path

from risk import views

app_name = "risk"

urlpatterns = [
    path("risk/<int:portfolio_id>/", views.risk_report, name="report"),
    path("rebalance/<int:portfolio_id>/", views.rebalance_report, name="rebalance"),
    path("performance/<int:portfolio_id>/", views.performance_report, name="performance"),
    # The same report as risk/<id>/, as a downloadable document. No trailing
    # slash: it names a FILE, and the extension is what makes a browser and an
    # operating system treat the download correctly.
    path("risk/<int:portfolio_id>/report.pdf", views.risk_report_pdf, name="report-pdf"),
]
