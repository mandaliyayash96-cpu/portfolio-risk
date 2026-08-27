"""
Health endpoint.

Deliberately the only view in Phase 1: it touches no ORM (architecture rule 1)
and exists to prove the envelope + exception handler wiring works.
"""

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """GET /api/health/ -> {"success": true, "data": {...}, "error": null}"""
    return Response(
        {
            "status": "ok",
            "service": "portfolio-risk-api",
            "phase": 1,
            "server_time": timezone.now().isoformat(),
        }
    )
