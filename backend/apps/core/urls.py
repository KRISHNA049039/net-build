from django.urls import path

from .health import health
from .metrics_view import metrics_view

urlpatterns = [
    path("health/", health, name="health"),
    path("metrics/", metrics_view, name="metrics"),
]