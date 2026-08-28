#!/usr/bin/env python3
"""
COMINT FF NET PIPELINE  -  unified build + incremental update
=============================================================
One entry point handles BOTH cases:

  * Net workbook ABSENT  -> the nets are BUILT from the input reports and a new
                            workbook is created.
  * Net workbook PRESENT -> each incoming report either UPDATES an existing
                            record (same emitter re-intercepted) or spawns a
                            brand-new net. The workbook is updated in place.

THREE-LEVEL STRUCTURE:
    MY_NET  (id 00001, 00002, ...)
      +- SYSTEM_NET  (id 100001, 100002, ...)
           +- FF interception report(s)  (the emitter rows)

    ONE record per net. Distinct emitters are NEVER clubbed together, even if
    their frequencies fall within tolerance of one another.

MATCHING RULE  (same emitter re-intercepted only):
    A NEW report updates an EXISTING record only when BOTH hold:
        * same RDFS (same intercepting station), AND
        * band overlap:  |f_A - f_B| <= (BW_A/2 + BW_B/2) + pad   [MHz]
    In that case the record is UPDATED (not cloned): FTI = earliest,
    LTI = latest, NTI = summed, all other fields from the newer record (max LTI).

    Anything else -> a brand-new MY_NET *and* a brand-new SYSTEM_NET. In
    particular, a report within frequency tolerance but on a DIFFERENT RDFS gets
    its own fresh MY_NET / SYSTEM_NET ids; it is not merged with the other net.

Outputs (two sheets in the net workbook + a mirror CSV of the raw log):
    Nets        - three-level view: a MY_NET header row, then a SYSTEM_NET
                  header row, then the FF interception report row(s).
    Emitter_Log - every raw incoming record ever processed, with an audit trail.

The workbook is written atomically (temp file + os.replace), so an interrupted
run can never leave a half-written or corrupted net file behind.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Union

import pandas as pd

log = logging.getLogger("net_pipeline")

# ============================ CONFIG ==================================

DT_FMT = "%d-%m-%Y %H:%M:%S"   # canonical OUTPUT format for FTI / LTI

# Accepted INPUT timestamp formats, tried in order. The source commonly uses
# colon-separated dates like '05:05:2024 02:13:36' (DD:MM:YYYY HH:MM:SS).
IN_FMTS = (
    "%d:%m:%Y %H:%M:%S",
    "%d:%m:%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)

# Columns that MUST be present for the pipeline to make sense.
REQUIRED_COLS = ("Frequency_MHz", "Bandwidth_kHz", "RDFS", "LTI")
# Columns we use if present but can synthesise/default when missing.
RECOMMENDED_COLS = ("FF_ID", "FTI", "NTI")

NETS_SHEET = "Nets"
LOG_SHEET = "Emitter_Log"

# ID formatting for the two header levels.
MY_NET_START = 1            # MY_NET ids: 00001, 00002, ...
MY_NET_WIDTH = 5           # zero-padded width
SYSTEM_NET_START = 100001  # SYSTEM_NET ids: 100001, 100002, ...


@dataclass(frozen=True)
class MatchConfig:
    """All the knobs that decide whether two records are the same emitter."""
    use_band_tol: bool = True             # True -> freq +/- BW/2 band overlap
    freq_tol_mhz: float = 0.4             # used only when use_band_tol is False
    band_pad_mhz: float = 0.0             # optional extra slack on the band test
    # Future: plug in a separate freq / bw tolerance. Leave None to use each
    # record's own half-bandwidth (Bandwidth_kHz / 2).
    freq_tol_override_mhz: Optional[float] = None
    bw_tol_override_mhz: Optional[float] = None
    # Legacy guards (off by default; the band test already subsumes the BW one).
    use_bw_guard: bool = False
    bw_tol_frac: float = 0.15
    match_signal_type: bool = False


# ======================= MATCHING PRIMITIVES =========================

def tune_tol(r1: pd.Series, r2: pd.Series, cfg: MatchConfig) -> float:
    """Max allowed |f1 - f2| (MHz) for r1 and r2 to be the same emitter.

        (bw_half_1 + bw_half_2) + freq_pad_1 + freq_pad_2 + band_pad

    bw_half uses cfg.bw_tol_override_mhz if set, else the record's
    Bandwidth_kHz/2. freq_pad is 0 unless cfg.freq_tol_override_mhz is set.
    Falls back to the flat cfg.freq_tol_mhz window when use_band_tol is False.
    """
    if not cfg.use_band_tol:
        return cfg.freq_tol_mhz

    def bw_half(rec: pd.Series) -> float:
        if cfg.bw_tol_override_mhz is not None:
            return cfg.bw_tol_override_mhz
        return (float(rec["Bandwidth_kHz"]) / 2.0) / 1000.0

    def freq_pad(_rec: pd.Series) -> float:
        return cfg.freq_tol_override_mhz if cfg.freq_tol_override_mhz is not None else 0.0

    return bw_half(r1) + bw_half(r2) + freq_pad(r1) + freq_pad(r2) + cfg.band_pad_mhz


def freq_match(r1: pd.Series, r2: pd.Series, cfg: MatchConfig) -> bool:
    """|f1 - f2| within the single tune_tol (freq +/- BW/2)."""
    d = abs(float(r1["Frequency_MHz"]) - float(r2["Frequency_MHz"]))
    return d <= tune_tol(r1, r2, cfg)


def same_emitter(r1: pd.Series, r2: pd.Series, cfg: MatchConfig) -> bool:
    """Cross-station match: frequency band (+ optional legacy guards). No DOA."""
    if not freq_match(r1, r2, cfg):
        return False
    if cfg.use_bw_guard and abs(float(r1["Bandwidth_kHz"]) - float(r2["Bandwidth_kHz"])) > \
            cfg.bw_tol_frac * float(r1["Bandwidth_kHz"]):
        return False
    if cfg.match_signal_type and r1.get("Signal_Type") != r2.get("Signal_Type"):
        return False
    return True


def same_emitter_same_station(r1: pd.Series, r2: pd.Series, cfg: MatchConfig) -> bool:
    """Same-station match: frequency band overlap (DOA not used)."""
    return same_emitter(r1, r2, cfg)


# ============================ TIME PARSING ===========================

def _to_dt(s) -> pd.Timestamp:
    """Parse an intercept timestamp using the known FF formats.

    Never fabricates today's date: an unparseable value returns NaT rather than
    letting pandas silently fill in the current date."""
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


# ============================ IO / READING ===========================

def _read_tabular(path: str, sheet=None) -> pd.DataFrame:
    """Read a .csv/.tsv/.xlsx/.xlsm/.xltx/.xls file into a DataFrame."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xls"):
        engine = "xlrd" if ext == ".xls" else "openpyxl"
        return pd.read_excel(path, sheet_name=(sheet if sheet is not None else 0),
                             engine=engine)
    if ext == ".tsv":
        return pd.read_csv(path, sep="\t")
    if ext == ".csv":
        return pd.read_csv(path)
    # Unknown extension: try CSV first, then Excel.
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_excel(path)


