"""
Alert endpoints, mounted at /api/alerts/ by config/urls.py.

Rules are addressed by PORTFOLIO (they are a property of one), events by their
own id (acknowledging is a fact about that event, and the portfolio it belongs
to is already known from the feed it arrived in).
"""

from django.urls import path

from alerts import views

app_name = "alerts"

urlpatterns = [
    path("rules/<int:portfolio_id>/", views.rules, name="rules"),
    path("events/<int:event_id>/ack/", views.acknowledge_event, name="acknowledge"),
]
