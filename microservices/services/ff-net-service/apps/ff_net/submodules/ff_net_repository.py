"""ff_net_report submodule. Primary key: ((ff_id), lti).

Also exposes the repositories for the tables the net-building pipeline
(net_building_service.py) reads/writes -- ff_net_building (derived nets)
and ff_net_report_history (append-only audit trail) -- and the one the
net-merging pipeline (net_merging_service.py) reads/writes:
ff_net_merge_candidates (pending/approved/rejected merge decisions).
"""
from rest_framework import serializers, status, viewsets
from rest_framework.response import Response

from apps.ff_net.repository import (
    BaseRepository,
    coerce_query_values,
    decode_paging_state,
    encode_paging_state,
)
from apps.core import cassandra_session

KEYSPACE = "ans_transformed"


class FfNetReportRepository(BaseRepository):
    keyspace = KEYSPACE
    table = "ff_net_report"
    primary_key_columns = ("ff_id", "lti")


class FfNetBuildingRepository(BaseRepository):
    keyspace = KEYSPACE
    table = "ff_net_building"
    primary_key_columns = ("net_id",)


class FfNetReportHistoryRepository(BaseRepository):
    keyspace = KEYSPACE
    table = "ff_net_report_history"
    primary_key_columns = ("net_id", "processed_at", "ff_id")


class FfNetMergeCandidatesRepository(BaseRepository):
    keyspace = KEYSPACE
    table = "ff_net_merge_candidates"
    primary_key_columns = ("net_a", "net_b")


class FFNetReportSerializer(serializers.Serializer):
    # Primary Key / Clustering Key fields
    ff_id = serializers.IntegerField()
    lti = serializers.DateField(required=False, allow_null=True)

    # Data fields
    frequency = serializers.FloatField(required=False, allow_null=True)
    bandwidth = serializers.FloatField(required=False, allow_null=True)
    modulation = serializers.CharField(required=False, allow_null=True)
    signal_type = serializers.CharField(required=False, allow_null=True)
    net_level = serializers.CharField(required=False, allow_null=True)
    net_type = serializers.CharField(required=False, allow_null=True)
    branch = serializers.CharField(required=False, allow_null=True)
    nationality = serializers.CharField(required=False, allow_null=True)
    force = serializers.CharField(required=False, allow_null=True)
    language = serializers.CharField(required=False, allow_null=True)
    cipher = serializers.CharField(required=False, allow_null=True)

    # Geographic/Spatial fields
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    geo_distance = serializers.FloatField(required=False, allow_null=True)

    # Intercept fields
    nti = serializers.IntegerField(required=False, allow_null=True)
    fti = serializers.DateField(required=False, allow_null=True)

    # Signal characteristics
    azimuth = serializers.FloatField(required=False, allow_null=True)
    elevation = serializers.FloatField(required=False, allow_null=True)
    snr_db = serializers.FloatField(required=False, allow_null=True)
    rdfs = serializers.CharField(required=False, allow_null=True)


ff_net_report_repository = FfNetReportRepository()
ff_net_building_repository = FfNetBuildingRepository()
ff_net_report_history_repository = FfNetReportHistoryRepository()
ff_net_merge_candidates_repository = FfNetMergeCandidatesRepository()


class FFNetViewSet(viewsets.ViewSet):
    # Per-submodule wiring — the only lines that differ between submodules.
    serializer_class = FFNetReportSerializer
    repository = ff_net_report_repository
    default_page_size = 100
    max_page_size = 1000

    # -- small response helpers -----------------------------------------
    def _service_unavailable(self):
        return Response(
            {"detail": "Cassandra is not available"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    def _bad_request(self, detail):
        return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)

    # -- guard helpers (return (value, error_response)) -----------------
    def _require_cassandra(self):
        """Return a 503 response if Cassandra isn't ready, else None."""
        if cassandra_session.is_ready():
            return None
        return self._service_unavailable()

    def _parse_primary_key(self, pk):
        parts = (pk or "").split("__")
        key_columns = self.repository.primary_key_columns
        if len(parts) != len(key_columns):
            return None, self._bad_request(
                f"primary key must be {len(key_columns)} parts joined by '__'"
            )
        raw_key = dict(zip(key_columns, parts))
        return coerce_query_values(self.serializer_class, raw_key), None

    def _read_page_size(self, query_params):
        raw_value = query_params.pop("page_size", None)
        if raw_value is None:
            return self.default_page_size, None
        try:
            page_size = int(raw_value)
        except (TypeError, ValueError):
            return None, self._bad_request("page_size must be an integer")
        if page_size < 1 or page_size > self.max_page_size:
            return None, self._bad_request(
                f"page_size must be between 1 and {self.max_page_size}"
            )
        return page_size, None

    def _decode_cursor(self, query_params):
        raw_cursor = query_params.pop("cursor", None)
        try:
            return decode_paging_state(raw_cursor), None
        except (ValueError, TypeError):
            return None, self._bad_request("cursor is invalid")

    # -- endpoints ------------------------------------------------------
    def list(self, request):
        unavailable = self._require_cassandra()
        if unavailable:
            return unavailable

        # dict() is mutable; page_size and cursor are popped, the rest are filters.
        query_params = request.query_params.dict()

        page_size, error = self._read_page_size(query_params)
        if error:
            return error

        paging_state, error = self._decode_cursor(query_params)
        if error:
            return error

        filters = coerce_query_values(self.serializer_class, query_params)
        rows, next_state = self.repository.find_page(
            filters, page_size=page_size, paging_state=paging_state
        )
        return Response({
            "results": rows,
            "next_cursor": encode_paging_state(next_state),
        })

    def create(self, request):
        unavailable = self._require_cassandra()
        if unavailable:
            return unavailable
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.repository.insert(serializer.validated_data)
        return Response(serializer.validated_data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        unavailable = self._require_cassandra()
        if unavailable:
            return unavailable
        key_values, error = self._parse_primary_key(pk)
        if error:
            return error
        row = self.repository.get(key_values)
        if row is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(row)

    def update(self, request, pk=None):
        unavailable = self._require_cassandra()
        if unavailable:
            return unavailable
        key_values, error = self._parse_primary_key(pk)
        if error:
            return error
        serializer = self.serializer_class(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updatable_fields = {
            column: value
            for column, value in serializer.validated_data.items()
            if column not in self.repository.primary_key_columns
        }
        if not updatable_fields:
            return Response({"updated": False, "detail": "no updatable fields"})
        self.repository.update(key_values, updatable_fields)
        return Response({"updated": True})

    def destroy(self, request, pk=None):
        unavailable = self._require_cassandra()
        if unavailable:
            return unavailable
        key_values, error = self._parse_primary_key(pk)
        if error:
            return error
        self.repository.delete(key_values)
        return Response(status=status.HTTP_204_NO_CONTENT)
