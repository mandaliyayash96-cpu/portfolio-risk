"""
WebSocket URL patterns, mounted by config/asgi.py.

The HTTP equivalent of alerts/urls.py, kept in its own module for the same
reason Channels documents it that way: config/asgi.py must be importable to
build the router, and pulling consumers in through a urls.py that DRF views also
live in would drag the whole REST stack into the ASGI import path.

`path` rather than `re_path`: <int:portfolio_id> converts the id for us, so the
consumer receives an int and a non-numeric segment never matches at all.

The `ws/` prefix is a convention, not a requirement - it exists so a reverse
proxy in Phase 8 can route WebSocket traffic on a path prefix without inspecting
the Upgrade header.
"""

from django.urls import path

from alerts import consumers

websocket_urlpatterns = [
    path("ws/alerts/<int:portfolio_id>/", consumers.AlertConsumer.as_asgi()),
]
