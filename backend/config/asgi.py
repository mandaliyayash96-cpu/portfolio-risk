"""
ASGI config.

Plain HTTP for now. Phase 6 replaces `application` with a Channels
ProtocolTypeRouter so the alert feed can be pushed over WebSocket.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()

# TODO Phase 6: Channels
# from channels.auth import AuthMiddlewareStack
# from channels.routing import ProtocolTypeRouter, URLRouter
# import alerts.routing
#
# application = ProtocolTypeRouter({
#     "http": get_asgi_application(),
#     "websocket": AuthMiddlewareStack(URLRouter(alerts.routing.websocket_urlpatterns)),
# })
