"""Cassandra-backed orchestration for net-to-net merge candidate detection
and commander approval.

Unlike net_building_service.run_pipeline() (fully automatic -- every call
folds new reports into nets with no human step), merging never changes
ff_net_building on its own:

  * GET  .../merge/candidates/         -> list_pending_candidates()
    Detects any NEW candidate pairs among the current nets and returns
    every still-pending one. This is the commander-facing "alert queue" --
    a frontend polls this to know what needs a merge/keep-separate call.
    Safe to call repeatedly: a pair already recorded (pending, approved, OR
    rejected) is never re-proposed, mirroring the idempotency pattern
    net_building_service uses via ff_net_report_history, except here the
    ledger is ff_net_merge_candidates itself.

  * POST .../merge/candidates/<net_a>__<net_b>/approve/   -> apply_decision(..., "approve")
    POST .../merge/candidates/<net_a>__<net_b>/reject/    -> apply_decision(..., "reject")
    The only two ways ff_net_building.my_net_id ever changes after a net is
    first built. Approving folds the higher my_net_id GROUP into the lower
    one (every net_id currently sharing that my_net_id moves, not just the
    two nets in the pair) -- this is what lets three-or-more-net merges
    happen incrementally: approve A-B, then later approve B-C, and A/B/C
    end up in one group without ever re-deciding A-B.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from apps.ff_net.submodules import net_merging as nm
from apps.ff_net.submodules.net_building_service import _cassandra_row_to_df_row
from apps.ff_net.submodules.ff_net_repository import (
    ff_net_building_repository,
    ff_net_merge_candidates_repository,
)

VALID_DECISIONS = ("approve", "reject")


# ---------------------------------------------------------------- loading --

def _fetch_nets() -> pd.DataFrame:
    """Every row currently in ff_net_building, in net_merging.py's column
    convention (reuses net_building_service's Cassandra<->DataFrame mapping,
    since it's the same source data net_building_service itself writes)."""
    rows = ff_net_building_repository.find_all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([_cassandra_row_to_df_row(r) for r in rows])


def _existing_pairs() -> set:
    rows = ff_net_merge_candidates_repository.find_all()
    return {(r["net_a"], r["net_b"]) for r in rows}


# ---------------------------------------------------------------- detect ---

def _snapshot_row(net_a: int, net_b: int, reasons: dict, nets_by_id: dict) -> dict:
    a, b = nets_by_id[net_a], nets_by_id[net_b]
    lti_a, lti_b = a.get("LTI"), b.get("LTI")
    now = dt.datetime.utcnow()
    return {
        "net_a": net_a, "net_b": net_b, "status": "pending",
        "my_net_id_a": int(a["my_net_id"]), "my_net_id_b": int(b["my_net_id"]),
        "rdfs_a": a.get("RDFS"), "rdfs_b": b.get("RDFS"),
        "frequency_a": float(a["Frequency_MHz"]), "frequency_b": float(b["Frequency_MHz"]),
        "lti_a": lti_a.date() if pd.notna(lti_a) else None,
        "lti_b": lti_b.date() if pd.notna(lti_b) else None,
        "freq_gap_mhz": reasons.get("freq_gap_mhz"),
        "freq_tol_mhz": reasons.get("freq_tol_mhz"),
        "lti_gap_sec": reasons.get("lti_gap_sec"),
        "lti_tol_sec": reasons.get("lti_tol_sec"),
        "distance_m": reasons.get("distance_m"),
        "loc_tol_m": reasons.get("loc_tol_m"),
        "detected_at": now,
        "decided_at": None,
    }


def detect_new_candidates(cfg: Optional[nm.MergeConfig] = None) -> int:
    """Run the matcher over current nets, insert any newly-found pair as a
    'pending' row. Returns how many NEW pairs were inserted. Pairs already
    recorded (pending, approved, or rejected) are never re-proposed."""
    nets = _fetch_nets()
    if nets.empty:
        return 0

    seen_pairs = _existing_pairs()
    nets_by_id = {int(r["net_id"]): r for r in nets.to_dict("records")}

    candidates = nm.propose_pairs(nets, cfg)
    inserted = 0
    for net_a, net_b, reasons in candidates:
        if (net_a, net_b) in seen_pairs:
            continue
        ff_net_merge_candidates_repository.insert(
            _snapshot_row(net_a, net_b, reasons, nets_by_id)
        )
        inserted += 1
    return inserted


def list_pending_candidates(cfg: Optional[nm.MergeConfig] = None) -> dict:
    """Detect anything new, then return every still-pending candidate --
    the commander-facing alert list."""
    newly_detected = detect_new_candidates(cfg)
    rows = ff_net_merge_candidates_repository.find_all()
    # find_all() -> apps.core.execution.rows_to_dicts already coerces Cassandra
    # date/timestamp columns (lti_a/lti_b/detected_at/decided_at) to ISO
    # strings, so these rows are already JSON-safe as-is.
    pending = [r for r in rows if r.get("status") == "pending"]

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "newly_detected": newly_detected,
        "pending_count": len(pending),
        "candidates": pending,
    }


# ----------------------------------------------------------------- decide --

def apply_decision(net_a: int, net_b: int, decision: str) -> dict:
    """Approve or reject one pending candidate.

    Raises ValueError for a bad `decision` or a pair that's already been
    decided; LookupError if the pair (or, on approve, either underlying
    net) no longer exists.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_DECISIONS}")

    net_a, net_b = sorted((int(net_a), int(net_b)))
    key = {"net_a": net_a, "net_b": net_b}
    candidate = ff_net_merge_candidates_repository.get(key)
    if candidate is None:
        raise LookupError(f"no merge candidate recorded for ({net_a}, {net_b})")
    if candidate.get("status") != "pending":
        raise ValueError(f"({net_a}, {net_b}) was already {candidate.get('status')}")

    now = dt.datetime.utcnow()
    result = {"net_a": net_a, "net_b": net_b, "decision": decision}

    if decision == "reject":
        ff_net_merge_candidates_repository.update(
            key, {"status": "rejected", "decided_at": now}
        )
        return result

    # approve: fold the higher my_net_id GROUP into the lower one.
    row_a = ff_net_building_repository.get({"net_id": net_a})
    row_b = ff_net_building_repository.get({"net_id": net_b})
    if row_a is None or row_b is None:
        raise LookupError("one or both nets no longer exist in ff_net_building")

    my_a, my_b = row_a["my_net_id"], row_b["my_net_id"]
    if my_a == my_b:
        # Already in the same group via some other approved pair -- nothing
        # left to move, just record the decision.
        ff_net_merge_candidates_repository.update(
            key, {"status": "approved", "decided_at": now}
        )
        result.update(survivor_my_net_id=my_a, absorbed_my_net_id=None, nets_moved=0)
        return result

    survivor, absorbed = sorted((my_a, my_b))
    absorbed_rows = ff_net_building_repository.find(
        filters={"my_net_id": absorbed}, limit=10000
    )
    for row in absorbed_rows:
        ff_net_building_repository.update(
            {"net_id": row["net_id"]}, {"my_net_id": survivor, "updated_at": now}
        )

    ff_net_merge_candidates_repository.update(
        key, {"status": "approved", "decided_at": now}
    )
    result.update(
        survivor_my_net_id=survivor,
        absorbed_my_net_id=absorbed,
        nets_moved=len(absorbed_rows),
    )
    return result
