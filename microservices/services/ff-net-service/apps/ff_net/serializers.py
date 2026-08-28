"""Response/request serializers for OpenAPI schema generation ONLY.

views.py's endpoints return plain dicts via Response(...) -- these
serializers are never used to actually validate or render a response (see
each view's @extend_schema(...) in views.py for how they're wired in).
They exist purely so drf-spectacular can generate an accurate contract for
endpoints built as plain APIViews rather than ModelViewSets. Keep these in
sync with the real dict shapes in net_building_service.py /
net_merging_service.py by hand -- nothing enforces that automatically.
"""
from rest_framework import serializers


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


# ---------------------------------------------------------------- build ---

class NetRecordSerializer(serializers.Serializer):
    """One row of ff_net_building, as returned in a build response's
    `nets` list. Domain fields beyond these core ones (branch, nationality,
    latitude, longitude, azimuth, elevation, snr_db, cipher, force,
    language, net_level, net_type, geo_distance, nti, modulation,
    signal_type, ...) also appear but aren't individually declared here --
    see PIPELINE_REFERENCE.md for the full field list."""
    my_net_id = serializers.IntegerField()
    net_id = serializers.IntegerField()
    ff_id = serializers.IntegerField(required=False, allow_null=True)
    frequency = serializers.FloatField(required=False, allow_null=True)
    bandwidth = serializers.FloatField(required=False, allow_null=True)
    rdfs = serializers.CharField(required=False, allow_null=True)
    fti = serializers.CharField(required=False, allow_null=True)
    lti = serializers.CharField(required=False, allow_null=True)


class BuildProcessedSerializer(serializers.Serializer):
    new = serializers.IntegerField()
    collapsed = serializers.IntegerField()
    skipped = serializers.IntegerField()


class NetBuildResponseSerializer(serializers.Serializer):
    generated_at = serializers.CharField()
    processed = BuildProcessedSerializer()
    nets = NetRecordSerializer(many=True)


# --------------------------------------------------------------- merge ----

class MergeCandidateSerializer(serializers.Serializer):
    """One row of ff_net_merge_candidates -- a pending, approved, or
    rejected merge decision, with the freq/time/location numbers behind it
    frozen at detection time (see MERGE_PIPELINE_REFERENCE.md section 5.4)."""
    net_a = serializers.IntegerField()
    net_b = serializers.IntegerField()
    status = serializers.ChoiceField(choices=["pending", "approved", "rejected"])
    my_net_id_a = serializers.IntegerField()
    my_net_id_b = serializers.IntegerField()
    rdfs_a = serializers.CharField(required=False, allow_null=True)
    rdfs_b = serializers.CharField(required=False, allow_null=True)
    frequency_a = serializers.FloatField()
    frequency_b = serializers.FloatField()
    lti_a = serializers.CharField(required=False, allow_null=True)
    lti_b = serializers.CharField(required=False, allow_null=True)
    freq_gap_mhz = serializers.FloatField()
    freq_tol_mhz = serializers.FloatField()
    lti_gap_sec = serializers.FloatField(required=False, allow_null=True)
    lti_tol_sec = serializers.FloatField(required=False, allow_null=True)
    distance_m = serializers.FloatField(required=False, allow_null=True)
    loc_tol_m = serializers.FloatField(required=False, allow_null=True)
    detected_at = serializers.CharField()
    decided_at = serializers.CharField(required=False, allow_null=True)


class MergeCandidatesResponseSerializer(serializers.Serializer):
    generated_at = serializers.CharField()
    newly_detected = serializers.IntegerField(
        help_text="How many brand-new candidate pairs this call itself just inserted."
    )
    pending_count = serializers.IntegerField()
    candidates = MergeCandidateSerializer(many=True)


class MergeDecisionResponseSerializer(serializers.Serializer):
    net_a = serializers.IntegerField()
    net_b = serializers.IntegerField()
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    survivor_my_net_id = serializers.IntegerField(
        required=False, help_text="approve only: the my_net_id the group now shares."
    )
    absorbed_my_net_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="approve only: the my_net_id that was folded in (null if the "
                   "two nets were already in the same group).",
    )
    nets_moved = serializers.IntegerField(
        required=False,
        help_text="approve only: how many ff_net_building rows had my_net_id reassigned.",
    )
