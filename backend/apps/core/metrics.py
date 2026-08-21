"""Prometheus metrics: request count, latency, cassandra readiness."""
from prometheus_client import Counter, Histogram, Gauge

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)

CASSANDRA_UP = Gauge(
    "cassandra_up",
    "1 if Cassandra readiness is up, else 0",
)