"""Cassandra-backed orchestration for the FF-net building pipeline.

Bridges the pure matching/collapsing algorithm in net_building.py (which
works on DataFrames using its own column-naming convention, e.g.
FF_ID/Frequency_MHz/LTI) to the Cassandra tables, which use plain lowercase
column names. Only this module knows about that mapping and about Cassandra;
net_building.py itself stays storage-agnostic and untouched, so its existing
Excel CLI path keeps working unmodified.

ff_net_building is a derived table: every run recomputes the net hierarchy
from ff_net_report + ff_net_report_history and syncs it (delete rows that
disappeared or moved to a different net, upsert the rest) rather than
truncating. ff_net_report_history is append-only — a run only ever inserts
newly-processed reports into it, never rewrites or deletes existing rows.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

from apps.ff_net.submodules import net_building as nb
from apps.ff_net.submodules.ff_net_repository import (
    ff_net_report_repository,
    ff_net_building_repository,
    ff_net_report_history_repository,
)

# Only these columns need renaming to match net_building.py's REQUIRED_COLS /
# RECOMMENDED_COLS; every other column (branch, nationality, latitude, ...)
# passes through unchanged in both directions.
CASSANDRA_TO_DF = {
    "ff_id": "FF_ID", "frequency": "Frequency_MHz", "bandwidth": "Bandwidth_kHz",
    "rdfs": "RDFS", "fti": "FTI", "lti": "LTI", "nti": "NTI",
    "modulation": "Modulation", "signal_type": "Signal_Type",
}
DF_TO_CASSANDRA = {v: k for k, v in CASSANDRA_TO_DF.items()}
DATE_DF_COLS = ("FTI", "LTI")
DATE_CASSANDRA_COLS = ("fti", "lti")


# --------------------------------------------------------------- helpers ---

def _parse_ts(value):
    """Any Cassandra date/datetime value (or the ISO string
    apps.core.execution.rows_to_dicts produces for a Date) -> pd.Timestamp,
    or NaT."""
    if value is None:
        return pd.NaT
    return pd.to_datetime(value, errors="coerce")


def _clean(v):
    """NaN/NaT -> None; numpy scalars -> native Python; everything else as-is."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, np.generic):
        return v.item()
    return v


def _cassandra_row_to_df_row(row: dict) -> dict:
    """Rename+parse one Cassandra row-dict into net_building.py's DataFrame
    column convention (FTI/LTI become pd.Timestamp, which net_building's
    _to_dt() already handles natively)."""
    out = {}
    for k, v in row.items():
        key = CASSANDRA_TO_DF.get(k, k)
        if k in DATE_CASSANDRA_COLS:
            v = _parse_ts(v)
        out[key] = _clean(v)
    return out


def _df_row_to_cassandra(row: dict) -> dict:
    """Reverse of _cassandra_row_to_df_row, for writing rows back out.
    Skips net_id/Row_Type — callers set those explicitly since their meaning
    differs slightly by destination (building table vs. history table)."""
    out = {}
    for k, v in row.items():
        if k in ("net_id", "Row_Type"):
            continue
        key = DF_TO_CASSANDRA.get(k, k)
        if k in DATE_DF_COLS:
            t = nb._to_dt(v)
            v = t.date() if pd.notna(t) else None
        elif k in ("NTI", "FF_ID"):
            v = int(v) if v is not None and pd.notna(v) else None
        else:
            v = _clean(v)
        out[key] = v
    return out


def _building_keys(rows: list) -> set:
    return {r.get("net_id") for r in rows}


# ---------------------------------------------------------------- loading --

def fetch_new_reports() -> pd.DataFrame:
    """Every row currently in ff_net_report, in net_building.py's DataFrame
    column convention."""
    rows = ff_net_report_repository.find_all()
    return pd.DataFrame([_cassandra_row_to_df_row(r) for r in rows])


def load_members(building_rows: list):
    """Equivalent of net_building.load_members, sourced from ff_net_building.

    ff_net_building is now flat (one row per net), so every stored row is a
    real member record tagged with both my_net_id (MY_NET) and net_id
    (SYSTEM_NET) already.

    Returns (members_df, data_cols); (empty_df, None) when the table is empty
    so the caller can bootstrap from the input schema, matching
    net_building.load_members's contract.
    """
    if not building_rows:
        return pd.DataFrame(), None

    rows = [
        _cassandra_row_to_df_row({k: v for k, v in r.items() if k != "updated_at"})
        for r in building_rows
    ]
    members = pd.DataFrame(rows)
    data_cols = [c for c in members.columns if c not in ("net_id", "my_net_id")]
    return members[data_cols + ["my_net_id", "net_id"]], data_cols


def load_history() -> pd.DataFrame:
    """Equivalent of net_building.load_log, sourced from ff_net_report_history."""
    rows = ff_net_report_history_repository.find_all()
    if not rows:
        return pd.DataFrame()

    skip = {"net_id", "my_net_id", "action", "collapsed_ff_id", "was_collapsed", "processed_at"}
    out_rows = []
    for row in rows:
        out = _cassandra_row_to_df_row({k: v for k, v in row.items() if k not in skip})
        out["net_id"] = row.get("net_id")
        out["my_net_id"] = row.get("my_net_id")
        out["Action"] = row.get("action")
        out["Collapsed_FF_ID"] = row.get("collapsed_ff_id")
        out["Was_Collapsed"] = row.get("was_collapsed")
        out_rows.append(out)
    return pd.DataFrame(out_rows)


