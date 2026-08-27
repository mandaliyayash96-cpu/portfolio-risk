"""
The WebSocket end of the alert feed.

One consumer instance per open browser tab. Its whole job is to join the group
for the portfolio named in the URL, hand over what is already open, and then
relay whatever `alerts.services.scan_and_emit` pushes into that group.

SNAPSHOT THEN STREAM
--------------------
A socket that only streamed would show an empty feed to anyone who connected
after the breach - which, for an alert that fired overnight, is everyone. So
connect sends the currently-open events first, and every push after that is an
increment on top of it. The client therefore never needs a separate REST call
to populate the panel, and a reconnect after a dropped connection re-syncs by
construction: the fresh snapshot is the truth, whatever the tab missed while it
was away.

Both messages carry a `type` field ("snapshot" / "alert") because they arrive on
the same wire and the client dispatches on it.

WHY EVERY ORM CALL IS WRAPPED
-----------------------------
This is async code. Django's ORM is synchronous, and touching it directly from a
coroutine either raises SynchronousOnlyOperation or, worse, blocks the event
loop that is serving every other socket in the process. `database_sync_to_async`
moves the query to a threadpool. The serialisers in alerts.selectors dereference
`event.rule`, so that dereference has to happen INSIDE the wrapped call - which
is why the helpers below return finished dicts rather than model instances.
"""

from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from alerts.services import group_name, open_alerts
from portfolio.models import Portfolio

logger = logging.getLogger(__name__)

#: Close codes. 4000-4999 is the range reserved for application use, so these
#: reach the browser's `event.code` intact - but ONLY on a socket that was
#: accepted first. A close before accept() is an HTTP 403 at handshake time and
#: the browser reports it as 1006 with no code, which is why connect() accepts
#: before refusing. The panel reads these to decide whether reconnecting could
#: ever help.
CLOSE_BAD_PORTFOLIO_ID = 4400
CLOSE_NO_SUCH_PORTFOLIO = 4404


class AlertConsumer(AsyncWebsocketConsumer):
    """
    ws://<host>/ws/alerts/<portfolio_id>/

    AllowAny, matching the REST side for this phase. `AuthMiddlewareStack` in
    config/asgi.py has already populated scope["user"], so the Phase 4 auth work
    is a permission check here plus scoping the portfolio lookup - not a
    re-plumbing.

    Cross-origin connections are rejected before this class is reached, by the
    AllowedHostsOriginValidator wrapping the router.
    """

    async def connect(self):
        raw_id = self.scope["url_route"]["kwargs"]["portfolio_id"]
        try:
            portfolio_id = int(raw_id)
        except (TypeError, ValueError):
            # The URL pattern is <int:...>, so this is unreachable through
            # routing; it stays because the class is not the router's to trust.
            await self.accept()
            await self.close(code=CLOSE_BAD_PORTFOLIO_ID)
            return
        self.portfolio_id = portfolio_id

        if not await self._portfolio_exists(self.portfolio_id):
            # ACCEPT, then close with a 4xxx code - deliberately not a bare
            # rejection. Closing before accept() makes daphne fail the handshake
            # with HTTP 403, and a browser reports a failed handshake as close
            # code 1006 with no detail: indistinguishable from "the server is
            # down". The panel reconnects with backoff, so that ambiguity would
            # have it retrying a portfolio id that will never exist, forever.
            # Accepting first costs one round trip and lets the client read
            # CLOSE_NO_SUCH_PORTFOLIO and give up with a real message.
            logger.info("Rejected alert socket for unknown portfolio %s.", self.portfolio_id)
            await self.accept()
            await self.close(code=CLOSE_NO_SUCH_PORTFOLIO)
            return

        self.group = group_name(self.portfolio_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

        events = await self._open_alerts(self.portfolio_id)
        await self._send_json(
            {
                "type": "snapshot",
                "portfolio_id": self.portfolio_id,
                "events": events,
            }
        )
        logger.info(
            "Alert socket open for portfolio %s (%s event(s) in snapshot).",
            self.portfolio_id,
            len(events),
        )

    async def disconnect(self, code):
        # getattr, not self.group: connect() can bail before the attribute
        # exists, and disconnect still runs for a rejected handshake.
        group = getattr(self, "group", None)
        if group and self.channel_layer is not None:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """
        The feed is one-directional; the only inbound message is a keepalive.

        Answering ping with pong lets the client prove the connection is alive
        without waiting for an alert that may be hours away. Anything else is
        ignored rather than errored - a stray message from a client is not a
        reason to drop a socket that is working.
        """
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except (TypeError, ValueError):
            return
        if isinstance(message, dict) and message.get("type") == "ping":
            await self._send_json({"type": "pong"})

    # -- group handler -------------------------------------------------------
    async def alert_event(self, message: dict):
        """
        Handler for the "alert.event" messages `scan_and_emit` group_sends.

        Channels derives this method's name from the message's `type` by
        replacing dots with underscores, so renaming either half without the
        other silently stops delivery. That is why the type string is a
        constant in alerts.services (EVENT_MESSAGE_TYPE) rather than a literal
        typed out at both ends.
        """
        await self._send_json({"type": "alert", "event": message["event"]})

    # -- helpers -------------------------------------------------------------
    async def _send_json(self, payload: dict):
        """AsyncWebsocketConsumer sends text; this is the JSON on top of it."""
        await self.send(text_data=json.dumps(payload))

    @staticmethod
    @database_sync_to_async
    def _portfolio_exists(portfolio_id: int) -> bool:
        return Portfolio.objects.filter(pk=portfolio_id).exists()

    @staticmethod
    @database_sync_to_async
    def _open_alerts(portfolio_id: int) -> list[dict]:
        """Serialised inside the threadpool - see the module docstring."""
        return open_alerts(portfolio_id)
