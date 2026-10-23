"""Request middleware for restricting internal endpoints."""

import base64
import logging
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.crypto import constant_time_compare

logger = logging.getLogger(__name__)


class MetricsAuthenticationMiddleware:
    """Require HTTP Basic authentication for Prometheus metrics."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info.rstrip("/") == "/metrics":
            if not settings.METRICS_USERNAME or not settings.METRICS_PASSWORD:
                logger.error("Metrics authentication credentials are not configured")
                return HttpResponse(status=503)
            expected = base64.b64encode(f"{settings.METRICS_USERNAME}:{settings.METRICS_PASSWORD}".encode()).decode(
                "ascii"
            )
            authorization = request.headers.get("Authorization", "").split()
            if (
                len(authorization) != 2
                or authorization[0].lower() != "basic"
                or not constant_time_compare(authorization[1], expected)
            ):
                return HttpResponse(status=401, headers={"WWW-Authenticate": 'Basic realm="metrics"'})

        return self.get_response(request)