def read_new_reports(source: Union[str, dict, list, tuple, pd.DataFrame],
                     sheet=None) -> pd.DataFrame:
    """Normalise a single dict / list of dicts / file path / DataFrame into a
    DataFrame of incoming reports."""
    if isinstance(source, pd.DataFrame):
        return source.reset_index(drop=True)
    if isinstance(source, dict):
        return pd.DataFrame([source])
    if isinstance(source, (list, tuple)):
        return pd.DataFrame(list(source))
    if isinstance(source, str):
        return _read_tabular(source, sheet=sheet)
    raise TypeError(f"Unsupported source type: {type(source)!r}")


def load_members(net_file: str):
    """Rebuild the per-record table from the Nets sheet.

    Understands BOTH layouts, auto-detected by the presence of a 'Row_Type'
    column:
      * FLAT (current): one row per net, MY_NET and SYSTEM_NET as leading
        columns, followed by that net's single emitter record.
      * 3-LEVEL (legacy): MY_NET header row, then SYSTEM_NET header row, then
        the emitter row with a blank Row_Type. Ids are carried down from the
        header rows to the emitter row beneath them.

    Returns one row per emitter tagged with my_net_id + net_id (SYSTEM_NET id).
    When the workbook does not exist, returns (empty_df, None) so the caller can
    bootstrap from the input schema.
    """
    if not os.path.exists(net_file):
        return pd.DataFrame(), None

    try:
        hier = pd.read_excel(net_file, sheet_name=NETS_SHEET,
                             dtype={"MY_NET": str, "SYSTEM_NET": str})
    except ValueError:
        # File exists but has no 'Nets' sheet -> treat as empty/bootstrap.
        log.warning("'%s' has no '%s' sheet; treating as empty.", net_file, NETS_SHEET)
        return pd.DataFrame(), None

    def _as_int(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        try:
            return int(float(str(v).strip()))
        except (TypeError, ValueError):
            return None

    is_legacy = "Row_Type" in hier.columns and \
        hier["Row_Type"].astype(str).str.strip().isin(["MY_NET", "SYSTEM_NET"]).any()

    if is_legacy:
        # Legacy 3-level: carry ids down from header rows to the emitter row.
        cur_my, cur_sys = None, None
        my_ids, sys_ids, keep = [], [], []
        for _, r in hier.iterrows():
            rtype = r.get("Row_Type")
            rtype = "" if pd.isna(rtype) else str(rtype).strip()
            if rtype == "MY_NET":
                cur_my = _as_int(r.get("MY_NET")); cur_sys = None
                keep.append(False)
            elif rtype == "SYSTEM_NET":
                cur_sys = _as_int(r.get("SYSTEM_NET"))
                keep.append(False)
            else:
                has_data = (("FF_ID" in hier.columns and pd.notna(r.get("FF_ID")))
                            or ("Frequency_MHz" in hier.columns and pd.notna(r.get("Frequency_MHz"))))
                keep.append(bool(has_data) and cur_sys is not None)
            my_ids.append(cur_my); sys_ids.append(cur_sys)
        tmp = hier.assign(_my=my_ids, _sys=sys_ids, _keep=keep)
        members = tmp[tmp["_keep"]].copy()
        members["my_net_id"] = members["_my"]
        members["net_id"] = members["_sys"]
    else:
        # Flat: one row per net.
        members = hier.copy()
        members["my_net_id"] = members["MY_NET"].map(_as_int) if "MY_NET" in members.columns else None
        members["net_id"] = members["SYSTEM_NET"].map(_as_int) if "SYSTEM_NET" in members.columns else None
        members = members[members["net_id"].notna()].reset_index(drop=True)

    data_cols = [c for c in hier.columns
                 if c not in ("MY_NET", "SYSTEM_NET", "Row_Type",
                              "net_id", "my_net_id")]
    # Select only columns that actually exist, so a partially-formed or
    # differently-shaped net sheet can never raise a KeyError here.
    want = [c for c in data_cols + ["my_net_id", "net_id"] if c in members.columns]
    return members[want].reset_index(drop=True), data_cols


def load_log(net_file: str) -> pd.DataFrame:
    """Load the existing raw Emitter_Log sheet, if any."""
    if not os.path.exists(net_file):
        return pd.DataFrame()
    try:
        return pd.read_excel(net_file, sheet_name=LOG_SHEET)
    except (ValueError, FileNotFoundError):
        return pd.DataFrame()


# ======================= VALIDATION / SCHEMA =========================

def validate_reports(df: pd.DataFrame) -> None:
    """Fail fast with a clear message if the input can't be processed."""
    if df is None or len(df) == 0:
        raise ValueError("No reports to process (input is empty).")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input is missing required column(s): {missing}. "
            f"Required: {list(REQUIRED_COLS)}."
        )
    # Frequency / bandwidth must be numeric and populated to match anything.
    for c in ("Frequency_MHz", "Bandwidth_kHz"):
        coerced = pd.to_numeric(df[c], errors="coerce")
        if coerced.isna().any():
            bad = df.index[coerced.isna()].tolist()
            raise ValueError(
                f"Column '{c}' has non-numeric/empty values at rows {bad[:10]}"
                f"{' ...' if len(bad) > 10 else ''}."
            )
    for c in RECOMMENDED_COLS:
        if c not in df.columns:
            log.warning("Recommended column '%s' is absent; defaults will be used.", c)


