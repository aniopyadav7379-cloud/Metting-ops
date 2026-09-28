from __future__ import annotations

import time

try:
    from prometheus_client import Counter, Histogram
except ImportError:  # pragma: no cover - minimal local test environments
    class _NoopMetric:
        def __init__(self, *args, **kwargs): pass
        def labels(self, **kwargs): return self
        def inc(self): pass
        def observe(self, value): pass

    Counter = Histogram = _NoopMetric
from starlette.middleware.base import BaseHTTPMiddleware

HTTP_REQUESTS = Counter(
    "meeting_ops_http_requests_total",
    "HTTP requests by route template and status.",
    ("method", "handler", "status"),
)
HTTP_DURATION = Histogram(
    "meeting_ops_http_request_duration_seconds",
    "HTTP request latency by route template.",
    ("method", "handler"),
)


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            handler = getattr(route, "path", None) or "other"
            method = request.method
            HTTP_REQUESTS.labels(method=method, handler=handler, status=str(status)).inc()
            HTTP_DURATION.labels(method=method, handler=handler).observe(time.perf_counter() - started)
