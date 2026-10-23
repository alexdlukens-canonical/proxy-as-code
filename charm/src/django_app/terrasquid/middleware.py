"""Request middleware for restricting internal endpoints."""

import ipaddress
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseForbidden


class LocalhostMetricsMiddleware:
    """Restrict the Prometheus metrics endpoint to loopback clients."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info.rstrip("/") == "/metrics":
            try:
                is_loopback = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                return HttpResponseForbidden()

        return self.get_response(request)