# ========================= CORE OPERATIONS ===========================

_SIG_COLS = ("FF_ID", "FTI", "LTI", "NTI", "Frequency_MHz")


def _report_signature(row, cols):
    """A stable identity for a raw report, so re-ingesting the SAME record is a
    no-op (and never double-counts NTI). Time fields are normalised through the
    date parser and numbers coerced, so a colon-style input string matches the
    dash-style / datetime value already stored in the log."""
    sig = []
    for c in cols:
        try:
            v = row.get(c) if hasattr(row, "get") else row[c]
        except (KeyError, TypeError):
            v = None
        if c in ("FTI", "LTI"):
            t = _to_dt(v)
            sig.append(t.isoformat() if pd.notna(t) else None)
        elif c in ("FF_ID", "NTI"):
            try:
                sig.append(int(v) if pd.notna(v) else None)
            except (TypeError, ValueError):
                sig.append(str(v) if pd.notna(v) else None)
        elif c == "Frequency_MHz":
            try:
                sig.append(round(float(v), 6) if pd.notna(v) else None)
            except (TypeError, ValueError):
                sig.append(None)
        else:
            sig.append(v if pd.notna(v) else None)
    return tuple(sig)


def _seen_signatures(prev_log: pd.DataFrame):
    """Signatures of every raw report already in the running Emitter_Log."""
    if prev_log is None or len(prev_log) == 0:
        return set(), ()
    cols = tuple(c for c in _SIG_COLS if c in prev_log.columns)
    if not cols:
        return set(), ()
    return {_report_signature(r, cols) for _, r in prev_log.iterrows()}, cols


