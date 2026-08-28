"""Prometheus scrape endpoint."""
from django.http import HttpResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from apps.core import cassandra_session, metrics


def metrics_view(request):
    # Refresh the readiness gauge at scrape time.
    metrics.CASSANDRA_UP.set(1 if cassandra_session.is_ready() else 0)
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)