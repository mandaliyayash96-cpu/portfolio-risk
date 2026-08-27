"""
Central DRF exception handler (architecture rule 3) plus the service-layer
error hierarchy.

`DomainError` and friends are plain Python exceptions on purpose: services.py
raises them, and only this module knows how to turn them into HTTP. That keeps
the domain layer framework-agnostic and unit-testable.
"""

import logging
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from common.response import envelope, error_payload

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain errors — raised by services.py / selectors.py, never by views.
# ---------------------------------------------------------------------------
class DomainError(Exception):
    """Base class for expected, business-rule failures."""

    code = "domain_error"
    status_code = http_status.HTTP_400_BAD_REQUEST
    message = "The request could not be completed."

    def __init__(self, message: str | None = None, details: Any = None, code: str | None = None):
        self.message = message or self.message
        self.details = details
        if code:
            self.code = code
        super().__init__(self.message)


class InvalidInputError(DomainError):
    code = "invalid_input"
    status_code = http_status.HTTP_400_BAD_REQUEST
    message = "Invalid input."


class NotFoundError(DomainError):
    code = "not_found"
    status_code = http_status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ConflictError(DomainError):
    code = "conflict"
    status_code = http_status.HTTP_409_CONFLICT
    message = "The request conflicts with the current state."


class ProviderError(DomainError):
    """
    The upstream market-data feed failed: unreachable, rate limited, or it
    returned something unusable. Providers never let a raw yfinance/network
    exception escape - everything is wrapped in this or a subclass.
    """

    code = "provider_error"
    status_code = http_status.HTTP_502_BAD_GATEWAY
    message = "Market data provider is unavailable."


class UnknownTickerError(ProviderError):
    """The symbol is not recognised by the provider (typo, or delisted)."""

    code = "unknown_ticker"
    status_code = http_status.HTTP_404_NOT_FOUND
    message = "Unknown ticker."


class EmptyHistoryError(ProviderError):
    """The symbol resolved but the provider returned no price rows."""

    code = "empty_history"
    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "No price history available for this ticker."


# TODO Phase 3: InsufficientHistoryError(DomainError) -> 422, raised when a
#               portfolio has too few aligned return observations to compute risk.


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def _describe(exc: drf_exceptions.APIException, response: Response) -> dict:
    """Map a DRF exception + its rendered body onto the envelope's error half."""
    code = getattr(exc, "default_code", None) or "error"
    detail = response.data

    if isinstance(exc, drf_exceptions.ValidationError):
        return error_payload("validation_error", "Validation failed.", detail)
    if isinstance(detail, dict) and "detail" in detail:
        return error_payload(code, str(detail["detail"]))
    if isinstance(detail, list):
        return error_payload(code, "; ".join(str(item) for item in detail), detail)
    return error_payload(code, str(detail))


def custom_exception_handler(exc, context):
    """
    Every error leaving the API passes through here, so clients only ever see
    {"success": false, "data": null, "error": {...}}.

    Unhandled exceptions are logged with their traceback and returned as a 500
    envelope; the exception repr is echoed in `details` only when DEBUG is on.
    """
    if isinstance(exc, DomainError):
        return Response(
            envelope(error=error_payload(exc.code, exc.message, exc.details)),
            status=exc.status_code,
        )

    # Normalise the Django-level exceptions DRF does not translate on its own.
    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exceptions.PermissionDenied()
    elif isinstance(exc, DjangoValidationError):
        exc = drf_exceptions.ValidationError(detail=getattr(exc, "message_dict", exc.messages))

    response = drf_exception_handler(exc, context)

    if response is None:
        view = context.get("view") if context else None
        logger.exception("Unhandled exception in %s", view.__class__.__name__ if view else "API")
        details = f"{exc.__class__.__name__}: {exc}" if settings.DEBUG else None
        return Response(
            envelope(error=error_payload("server_error", "Internal server error.", details)),
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response.data = envelope(error=_describe(exc, response))
    return response