def _next_net_id(members: pd.DataFrame) -> int:
    """Next free SYSTEM_NET id (100001+ for a fresh build; max+1 thereafter)."""
    if len(members) and members["net_id"].notna().any():
        return int(members["net_id"].max()) + 1
    return SYSTEM_NET_START


def _next_my_net_id(members: pd.DataFrame) -> int:
    """Next free MY_NET id (1+ for a fresh build; max+1 thereafter)."""
    if len(members) and "my_net_id" in members.columns and members["my_net_id"].notna().any():
        return int(members["my_net_id"].max()) + 1
    return MY_NET_START


def collapse_into(members: pd.DataFrame, midx, new_report: dict):
    """Fold a re-intercept (same RDFS + overlapping band) into member `midx`.

    Keep the earliest FTI, advance LTI to the later of the two, SUM the NTI, and
    take non-time fields from whichever record is newer (by LTI)."""
    old = members.loc[midx]
    old_lti, new_lti = _to_dt(old.get("LTI")), _to_dt(new_report.get("LTI"))
    old_fti, new_fti = _to_dt(old.get("FTI")), _to_dt(new_report.get("FTI"))

    if pd.notna(new_lti) and (pd.isna(old_lti) or new_lti >= old_lti):
        for c in members.columns:
            if c in ("net_id", "my_net_id", "FTI", "LTI", "NTI"):
                continue
            if c in new_report and pd.notna(new_report.get(c)):
                val = new_report[c]
                # Guard against dtype clashes (e.g. a bool/str into a column
                # pandas inferred as float when the sheet was read back).
                try:
                    members.at[midx, c] = val
                except (TypeError, ValueError):
                    members[c] = members[c].astype("object")
                    members.at[midx, c] = val

    ftis = [t for t in (old_fti, new_fti) if pd.notna(t)]
    ltis = [t for t in (old_lti, new_lti) if pd.notna(t)]
    if ftis:
        members.at[midx, "FTI"] = min(ftis).strftime(DT_FMT)
    if ltis:
        members.at[midx, "LTI"] = max(ltis).strftime(DT_FMT)

    # Preserve the full band this emitter has ever been seen on, so narrow-band
    # frequency jitter across sightings keeps matching the same net instead of
    # fragmenting. We store the observed frequency span in hidden _f_lo/_f_hi
    # columns; the displayed Frequency_MHz/Bandwidth_kHz still come from the
    # newest record (handled in the field-copy loop above).
    def _band_edges(rec):
        try:
            f = float(rec.get("Frequency_MHz"))
        except (TypeError, ValueError):
            return None, None
        try:
            half = (float(rec.get("Bandwidth_kHz")) / 2.0) / 1000.0
        except (TypeError, ValueError):
            half = 0.0
        return f - half, f + half

    n_lo, n_hi = _band_edges(new_report)
    o_lo = old.get("_f_lo"); o_hi = old.get("_f_hi")
    los = [v for v in (o_lo, n_lo) if pd.notna(v)]
    his = [v for v in (o_hi, n_hi) if pd.notna(v)]
    if los:
        members.at[midx, "_f_lo"] = min(los)
    if his:
        members.at[midx, "_f_hi"] = max(his)

    if "NTI" in members.columns:
        o = old.get("NTI"); n = new_report.get("NTI")
        o = int(o) if pd.notna(o) else 0
        n = int(n) if pd.notna(n) else 0
        if o or n:
            members.at[midx, "NTI"] = o + n

    survivor_ffid = members.at[midx, "FF_ID"] if "FF_ID" in members.columns else None
    return int(members.at[midx, "net_id"]), survivor_ffid


