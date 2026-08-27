"""Risk endpoints, mounted at /api/risk/ by config/urls.py."""

from django.urls import path

from risk import views

app_name = "risk"

urlpatterns = [
    path("<int:portfolio_id>/", views.risk_report, name="report"),
    # TODO Phase 5: path("<int:portfolio_id>/optimize/", views.optimize)
]
