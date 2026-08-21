#!/usr/bin/env python3
"""
NET-TO-NET MERGE CANDIDATE DETECTION  --  pure algorithm, no Cassandra/Django
==============================================================================
Unlike net_building.py (which folds RAW REPORTS into nets automatically, no
human in the loop), this module never merges anything by itself. It only
answers one question: "do these two ALREADY-BUILT nets look like the same
tactical net?" Turning a yes into an actual merge is a human (commander)
decision, applied by net_merging_service.apply_decision() -- see that
module's docstring for the approve/reject workflow.

Ported from the standalone net_merging_df.py / net_merging_json.py scripts'
matching math, adapted to operate directly on ff_net_building rows (one row
per net already -- no MY_NET-blank-row grouping convention needed, since
every net_id row is independently addressable in Cassandra). One
intentional naming difference from those scripts: latitude/longitude are
read lowercase here (`latitude`/`longitude`), matching the field names
net_building_service.py's Cassandra<->DataFrame column mapping actually
produces, instead of the scripts' `Latitude`/`Longitude`.

TWO NETS ARE A MERGE CANDIDATE when ALL THREE hold:
    1) FREQUENCY : bands overlap  |f_A - f_B| <= (BW_A/2 + BW_B/2) + pads   (MHz)
    2) TIME (LTI): |LTI_A - LTI_B| <= LTI_TOL_SEC                          (seconds)
    3) LOCATION  : geodesic_distance(A, B) <= LOC_TOL_M                    (metres)

This is deliberately a DIFFERENT (looser, cross-station) test than
net_building.assign_net's RDFS+band collapse rule. net_building only ever
folds a re-intercept from the SAME station into a net in real time; merging
is the separate, human-approved recognition that two DIFFERENT stations'
nets are actually the same tactical net, which needs the extra time/location
corroboration net_building's RDFS-based collapse doesn't have.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

IN_FMTS = (
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%d:%m:%Y %H:%M:%S", "%d:%m:%Y %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
)


@dataclass(frozen=True)
class MergeConfig:
    """All the knobs that decide whether two nets are a merge candidate."""
    use_band_tol: bool = True
    freq_tol_mhz: float = 0.4             # used only when use_band_tol is False
    band_pad_mhz: float = 0.0
    freq_tol_override_mhz: Optional[float] = None
    bw_tol_override_mhz: Optional[float] = None
    use_time_tol: bool = True
    lti_tol_sec: float = 5.0
    use_loc_tol: bool = True
    loc_tol_m: float = 100.0


# ============================ TIME PARSING ===========================

def _to_dt(s) -> pd.Timestamp:
    if s is None:
        return pd.NaT
    if isinstance(s, pd.Timestamp) or hasattr(s, "year"):
        return pd.Timestamp(s)
    if not isinstance(s, str):
        try:
            if pd.isna(s):
                return pd.NaT
        except (TypeError, ValueError):
            pass
        t = pd.to_datetime(s, errors="coerce")
        return t if (pd.notna(t) and getattr(t, "year", None)) else pd.NaT
    s = s.strip()
    if not s:
        return pd.NaT
    for fmt in IN_FMTS:
        t = pd.to_datetime(s, format=fmt, errors="coerce")
        if pd.notna(t):
            return t
    return pd.NaT


# ======================= THE THREE CONDITION CHECKS ===================

def tune_tol(r1, r2, cfg: MergeConfig) -> float:
    if not cfg.use_band_tol:
        return cfg.freq_tol_mhz

    def bw_half(rec):
        if cfg.bw_tol_override_mhz is not None:
            return cfg.bw_tol_override_mhz
        return (float(rec["Bandwidth_kHz"]) / 2.0) / 1000.0

    def freq_pad(_rec):
        return cfg.freq_tol_override_mhz if cfg.freq_tol_override_mhz is not None else 0.0

    return bw_half(r1) + bw_half(r2) + freq_pad(r1) + freq_pad(r2) + cfg.band_pad_mhz


def freq_match(r1, r2, cfg: MergeConfig) -> bool:
    return abs(float(r1["Frequency_MHz"]) - float(r2["Frequency_MHz"])) <= tune_tol(r1, r2, cfg)


def _lti_gap_sec(r1, r2):
    t1, t2 = _to_dt(r1.get("LTI")), _to_dt(r2.get("LTI"))
    if pd.isna(t1) or pd.isna(t2):
        return None
    return abs((t1 - t2).total_seconds())


def time_match(r1, r2, cfg: MergeConfig) -> bool:
    if not cfg.use_time_tol:
        return True
    g = _lti_gap_sec(r1, r2)
    return g is not None and g <= cfg.lti_tol_sec


def _loc_dist_m(r1, r2):
    try:
        lat1, lon1 = float(r1["latitude"]), float(r1["longitude"])
        lat2, lon2 = float(r2["latitude"]), float(r2["longitude"])
    except (TypeError, ValueError, KeyError):
        return None
    if any(pd.isna(v) for v in (lat1, lon1, lat2, lon2)):
        return None
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def loc_match(r1, r2, cfg: MergeConfig) -> bool:
    if not cfg.use_loc_tol:
        return True
    d = _loc_dist_m(r1, r2)
    return d is not None and d <= cfg.loc_tol_m


def same_net(r1, r2, cfg: MergeConfig) -> bool:
    """All enabled conditions must hold for two nets to be a merge candidate."""
    return freq_match(r1, r2, cfg) and time_match(r1, r2, cfg) and loc_match(r1, r2, cfg)


def match_reasons(r1, r2, cfg: MergeConfig) -> dict:
    """Human-readable numbers behind a match, for the commander-facing alert."""
    fa, fb = float(r1["Frequency_MHz"]), float(r2["Frequency_MHz"])
    reasons = {
        "freq_gap_mhz": round(abs(fa - fb), 4),
        "freq_tol_mhz": round(tune_tol(r1, r2, cfg), 4),
    }
    if cfg.use_time_tol:
        g = _lti_gap_sec(r1, r2)
        reasons["lti_gap_sec"] = None if g is None else round(g, 1)
        reasons["lti_tol_sec"] = cfg.lti_tol_sec
    if cfg.use_loc_tol:
        d = _loc_dist_m(r1, r2)
        reasons["distance_m"] = None if d is None else round(d, 1)
        reasons["loc_tol_m"] = cfg.loc_tol_m
    return reasons


# ============================ CANDIDATE SEARCH =========================

def propose_pairs(nets: pd.DataFrame, cfg: Optional[MergeConfig] = None) -> list:
    """nets: one row per ff_net_building record (net_id, my_net_id,
    Frequency_MHz, Bandwidth_kHz, LTI, latitude, longitude, ...).

    Returns a list of (net_id_a, net_id_b, reasons) for every pair across
    DIFFERENT my_net_id groups that satisfies same_net(). net_id_a < net_id_b
    always.

    Nets already in the SAME my_net_id group (a previously-approved merge)
    are never compared against each other -- there's nothing new to propose
    there. This is an O(n^2) scan over every net; fine at the current data
    volumes (hundreds of nets), but revisit if that grows by orders of
    magnitude.
    """
    cfg = cfg or MergeConfig()
    rows = nets.to_dict("records")
    candidates = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a.get("my_net_id") == b.get("my_net_id"):
                continue
            if same_net(a, b, cfg):
                lo, hi = sorted((int(a["net_id"]), int(b["net_id"])))
                reasons = match_reasons(a, b, cfg)
                candidates.append((lo, hi, reasons))
    return candidates