def _report_band(rep, cfg: MatchConfig):
    """Occupied band [lo, hi] (MHz) for a raw report, from freq +/- BW/2."""
    try:
        f = float(rep.get("Frequency_MHz"))
    except (TypeError, ValueError):
        return None
    try:
        half = (float(rep.get("Bandwidth_kHz")) / 2.0) / 1000.0
    except (TypeError, ValueError):
        half = 0.0
    return f - half, f + half


def _member_band(row):
    """A net's full observed band, using _f_lo/_f_hi if we have been tracking
    them, else the single record's freq +/- BW/2."""
    lo = row.get("_f_lo") if hasattr(row, "get") else None
    hi = row.get("_f_hi") if hasattr(row, "get") else None
    if pd.notna(lo) and pd.notna(hi):
        return float(lo), float(hi)
    try:
        f = float(row.get("Frequency_MHz"))
    except (TypeError, ValueError):
        return None
    try:
        half = (float(row.get("Bandwidth_kHz")) / 2.0) / 1000.0
    except (TypeError, ValueError):
        half = 0.0
    return f - half, f + half


def assign_net(members: pd.DataFrame, new_report: dict, cfg: MatchConfig):
    """Route one report. Returns (my_net_id, net_id, action, survivor_ffid).
    action in {'collapsed', 'new'}.

    ONE record per net. Distinct emitters are never clubbed together across
    stations. A report UPDATES an existing record when it is the SAME emitter
    re-intercepted on the SAME RDFS, judged by EITHER:
        * same FF_ID (a real emitter id -> definitively the same emitter), OR
        * band overlap against the net's FULL observed band (freq +/- BW/2,
          widened across every sighting so narrow-band jitter never splits one
          emitter into separate nets).
    Everything else (different RDFS, or a genuinely different frequency) spawns
    a brand-new MY_NET + SYSTEM_NET.
    """
    if "RDFS" in members.columns and pd.notna(new_report.get("RDFS")):
        same_rdfs = members[members["RDFS"] == new_report["RDFS"]]

        new_ffid = new_report.get("FF_ID")
        rband = _report_band(new_report, cfg)
        pad = cfg.band_pad_mhz

        cand = []
        for idx, row in same_rdfs.iterrows():
            # 1) same real emitter id on the same station -> always same emitter
            if pd.notna(new_ffid) and "FF_ID" in row and pd.notna(row.get("FF_ID")):
                try:
                    if int(row["FF_ID"]) == int(new_ffid):
                        cand.append(idx)
                        continue
                except (TypeError, ValueError):
                    if str(row["FF_ID"]) == str(new_ffid):
                        cand.append(idx)
                        continue
            # 2) band overlap against the net's full observed band
            mband = _member_band(row)
            if rband is not None and mband is not None:
                if (rband[0] - pad) <= mband[1] and (rband[1] + pad) >= mband[0]:
                    cand.append(idx)

        if cand:
            def _lti_key(i):
                t = _to_dt(members.at[i, "LTI"])
                return pd.Timestamp.min if pd.isna(t) else t
            newest = max(cand, key=_lti_key)
            nid, survivor_ffid = collapse_into(members, newest, new_report)
            my_id = int(members.at[newest, "my_net_id"])
            return my_id, nid, "collapsed", survivor_ffid

    # Otherwise: a brand-new net (its own MY_NET and SYSTEM_NET).
    return _next_my_net_id(members), _next_net_id(members), "new", None


