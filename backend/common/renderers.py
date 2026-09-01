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


class PDFRenderer(JSONRenderer):
    """
    A renderer that exists so `Accept: application/pdf` is negotiable.

    The PDF endpoint returns a plain Django HttpResponse, which bypasses the
    renderer machinery entirely - so this class never actually renders a
    document. What it does is stop DRF answering 406 Not Acceptable during
    content negotiation, which happens BEFORE the view runs and would otherwise
    reject a client that asked for the exact media type the URL advertises.

    It subclasses the JSON renderer rather than BaseRenderer on purpose: if it
    is ever reached, it has been handed an error envelope by the exception
    handler, and JSON is the only useful thing to do with that. An empty body
    or a zero-byte "PDF" would be a worse answer than a readable error.
    """

    media_type = "application/pdf"
    format = "pdf"
    charset = None
