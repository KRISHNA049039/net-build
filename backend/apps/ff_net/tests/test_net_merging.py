"""Integration tests for the net-merging alert/approve/reject workflow.

Hit the real HTTP views (via Django's test client) against a real Cassandra
cluster -- see conftest.py for why (no SQL DB to fake it with here). Each
test uses its own reserved, disjoint net_id range so tests never interfere
with each other or with real data.
"""
import datetime as dt

import pytest
from django.test import Client
from django.urls import reverse

from apps.ff_net.submodules.ff_net_repository import (
    ff_net_building_repository,
    ff_net_merge_candidates_repository,
)

client = Client()


def candidates_url():
    return reverse("ff-net-merge-candidates")


def approve_url(net_a, net_b):
    return reverse("ff-net-merge-approve", kwargs={"pk": f"{net_a}__{net_b}"})


def reject_url(net_a, net_b):
    return reverse("ff-net-merge-reject", kwargs={"pk": f"{net_a}__{net_b}"})


def pending_pairs(payload):
    return {(c["net_a"], c["net_b"]) for c in payload["candidates"]}


# --------------------------------------------------------------- detection --

def test_candidates_detects_matching_pair(make_net, cleanup_candidates):
    a = make_net(999101, my_net_id=101)
    b = make_net(999102, my_net_id=102)  # same freq/time/location as `a`
    cleanup_candidates(999101, 999102)

    resp = client.get(candidates_url())
    assert resp.status_code == 200
    payload = resp.json()
    assert (999101, 999102) in pending_pairs(payload)

    row = next(c for c in payload["candidates"] if (c["net_a"], c["net_b"]) == (999101, 999102))
    assert row["status"] == "pending"
    assert row["my_net_id_a"] == 101 and row["my_net_id_b"] == 102
    assert row["freq_gap_mhz"] == 0.0
    assert row["lti_gap_sec"] == 0.0
    assert row["distance_m"] == 0.0


def test_candidates_skips_same_my_net_id_group(make_net, cleanup_candidates):
    make_net(999103, my_net_id=200)
    make_net(999104, my_net_id=200)  # same group already -- nothing to propose
    cleanup_candidates(999103, 999104)

    resp = client.get(candidates_url())
    assert resp.status_code == 200
    assert (999103, 999104) not in pending_pairs(resp.json())


def test_candidates_ignores_pair_outside_all_tolerances(make_net, cleanup_candidates):
    make_net(999105, my_net_id=105, frequency=1000.0)
    make_net(999106, my_net_id=106, frequency=5000.0)  # miles outside band tolerance
    cleanup_candidates(999105, 999106)

    resp = client.get(candidates_url())
    assert resp.status_code == 200
    assert (999105, 999106) not in pending_pairs(resp.json())


def test_candidates_is_idempotent(make_net, cleanup_candidates):
    make_net(999107, my_net_id=107)
    make_net(999108, my_net_id=108)
    cleanup_candidates(999107, 999108)

    first = client.get(candidates_url()).json()
    assert (999107, 999108) in pending_pairs(first)

    second = client.get(candidates_url()).json()
    # still pending, but NOT newly detected the second time around
    assert (999107, 999108) in pending_pairs(second)
    row = next(c for c in second["candidates"]
               if (c["net_a"], c["net_b"]) == (999107, 999108))
    assert row["detected_at"] is not None  # unchanged from the first call


# ----------------------------------------------------------------- decide --

def test_approve_merges_group_and_records_decision(make_net, cleanup_candidates):
    make_net(999109, my_net_id=109)
    make_net(999110, my_net_id=110)
    cleanup_candidates(999109, 999110)
    client.get(candidates_url())  # detect first

    resp = client.post(approve_url(999109, 999110))
    assert resp.status_code == 200
    body = resp.json()
    assert body["survivor_my_net_id"] == 109
    assert body["absorbed_my_net_id"] == 110
    assert body["nets_moved"] == 1

    row_a = ff_net_building_repository.get({"net_id": 999109})
    row_b = ff_net_building_repository.get({"net_id": 999110})
    assert row_a["my_net_id"] == row_b["my_net_id"] == 109

    candidate = ff_net_merge_candidates_repository.get({"net_a": 999109, "net_b": 999110})
    assert candidate["status"] == "approved"
    assert candidate["decided_at"] is not None

    # and it's gone from the pending list now
    assert (999109, 999110) not in pending_pairs(client.get(candidates_url()).json())


def test_approve_already_decided_returns_400(make_net, cleanup_candidates):
    make_net(999111, my_net_id=111)
    make_net(999112, my_net_id=112)
    cleanup_candidates(999111, 999112)
    client.get(candidates_url())

    first = client.post(approve_url(999111, 999112))
    assert first.status_code == 200

    second = client.post(approve_url(999111, 999112))
    assert second.status_code == 400
    assert "already" in second.json()["detail"]


def test_approve_nonexistent_pair_returns_404():
    resp = client.post(approve_url(1, 2))
    assert resp.status_code == 404


def test_reject_marks_rejected_and_is_never_reproposed(make_net, cleanup_candidates):
    make_net(999113, my_net_id=113)
    make_net(999114, my_net_id=114)
    cleanup_candidates(999113, 999114)
    client.get(candidates_url())

    resp = client.post(reject_url(999113, 999114))
    assert resp.status_code == 200
    assert resp.json() == {"net_a": 999113, "net_b": 999114, "decision": "reject"}

    candidate = ff_net_merge_candidates_repository.get({"net_a": 999113, "net_b": 999114})
    assert candidate["status"] == "rejected"

    # rejecting must NOT touch ff_net_building
    row_a = ff_net_building_repository.get({"net_id": 999113})
    row_b = ff_net_building_repository.get({"net_id": 999114})
    assert row_a["my_net_id"] == 113
    assert row_b["my_net_id"] == 114

    # and it must never come back as a pending alert again
    assert (999113, 999114) not in pending_pairs(client.get(candidates_url()).json())


# ------------------------------------------------------ incremental groups --

def test_incremental_transitive_merge(make_net, cleanup_candidates):
    """A-B approved, then B-C approved (using B's CURRENT my_net_id, which
    already moved to A's survivor by then) -> all three end up sharing one
    my_net_id, even though A and C were never directly decided against each
    other. Exercises the "whole group moves, not just the pair" behavior
    documented in MERGE_PIPELINE_REFERENCE.md section 5.3."""
    make_net(999115, my_net_id=115)
    make_net(999116, my_net_id=116)
    make_net(999117, my_net_id=117)  # all three share defaults -> all pairs match
    cleanup_candidates(999115, 999116)
    cleanup_candidates(999116, 999117)
    cleanup_candidates(999115, 999117)

    client.get(candidates_url())  # detects all three pairwise candidates

    first = client.post(approve_url(999115, 999116))
    assert first.status_code == 200
    assert first.json() == {
        "net_a": 999115, "net_b": 999116, "decision": "approve",
        "survivor_my_net_id": 115, "absorbed_my_net_id": 116, "nets_moved": 1,
    }

    second = client.post(approve_url(999116, 999117))
    assert second.status_code == 200
    body = second.json()
    assert body["survivor_my_net_id"] == 115    # net 999116's group, now 115
    assert body["absorbed_my_net_id"] == 117
    assert body["nets_moved"] == 1               # only 999117 was still at my_net_id=117

    rows = [ff_net_building_repository.get({"net_id": n}) for n in (999115, 999116, 999117)]
    my_net_ids = {r["my_net_id"] for r in rows}
    assert my_net_ids == {115}, f"expected all three merged under 115, got {rows}"