# ---------------------------------------------------------------- writing --

def _hier_to_cassandra_rows(hier: pd.DataFrame) -> list:
    now = dt.datetime.utcnow()
    rows = []
    for _, r in hier.iterrows():
        d = r.to_dict()
        my_net = d.pop("MY_NET")
        sys_net = d.pop("SYSTEM_NET")
        row = _df_row_to_cassandra(d)
        row["net_id"] = int(sys_net)
        row["my_net_id"] = int(my_net)
        row["updated_at"] = now
        rows.append(row)
    return rows


def _sync_building_table(hier: pd.DataFrame, old_building_rows: list) -> None:
    new_rows = _hier_to_cassandra_rows(hier)
    new_keys = {r["net_id"] for r in new_rows}
    old_keys = _building_keys(old_building_rows)

    for net_id in old_keys - new_keys:
        ff_net_building_repository.delete({"net_id": net_id})
    for row in new_rows:
        ff_net_building_repository.insert(row)


def _append_history(entries: list) -> None:
    now = dt.datetime.utcnow()
    skip = ("Action", "net_id", "my_net_id", "Collapsed_FF_ID", "Was_Collapsed")
    for entry in entries:
        row = _df_row_to_cassandra({k: v for k, v in entry.items() if k not in skip})
        net_id = entry.get("net_id")
        my_net_id = entry.get("my_net_id")
        row["net_id"] = int(net_id) if net_id is not None else None
        row["my_net_id"] = int(my_net_id) if my_net_id is not None else None
        row["processed_at"] = now
        row["action"] = entry.get("Action")
        collapsed_ff_id = entry.get("Collapsed_FF_ID")
        row["collapsed_ff_id"] = (
            int(collapsed_ff_id) if collapsed_ff_id is not None and pd.notna(collapsed_ff_id)
            else None
        )
        row["was_collapsed"] = bool(entry.get("Was_Collapsed"))
        ff_net_report_history_repository.insert(row)


def _hierarchy_to_json(hier: pd.DataFrame) -> list:
    """hier is flat now (one row per net), so this is a straight row -> dict
    conversion rather than a summary+emitters grouping."""
    if hier.empty:
        return []

    nets = []
    for _, r in hier.iterrows():
        d = r.to_dict()
        my_net = d.pop("MY_NET")
        sys_net = d.pop("SYSTEM_NET")
        record = _df_row_to_cassandra(d)
        for key in ("fti", "lti"):
            if record.get(key) is not None:
                record[key] = record[key].isoformat()
        record["my_net_id"] = int(my_net)
        record["net_id"] = int(sys_net)
        nets.append(record)
    return nets


# -------------------------------------------------------------- pipeline ---

def run_pipeline(cfg: Optional[nb.MatchConfig] = None) -> dict:
    """Fold every report in ff_net_report into the net hierarchy, sync the
    result into ff_net_building, append newly-processed reports to
    ff_net_report_history, and return the hierarchy as JSON.

    Idempotent: reports already recorded in ff_net_report_history are skipped
    (via net_building's signature-based dedup), so re-running with no new
    source rows is a no-op other than the returned JSON.
    """
    cfg = cfg or nb.MatchConfig()
    tally = {"new": 0, "collapsed": 0, "skipped": 0}

    building_rows = ff_net_building_repository.find_all()
    members, data_cols = load_members(building_rows)
    prev_log = load_history()
    new_df = fetch_new_reports()

    if new_df.empty and members.empty:
        return {
            "generated_at": dt.datetime.utcnow().isoformat() + "Z",
            "processed": tally,
            "nets": [],
        }

    bootstrap = data_cols is None
    if bootstrap:
        data_cols = [c for c in new_df.columns
                     if c not in ("net_id", "my_net_id", "MY_NET", "SYSTEM_NET", "Row_Type")]
        members = pd.DataFrame(columns=data_cols + ["my_net_id", "net_id"])

    seen, seen_cols = nb._seen_signatures(prev_log)
    sig_cols = tuple(c for c in seen_cols if c in new_df.columns)

    new_history_entries = []
    for _, rep in new_df.iterrows():
        if sig_cols:
            sig = nb._report_signature(rep, sig_cols)
            if sig in seen:
                tally["skipped"] += 1
                continue
            seen.add(sig)

        row = {c: rep[c] for c in data_cols if c in new_df.columns}
        for c in ("FTI", "LTI", "NTI", "RDFS"):
            if c in new_df.columns and c not in row:
                row[c] = rep[c]

        my_net_id, net_id, action, survivor_ffid = nb.assign_net(members, row, cfg)

        if action != "collapsed":
            row["my_net_id"] = my_net_id
            row["net_id"] = net_id
            rb = nb._report_band(row, cfg)
            if rb is not None:
                row["_f_lo"], row["_f_hi"] = rb
            members = pd.concat([members, pd.DataFrame([row])], ignore_index=True)

        tally[action] += 1

        entry = {c: rep[c] for c in new_df.columns}
        entry["Action"] = action
        entry["my_net_id"] = my_net_id
        entry["net_id"] = net_id
        entry["Collapsed_FF_ID"] = survivor_ffid if action == "collapsed" else None
        entry["Was_Collapsed"] = (action == "collapsed")
        new_history_entries.append(entry)

    members = nb.dedup_rdfs_per_net(members)
    hier = nb.build_hierarchical(members, data_cols)

    _sync_building_table(hier, building_rows)
    _append_history(new_history_entries)

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "processed": tally,
        "nets": _hierarchy_to_json(hier),
    }
