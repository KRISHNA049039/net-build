"""Log one line per request + feed Prometheus metrics."""
import logging
import time
import uuid

from apps.core import metrics

log = logging.getLogger("requests")


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "-")


def _route(request):
    # Use the matched URL pattern, not the raw path, so IDs don't explode label cardinality.
    match = getattr(request, "resolver_match", None)
    if match and match.route:
        return "/" + match.route
    return request.path


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex[:12]
        request.request_id = request_id

        start = time.perf_counter()
        response = self.get_response(request)
        elapsed = time.perf_counter() - start

        response["X-Request-ID"] = request_id

        route = _route(request)
        metrics.REQUEST_COUNT.labels(
            request.method, route, str(response.status_code)
        ).inc()
        metrics.REQUEST_LATENCY.labels(request.method, route).observe(elapsed)

        log.info(
            "%s %s -> %s | %.1fms | ip=%s | rid=%s",
            request.method,
            request.get_full_path(),
            response.status_code,
            elapsed * 1000,
            _client_ip(request),
            request_id,
        )
        return response