def dedup_rdfs_per_net(members: pd.DataFrame) -> pd.DataFrame:
    """Within each net, keep only ONE record per RDFS: the latest by LTI."""
    d = members.copy()
    d["_LTI_dt"] = d["LTI"].map(_to_dt)
    d = d.sort_values("_LTI_dt", ascending=False)
    d = d.drop_duplicates(subset=["net_id", "RDFS"], keep="first")
    return d.drop(columns=["_LTI_dt"]).reset_index(drop=True)


def _fmt_my_net(v) -> str:
    return str(int(v)).zfill(MY_NET_WIDTH)


def _fmt_system_net(v) -> str:
    return str(int(v))


def build_hierarchical(members: pd.DataFrame, data_cols: list) -> pd.DataFrame:
    """Emit a FLAT table: one row per net, with MY_NET and SYSTEM_NET as the
    leading columns followed by that net's single emitter record. Nets are
    emitted in ascending MY_NET / SYSTEM_NET order for a stable file.
    """
    out_cols = ["MY_NET", "SYSTEM_NET"] + data_cols
    if not len(members):
        return pd.DataFrame(columns=out_cols)

    m = members.copy()
    m["_LTI_dt"] = m["LTI"].map(_to_dt)
    rows = []
    for my_id, my_grp in m.groupby("my_net_id", sort=True):
        for sys_id, sys_grp in my_grp.groupby("net_id", sort=True):
            # One record per net; if more than one somehow present, latest wins.
            g = sys_grp.sort_values("_LTI_dt", ascending=False)
            r = g.iloc[0]
            row = {c: r[c] for c in data_cols}
            row["MY_NET"] = _fmt_my_net(my_id)
            row["SYSTEM_NET"] = _fmt_system_net(sys_id)
            rows.append(row)

    out = pd.DataFrame(rows)
    if not len(out):
        return pd.DataFrame(columns=out_cols)
    out = out[out_cols].reset_index(drop=True)
    # Keep the id columns as text so zero-padding (00001) is not lost to a
    # numeric round-trip when the workbook is re-read.
    for c in ("MY_NET", "SYSTEM_NET"):
        out[c] = out[c].astype("object")
    return out


def _atomic_write_workbook(net_file: str, hier: pd.DataFrame, full_log: pd.DataFrame) -> None:
    """Write the two-sheet workbook to a temp file in the same directory, then
    atomically replace the target so a crash can't corrupt an existing file."""
    dest_dir = os.path.dirname(os.path.abspath(net_file)) or "."
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=dest_dir)
    os.close(fd)
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl",
                            datetime_format="DD-MM-YYYY HH:MM:SS") as w:
            hier.to_excel(w, sheet_name=NETS_SHEET, index=False)
            full_log.to_excel(w, sheet_name=LOG_SHEET, index=False)

            # Store the id columns as TEXT cells so 00001 keeps its leading
            # zeros and is never re-read as a number.
            ws = w.sheets[NETS_SHEET]
            headers = {cell.value: cell.column for cell in ws[1]}
            for name in ("MY_NET", "SYSTEM_NET"):
                col = headers.get(name)
                if col is None:
                    continue
                for row in range(2, ws.max_row + 1):
                    cell = ws.cell(row=row, column=col)
                    if cell.value is not None:
                        cell.value = str(cell.value)
                        cell.number_format = "@"
        os.replace(tmp, net_file)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ============================ PIPELINE ===============================

