"""Readiness endpoint: reports each dependency's up/down state."""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import cassandra_session


@api_view(["GET"])
def health(request):
    deps = {
        "cassandra": cassandra_session.is_ready(),
    }
    all_up = all(deps.values())
    status_code = 200 if all_up else 503
    return Response(
        {"status": "ok" if all_up else "degraded", "dependencies": deps},
        status=status_code,
    )