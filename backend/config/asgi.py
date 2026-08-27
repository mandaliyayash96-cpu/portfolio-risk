"""
ASGI entry point: one callable that speaks both HTTP and WebSocket.

    daphne config.asgi:application

`ProtocolTypeRouter` switches on the connection's `scope["type"]`. HTTP scopes
go to the ordinary Django application - every existing view, the admin and the
whole DRF stack are untouched by this file. WebSocket scopes go to the alerts
URLRouter instead, which is the only reason this module is more than two lines.

ORDER MATTERS HERE, twice:

1. `get_asgi_application()` is called BEFORE `alerts.routing` is imported. That
   call is what runs django.setup() and populates the app registry; importing
   the routing module first would pull in consumers -> models -> an app registry
   that is not ready yet, and Django raises AppRegistryNotReady. Assigning it to
   a name and importing afterwards is the documented Channels ordering, not a
   style preference.

2. The websocket branch is wrapped OUTSIDE-IN: origin check, then session/auth,
   then routing. Each layer only sees connections the layer above it allowed.

`AllowedHostsOriginValidator` is the WebSocket equivalent of ALLOWED_HOSTS.
Browsers do not apply the same-origin policy to WebSockets and send no CORS
preflight, so without it any page on the internet could open a socket to this
server in a visitor's browser and read their alert feed. It validates the
`Origin` header against ALLOWED_HOSTS, which in this dev setup is
localhost,127.0.0.1 - the Vite dev server on :5173 passes (host matches, port is
not part of the check).

One consequence worth knowing before you reach for a CLI client: a connection
with NO Origin header at all is refused too, and websocat/wscat send none by
default. Testing from a terminal therefore needs the header supplied by hand:

    websocat -H 'Origin: http://127.0.0.1:8000' ws://127.0.0.1:8000/ws/alerts/1/

The browser always sends one, so the dashboard is unaffected.

`AuthMiddlewareStack` populates scope["user"] from the session cookie. Nothing
consumes it yet - the consumer is AllowAny for now, matching the REST side - but
it is the seam Phase 4's auth work plugs into, and adding it later would mean
re-deriving this ordering.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Populates the app registry. Must happen before any project module is imported.
django_asgi_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from alerts.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_application,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)
