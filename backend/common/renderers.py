"""
Renderer that wraps every successful response in the standard envelope, so
views stay thin and carry zero envelope boilerplate (architecture rule 3).

Failures are already enveloped by `common.exceptions.custom_exception_handler`;
`is_enveloped()` keeps this from double-wrapping them.
"""

from rest_framework.renderers import JSONRenderer

from common.response import envelope, error_payload, is_enveloped


class EnvelopeJSONRenderer(JSONRenderer):
    """JSON renderer emitting {"success": bool, "data": ..., "error": ...}."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        response = renderer_context.get("response")

        if is_enveloped(data):
            payload = data
        elif response is not None and response.status_code >= 400:
            # A view returned an error Response directly, bypassing the handler.
            payload = envelope(error=error_payload("error", "Request failed.", data))
        else:
            payload = envelope(data=data)

        return super().render(payload, accepted_media_type, renderer_context)