def process_reports(net_file: str,
                    source,
                    cfg: MatchConfig = MatchConfig(),
                    log_csv: Optional[str] = None,
                    input_sheet=None,
                    dry_run: bool = False):
    """Build or update the net state from `source`.

    `source` may be a file path (.csv/.tsv/.xlsx/...), a single dict, a list of
    dicts, or a DataFrame. If `net_file` does not exist it is created from the
    input; otherwise the input is folded into the existing nets.

    Returns (results, n_nets) where results is a list of (label, action, net_id).
    """
    members, data_cols = load_members(net_file)
    prev_log = load_log(net_file)

    new_df = read_new_reports(source, sheet=input_sheet).reset_index(drop=True)
    validate_reports(new_df)

    bootstrap = data_cols is None
    if bootstrap:
        # Fresh build: take the column layout from the input reports.
        data_cols = [c for c in new_df.columns
                     if c not in ("net_id", "my_net_id", "MY_NET", "SYSTEM_NET", "Row_Type")]
        members = pd.DataFrame(columns=data_cols + ["my_net_id", "net_id"])
        log.info("Net workbook '%s' not found -> building new nets from input.", net_file)
    else:
        log.info("Net workbook '%s' found -> updating existing nets.", net_file)

    # Idempotency guard: skip any raw report already present in the log so a
    # re-run never re-collapses a record and inflates its NTI.
    seen, seen_cols = _seen_signatures(prev_log)
    sig_cols = tuple(c for c in seen_cols if c in new_df.columns)

    results, log_rows = [], []
    for i, rep in new_df.iterrows():
        label = new_df.at[i, "FF_ID"] if "FF_ID" in new_df.columns else i

        if sig_cols:
            sig = _report_signature(rep, sig_cols)
            if sig in seen:
                results.append((label, "skipped", None))
                log.info("  report %-8s %-9s (already ingested)", label, "SKIPPED")
                continue
            seen.add(sig)   # guard against duplicates within THIS batch too

        row = {c: rep[c] for c in data_cols if c in new_df.columns}
        for c in ("FTI", "LTI", "NTI", "RDFS"):     # carry through key fields
            if c in new_df.columns and c not in row:
                row[c] = rep[c]

        my_net_id, net_id, action, survivor_ffid = assign_net(members, row, cfg)

        if action != "collapsed":
            # Normalise FTI/LTI to the canonical output format before appending
            # so the sheet never mixes colon-style input with dash-style output.
            for _c in ("FTI", "LTI"):
                if _c in row:
                    _t = _to_dt(row[_c])
                    if pd.notna(_t):
                        row[_c] = _t.strftime(DT_FMT)
            row["my_net_id"] = my_net_id
            row["net_id"] = net_id
            # Seed this net's observed frequency band from the first sighting.
            _rb = _report_band(row, cfg)
            if _rb is not None:
                row["_f_lo"], row["_f_hi"] = _rb
            members = pd.concat([members, pd.DataFrame([row])], ignore_index=True)

        results.append((label, action, net_id))
        log.info("  report %-8s %-9s -> MY_NET %s / SYSTEM_NET %s",
                 label, action.upper(), _fmt_my_net(my_net_id), _fmt_system_net(net_id))

        entry = {c: rep[c] for c in new_df.columns}
        entry["Action"] = action
        entry["MY_NET"] = _fmt_my_net(my_net_id)
        entry["SYSTEM_NET"] = _fmt_system_net(net_id)
        entry["net_id"] = net_id
        entry["Collapsed_FF_ID"] = survivor_ffid if action == "collapsed" else None
        entry["Was_Collapsed"] = (action == "collapsed")
        log_rows.append(entry)

    members = dedup_rdfs_per_net(members)
    hier = build_hierarchical(members, data_cols)

    new_log = pd.DataFrame(log_rows)
    full_log = pd.concat([prev_log, new_log], ignore_index=True) if len(prev_log) else new_log
    # Re-running the SAME batch must not inflate the log.
    key = [c for c in ("FF_ID", "FTI", "LTI", "NTI") if c in full_log.columns]
    if key:
        full_log = full_log.drop_duplicates(subset=key, keep="last").reset_index(drop=True)

    n_nets = int(members["net_id"].nunique()) if len(members) else 0

    if dry_run:
        log.info("[dry-run] would write %d net rows and %d log rows to '%s' "
                 "(not written).", len(hier), len(full_log), net_file)
        return results, n_nets

    _atomic_write_workbook(net_file, hier, full_log)
    if log_csv:
        full_log.to_csv(log_csv, index=False)

    return results, n_nets


