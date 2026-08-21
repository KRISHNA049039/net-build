from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import cassandra_session
from apps.ff_net.serializers import (
    ErrorResponseSerializer,
    MergeCandidatesResponseSerializer,
    MergeDecisionResponseSerializer,
    NetBuildResponseSerializer,
)
from apps.ff_net.submodules import net_building_service, net_merging_service

_PAIR_PARAM = OpenApiParameter(
    # drf-spectacular auto-detects this path segment from the URL's <str:pk>
    # and names it "id" in the generated schema (its default pk-aliasing
    # convention) -- naming this parameter "id" too makes it MERGE with
    # (override the description of) that auto-detected entry instead of
    # producing a confusing duplicate "pk" parameter alongside it.
    name="id",
    location=OpenApiParameter.PATH,
    type=str,
    description="The two net_ids as '<net_a>__<net_b>', smaller id first "
                "(matches the values GET .../merge/candidates/ returned).",
)


class NetBuildView(APIView):
    """GET-triggered: folds ff_net_report into the net hierarchy, syncs
    ff_net_building + ff_net_report_history, and returns the hierarchy."""

    @extend_schema(
        tags=["ff_net: build"],
        summary="Build/update the net hierarchy from raw intercept reports",
        description="Re-reads every ff_net_report row, folds each unseen one "
                    "into ff_net_building (same RDFS + matching FF_ID or "
                    "overlapping frequency band -> updates an existing net; "
                    "otherwise spawns a new my_net_id/net_id pair), and "
                    "returns the full net hierarchy. Idempotent -- re-running "
                    "with no new source rows is a no-op other than the "
                    "returned JSON.",
        responses={200: NetBuildResponseSerializer, 503: ErrorResponseSerializer},
    )
    def get(self, request):
        if not cassandra_session.is_ready():
            return Response(
                {"detail": "Cassandra is not available"}, status=503
            )
        return Response(net_building_service.run_pipeline())


class NetMergeCandidatesView(APIView):
    """GET-triggered: detects new merge candidates among the current nets
    (frequency band overlap + time + location correlation, across DIFFERENT
    my_net_id groups) and returns every still-pending one. This is the
    commander-facing alert queue -- nothing is ever merged automatically;
    see NetMergeApproveView / NetMergeRejectView."""

    @extend_schema(
        tags=["ff_net: merge"],
        summary="List pending net-merge candidates (the commander alert queue)",
        description="Detects any NEW candidate pairs among the current "
                    "ff_net_building nets (frequency band overlap + time gap "
                    "+ location proximity, across different my_net_id groups) "
                    "and returns every still-pending one, approved/rejected "
                    "pairs included implicitly by their absence. Safe to poll "
                    "repeatedly -- a pair already recorded, decided or not, "
                    "is never re-proposed. Nothing here ever changes "
                    "ff_net_building; only an explicit approve does.",
        responses={200: MergeCandidatesResponseSerializer, 503: ErrorResponseSerializer},
    )
    def get(self, request):
        if not cassandra_session.is_ready():
            return Response(
                {"detail": "Cassandra is not available"}, status=503
            )
        return Response(net_merging_service.list_pending_candidates())


class _NetMergeDecisionBase(APIView):
    """Shared logic for the approve/reject endpoints below. Split into two
    thin subclasses (rather than one view branching on a URL kwarg) purely
    so drf-spectacular documents them as two distinct, accurately-described
    OpenAPI operations."""

    decision = None  # set by subclass: "approve" | "reject"

    def _parse_pair(self, pk):
        parts = (pk or "").split("__")
        if len(parts) != 2:
            return None, None, Response(
                {"detail": "pair must be '<net_a>__<net_b>'"}, status=400
            )
        try:
            net_a, net_b = int(parts[0]), int(parts[1])
        except ValueError:
            return None, None, Response(
                {"detail": "net_a/net_b must be integers"}, status=400
            )
        return net_a, net_b, None

    def post(self, request, pk=None):
        if not cassandra_session.is_ready():
            return Response(
                {"detail": "Cassandra is not available"}, status=503
            )
        net_a, net_b, error = self._parse_pair(pk)
        if error:
            return error
        try:
            result = net_merging_service.apply_decision(net_a, net_b, self.decision)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        except LookupError as e:
            return Response({"detail": str(e)}, status=404)
        return Response(result)


class NetMergeApproveView(_NetMergeDecisionBase):
    """POST-triggered: a commander approves one pending merge candidate.
    Folds the higher my_net_id GROUP into the lower one in ff_net_building
    (every net_id currently sharing that my_net_id moves, not just the two
    nets in the pair -- see MERGE_PIPELINE_REFERENCE.md section 5.3)."""

    decision = "approve"

    @extend_schema(
        tags=["ff_net: merge"],
        summary="Approve a pending merge candidate",
        description="Folds the higher my_net_id GROUP into the lower one in "
                    "ff_net_building. No request body -- the pair is "
                    "identified entirely by the URL.",
        parameters=[_PAIR_PARAM],
        request=None,
        responses={
            200: MergeDecisionResponseSerializer,
            400: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request, pk=None):
        return super().post(request, pk)


class NetMergeRejectView(_NetMergeDecisionBase):
    """POST-triggered: a commander rejects one pending merge candidate.
    Never touches ff_net_building -- just records the decision so the pair
    is never proposed again."""

    decision = "reject"

    @extend_schema(
        tags=["ff_net: merge"],
        summary="Reject a pending merge candidate",
        description="Records the decision so this pair is never proposed "
                    "again. Does not change ff_net_building. No request body "
                    "-- the pair is identified entirely by the URL.",
        parameters=[_PAIR_PARAM],
        request=None,
        responses={
            200: MergeDecisionResponseSerializer,
            400: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
            503: ErrorResponseSerializer,
        },
    )
    def post(self, request, pk=None):
        return super().post(request, pk)
