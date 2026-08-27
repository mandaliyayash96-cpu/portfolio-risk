"""
The one and only API response shape (architecture rule 3):

    {"success": true,  "data": <payload>, "error": null}
    {"success": false, "data": null,      "error": {"code", "message", "details"}}

Views normally return plain data and `common.renderers.EnvelopeJSONRenderer`
wraps it; these helpers exist for the cases where a view (or a non-DRF code
path) needs to build the envelope by hand.
"""

from typing import Any

from rest_framework import status as http_status
from rest_framework.response import Response


def envelope(data: Any = None, error: dict | None = None) -> dict:
    """Build the raw envelope dict. `error` is None on success."""
    return {"success": error is None, "data": data, "error": error}


def error_payload(code: str, message: str, details: Any = None) -> dict:
    """Build the `error` half of the envelope."""
    return {"code": code, "message": message, "details": details}


def success_response(data: Any = None, status: int = http_status.HTTP_200_OK) -> Response:
    """DRF Response already wrapped in the success envelope."""
    return Response(envelope(data=data), status=status)


def error_response(
    code: str,
    message: str,
    details: Any = None,
    status: int = http_status.HTTP_400_BAD_REQUEST,
) -> Response:
    """DRF Response already wrapped in the failure envelope."""
    return Response(envelope(error=error_payload(code, message, details)), status=status)


def is_enveloped(payload: Any) -> bool:
    """True when `payload` has already been wrapped, so we never double-wrap."""
    return isinstance(payload, dict) and set(payload.keys()) == {"success", "data", "error"}