# backward-compatible aliases (old call sites keep working)
def process_batch(net_file, new_file):
    return process_reports(net_file, new_file)


# ============================== CLI ==================================

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build or incrementally update COMINT FF nets from reports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", default="ff_reports_big.xlsx",
                   help="Reports to ingest: .csv/.tsv/.xlsx/.xls path. "
                        "Defaults to 'ff_reports_big.xlsx'.")
    p.add_argument("--net-file", default="net_building_updated.xlsx",
                   help="Net workbook to create or update.")
    p.add_argument("--log-csv", default="emitter_log_updated.csv",
                   help="Mirror of the raw Emitter_Log as a standalone CSV.")
    p.add_argument("--input-sheet", default="reports",
                   help="Sheet name/index for an Excel input (default: 'reports').")
    p.add_argument("--flat-freq-tol", type=float, default=None, metavar="MHZ",
                   help="Use a flat +/- freq window (MHz) instead of band overlap.")
    p.add_argument("--band-pad", type=float, default=0.0, metavar="MHZ",
                   help="Extra slack added to the band-overlap test.")
    p.add_argument("--dry-run", action="store_true",
                   help="Process and report, but do not write any files.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose (DEBUG) logging.")
    return p


# One record to add by hand when no input path is given.
MANUAL_REPORT = {
    "FF_ID": 16,
    "Frequency_MHz": 726.355,
    "Bandwidth_kHz": 1116.302,
    "Modulation": "AM",
    "Signal_Type": "data",
    "RDFS": "RDFS5",
    "FTI": "02:07:2024 12:00:00",
    "LTI": "02:07:2024 12:50:00",
    "NTI": 2,
}


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = MatchConfig(
        use_band_tol=(args.flat_freq_tol is None),
        freq_tol_mhz=(args.flat_freq_tol if args.flat_freq_tol is not None else 0.4),
        band_pad_mhz=args.band_pad,
    )

    if args.input is not None:
        source = args.input
        # cast a numeric sheet arg to int (e.g. "--input-sheet 1")
        sheet = args.input_sheet
        if isinstance(sheet, str) and sheet.isdigit():
            sheet = int(sheet)
        log.info("Processing reports from '%s' into '%s' ...", source, args.net_file)
    else:
        source = MANUAL_REPORT
        sheet = None
        log.info("Processing ONE manual report into '%s' ...", args.net_file)

    freq_desc = ("band overlap (f +/- BW/2)" if cfg.use_band_tol
                 else f"flat {cfg.freq_tol_mhz} MHz")
    log.info("CONFIG: freq_tol=%s | band_pad=%s | BW_guard=%s | match_sig=%s | DOA=NOT USED",
             freq_desc, cfg.band_pad_mhz, cfg.use_bw_guard, cfg.match_signal_type)

    try:
        results, n_nets = process_reports(
            args.net_file, source, cfg=cfg,
            log_csv=args.log_csv, input_sheet=sheet, dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        log.error("Input not found: %s", e)
        return 2
    except (ValueError, TypeError) as e:
        log.error("%s", e)
        return 2

    tally = Counter(a for _, a, _ in results)
    log.info("\nProcessed %d report(s): %s",
             len(results), ", ".join(f"{k}={v}" for k, v in tally.items()) or "none")
    verb = "would total" if args.dry_run else "updated ->"
    log.info("%s %s %d nets total", args.net_file, verb, n_nets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
