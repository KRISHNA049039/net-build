# `net_building_service.py` + `net_merging_service.py` — full code walkthrough

Companion to [`NET_BUILDING_CODE_WALKTHROUGH.md`](NET_BUILDING_CODE_WALKTHROUGH.md)
and [`NET_MERGING_CODE_WALKTHROUGH.md`](NET_MERGING_CODE_WALKTHROUGH.md) (the two
pure, storage-agnostic algorithm modules) and [`PIPELINE_REFERENCE.md`](PIPELINE_REFERENCE.md) /
[`MERGE_PIPELINE_REFERENCE.md`](MERGE_PIPELINE_REFERENCE.md) (schema, endpoint
shapes, debugging playbooks). Those algorithm walkthroughs explain *what*
`assign_net`/`propose_pairs`/etc. decide; this file explains the two
**service** modules that actually call them against Cassandra —
`submodules/net_building_service.py` and `submodules/net_merging_service.py`
— function by function, in call order.

Both services follow the same shape: a pure algorithm module
(`net_building.py`/`net_merging.py`) that only knows about DataFrames and its
own `PascalCase`/`Title_Case` column convention, wrapped by a service module
that is the *only* code that knows Cassandra exists. `views.py` calls exactly
one function per endpoint — `net_building_service.run_pipeline()`,
`net_merging_service.list_pending_candidates()`, and
`net_merging_service.apply_decision()` — everything else in these two files
is a private helper reachable only through those three entry points.

---

## 1. `net_building_service.py`

### 1.1 The column mapping (lines 34–41)

```python
CASSANDRA_TO_DF = {
    "ff_id": "FF_ID", "frequency": "Frequency_MHz", "bandwidth": "Bandwidth_kHz",
    "rdfs": "RDFS", "fti": "FTI", "lti": "LTI", "nti": "NTI",
    "modulation": "Modulation", "signal_type": "Signal_Type",
}
DF_TO_CASSANDRA = {v: k for k, v in CASSANDRA_TO_DF.items()}
DATE_DF_COLS = ("FTI", "LTI")
DATE_CASSANDRA_COLS = ("fti", "lti")
```

Only the columns `net_building.py`'s `REQUIRED_COLS`/`RECOMMENDED_COLS` care
about need renaming — everything else (`branch`, `nationality`, `latitude`,
...) round-trips unchanged in both directions since the algorithm module
never inspects those names. `DF_TO_CASSANDRA` is derived from
`CASSANDRA_TO_DF` by inversion rather than written out separately, so the two
maps can never drift out of sync with each other.

### 1.2 Row-shape helpers (lines 46–101)

- **`_parse_ts(value)`** — any Cassandra date/datetime value (or the ISO
  string `apps.core.execution.rows_to_dicts` already produces for a `Date`
  column) into a `pd.Timestamp`, or `pd.NaT` for `None`. Thin wrapper over
  `pd.to_datetime(value, errors="coerce")`.
- **`_clean(v)`** — normalizes one scalar for the trip *into* a DataFrame:
  `NaN`/`NaT` → `None`, a numpy scalar (`np.int64`, `np.float64`, ...) → the
  native Python equivalent via `.item()`, everything else passed through.
  The `pd.isna(v)` check is wrapped in `try/except (TypeError, ValueError)`
  because `pd.isna` raises on some non-scalar inputs rather than returning
  `False`.
- **`_cassandra_row_to_df_row(row)`** — one Cassandra row-dict →
  `net_building.py`'s DataFrame convention: renames each key through
  `CASSANDRA_TO_DF` (unmapped keys pass through as-is), runs `fti`/`lti`
  through `_parse_ts` first (so they arrive as `pd.Timestamp`, which
  `net_building._to_dt` already handles natively — see that file's
  walkthrough §4), then `_clean`s every value. This is the function both
  `fetch_new_reports` and `load_members`/`load_history` funnel every row
  through.
- **`_df_row_to_cassandra(row)`** — the reverse, for writing rows back out.
  Explicitly skips `net_id`/`Row_Type` — every call site sets those itself
  since their meaning differs slightly by destination table (see §1.4/§1.5).
  For `FTI`/`LTI` it re-parses via `nb._to_dt` and takes `.date()` (Cassandra
  stores these as `date`, not `timestamp`) or `None` if unparseable. `NTI`/
  `FF_ID` are coerced to `int` (or `None`). Everything else goes through
  `_clean`.
- **`_building_keys(rows)`** — `{r["net_id"] for r in rows}`, a one-liner
  used only by `_sync_building_table` to diff old vs. new key sets.

### 1.3 Loading current state (lines 106–152)

- **`fetch_new_reports()`** — `ff_net_report_repository.find_all()`, mapped
  row-by-row through `_cassandra_row_to_df_row`, into a DataFrame. This is
  every row currently sitting in `ff_net_report`, regardless of whether it's
  already been processed — the idempotency skip happens later, inside
  `run_pipeline`'s loop, not here.
- **`load_members(building_rows)`** — the Cassandra-backed equivalent of
  `net_building.load_members`, but simpler: because `ff_net_building` is
  already flat (one row per net, no legacy header-row layout to detect), it's
  a straight map-and-frame. `data_cols` is every column except
  `net_id`/`my_net_id`. Returns `(pd.DataFrame(), None)` when `building_rows`
  is empty — the `None` is the same "bootstrap from the input's own schema"
  signal the Excel-path function uses, and `run_pipeline` checks it the same
  way (§1.6 step 3).
- **`load_history()`** — the equivalent of `net_building.load_log`, sourced
  from `ff_net_report_history_repository.find_all()`. Strips the
  history-only columns (`net_id`, `my_net_id`, `action`, `collapsed_ff_id`,
  `was_collapsed`, `processed_at`) before running each row through
  `_cassandra_row_to_df_row`, then adds back `net_id`/`my_net_id`/`Action`/
  `Collapsed_FF_ID`/`Was_Collapsed` under the names `net_building.py`'s log
  format expects. This is what feeds `nb._seen_signatures` in
  `run_pipeline`.

### 1.4 Writing state back out (lines 157–221)

- **`_hier_to_cassandra_rows(hier)`** — turns `build_hierarchical`'s output
  DataFrame into a list of Cassandra-shaped row dicts: pops `MY_NET`/
  `SYSTEM_NET` off each row, runs the rest through `_df_row_to_cassandra`,
  then adds back `net_id`/`my_net_id` as `int` and stamps `updated_at =
  datetime.utcnow()` on every row.
- **`_sync_building_table(hier, old_building_rows)`** — `ff_net_building` is
  a *derived* table (per the module docstring): every `run_pipeline` call
  recomputes the full hierarchy from `ff_net_report`/`ff_net_report_history`
  and syncs it in, rather than truncating first. Concretely: diff
  `old_keys - new_keys` (nets present before this run but absent from the
  freshly computed `hier` — this can't currently happen since nets are never
  deleted from the algorithm's output, but the sync logic doesn't assume
  that) and `delete()` each; then `insert()` every row in `new_rows`
  regardless of whether it's new or unchanged (Cassandra `INSERT` is an
  upsert, so re-writing an unchanged row is harmless, just not free).
- **`_append_history(entries)`** — `ff_net_report_history` is append-only,
  unlike the building table: this only ever inserts, never deletes or
  updates. For each entry it strips the history-only keys before
  `_df_row_to_cassandra`, then adds back `net_id`/`my_net_id` (as `int`),
  `processed_at` (`utcnow()`), `action`, `collapsed_ff_id` (`int` or `None`
  — guarded with `pd.notna` since a missing survivor comes through as
  `NaN`, not `None`, when it passed through a DataFrame row), and
  `was_collapsed` (coerced to `bool`).
- **`_hierarchy_to_json(hier)`** — the shape returned to the API caller.
  Since `hier` is already flat (one row per net — the multi-level
  summary/emitters grouping from the old MEMBER-rows model is gone, per
  `NET_BUILDING_CODE_WALKTHROUGH.md` §1), this is a straight per-row
  conversion: pop `MY_NET`/`SYSTEM_NET`, run the rest through
  `_df_row_to_cassandra` (reusing the exact same field coercions used for
  the actual Cassandra write), then `.isoformat()` the `fti`/`lti` `date`
  values so the result is JSON-serializable, and add back `my_net_id`/
  `net_id` as `int`. Empty input short-circuits to `[]`.

### 1.5 `run_pipeline(cfg=None)` (lines 226–303) — the orchestrator

The only function `views.py`'s `NetBuildView.get` calls. Mirrors
`net_building.process_reports` step for step, with Cassandra reads/writes in
place of workbook I/O:

1. `cfg = cfg or nb.MatchConfig()` — defaults match the live API's documented
   behavior in `NET_BUILDING_CODE_WALKTHROUGH.md` §2.
2. Load current state: `building_rows = ff_net_building_repository.find_all()`,
   `members, data_cols = load_members(building_rows)`,
   `prev_log = load_history()`, `new_df = fetch_new_reports()`.
3. **Empty short-circuit**: if both `new_df` and `members` are empty, return
   immediately with a zeroed tally and `nets: []` — nothing to bootstrap from
   and nothing to process.
4. **Bootstrap**: if `load_members` signaled `data_cols is None` (table was
   empty), derive `data_cols` from `new_df`'s own columns (minus routing
   columns `net_id`/`my_net_id`/`MY_NET`/`SYSTEM_NET`/`Row_Type`) and
   initialize `members` as an empty, correctly-shaped DataFrame — same logic
   as the Excel path's bootstrap step.
5. `seen, seen_cols = nb._seen_signatures(prev_log)`, intersected with
   `new_df`'s actual columns → `sig_cols`. This is the idempotency guard:
   `PIPELINE_REFERENCE.md` §3.8 covers why the 5-tuple signature
   (`FF_ID`/`FTI`/`LTI`/`NTI`/`Frequency_MHz`) is what makes re-running with
   no new source rows a no-op.
6. **Per-report loop** over `new_df.iterrows()`, byte-for-byte the same logic
   as `process_reports`' loop (see `NET_BUILDING_CODE_WALKTHROUGH.md` §9 step
   5 for the full breakdown): compute+check the signature, skip if already
   `seen` (and don't log it again); otherwise build `row` from `data_cols`
   (force-carrying `FTI`/`LTI`/`NTI`/`RDFS`), call `nb.assign_net(members,
   row, cfg)`; if the action wasn't `"collapsed"`, stamp the new
   `my_net_id`/`net_id`, seed `_f_lo`/`_f_hi` from `nb._report_band`, and
   append to `members` in memory (a `"collapsed"` action already mutated
   `members` in place inside `assign_net`/`collapse_into`); tally the action;
   append a history entry with `Action`/`Collapsed_FF_ID`/`Was_Collapsed`.
7. After the loop: `members = nb.dedup_rdfs_per_net(members)`, then
   `hier = nb.build_hierarchical(members, data_cols)` — same two calls the
   Excel path makes at the same point.
8. `_sync_building_table(hier, building_rows)` and
   `_append_history(new_history_entries)` — the two Cassandra writes. Note
   `building_rows` (the *pre-run* snapshot) is what's diffed against, not a
   re-fetch, since nothing outside this call could have changed it mid-run.
9. Return `{generated_at, processed: tally, nets: _hierarchy_to_json(hier)}`.

Everything that makes this **idempotent** — re-running with no new
`ff_net_report` rows produces the same `nets` output and doesn't grow
`ff_net_report_history` — lives entirely in step 5/6 (the signature guard)
and is inherited unmodified from `net_building.py`; this module only had to
get the Cassandra ↔ DataFrame translation right around it.

---

## 2. `net_merging_service.py`

Same overall shape as §1, but the workflow itself is different in kind:
`net_building_service.run_pipeline()` is **fully automatic** (every call can
change `ff_net_building`), while nothing here ever changes `ff_net_building`
except an explicit, human-approved `apply_decision(..., "approve")` call —
see the module docstring (lines 1–25) and `MERGE_PIPELINE_REFERENCE.md` §5
for the product-level rationale.

### 2.1 Loading (lines 45–57)

- **`_fetch_nets()`** — every row in `ff_net_building`, mapped through
  `net_building_service._cassandra_row_to_df_row` (reused directly, not
  reimplemented — `ff_net_building` is the exact table
  `net_building_service` itself writes, so the same column convention
  applies). Empty table → empty DataFrame.
- **`_existing_pairs()`** — `{(r["net_a"], r["net_b"]) for r in
  ff_net_merge_candidates_repository.find_all()}`. This is the full
  ledger — *every* status (`pending`/`approved`/`rejected`), not just
  pending — which is what makes a rejected pair stay rejected forever (see
  §2.2).

### 2.2 Detection (lines 62–104)

- **`_snapshot_row(net_a, net_b, reasons, nets_by_id)`** — builds one
  `ff_net_merge_candidates` insert row for a freshly-detected pair: both
  nets' `my_net_id`/`RDFS`/`Frequency_MHz`, each `LTI` reduced to `.date()`,
  every field from `net_merging.match_reasons` (`freq_gap_mhz`/
  `freq_tol_mhz`/`lti_gap_sec`/`lti_tol_sec`/`distance_m`/`loc_tol_m` — any
  of the latter four `match_reasons` omitted because its check was disabled
  in `cfg` come through as `None` here via `.get()`), `status="pending"`,
  `detected_at=utcnow()`, `decided_at=None`.
- **`detect_new_candidates(cfg=None)`** — fetches `_fetch_nets()`, bails
  `0` if empty; otherwise builds `nets_by_id` (keyed by `net_id`, for
  `_snapshot_row`'s lookups), calls `nm.propose_pairs(nets, cfg)` (the O(n²)
  scan documented in `NET_MERGING_CODE_WALKTHROUGH.md` §5), and for each
  candidate pair not already in `_existing_pairs()`, inserts a snapshot row
  and counts it. Returns the count of genuinely new insertions. Because
  `_existing_pairs()` includes rejected pairs, a commander's rejection is
  permanent — the pair is structurally incapable of being re-proposed as
  long as its `ff_net_merge_candidates` row exists.
- **`list_pending_candidates(cfg=None)`** — the function `views.py`'s
  `NetMergeCandidatesView.get` calls directly. Always calls
  `detect_new_candidates(cfg)` first (so every poll is current), then reads
  every row and filters to `status == "pending"`. The comment on lines
  112–114 is worth internalizing: `find_all()` → `rows_to_dicts` has already
  turned Cassandra `date`/`timestamp` columns into ISO strings by this point,
  so the returned rows need no further serialization before going straight
  into the `Response`. Returns `{generated_at, newly_detected, pending_count,
  candidates}`.

### 2.3 Deciding — `apply_decision(net_a, net_b, decision)` (lines 127–187)

The only function that can ever change `ff_net_building` after a net is
first built. Called by both `NetMergeApproveView` and `NetMergeRejectView`
in `views.py` (via `_NetMergeDecisionBase.post`) with `decision` fixed per
subclass.

1. **Validate `decision`** — must be `"approve"` or `"reject"`
   (`VALID_DECISIONS`), else `ValueError`.
2. **Canonicalize the pair**: `net_a, net_b = sorted((int(net_a),
   int(net_b)))` — matches `propose_pairs`' own `(lo, hi)` ordering, so the
   lookup below always hits regardless of which order the caller's URL
   passed them in.
3. **Look up the candidate**: `ff_net_merge_candidates_repository.get({net_a,
   net_b})`; `None` → `LookupError` (surfaced by `views.py` as a 404).
   `status != "pending"` → `ValueError` naming what it already was (surfaced
   as a 400) — a pair can only be decided once.
4. **`reject`**: just `update(key, {status: "rejected", decided_at:
   utcnow()})` and return `{net_a, net_b, decision}`. `ff_net_building` is
   never touched.
5. **`approve`**: re-fetch both nets by `net_id` from `ff_net_building`
   (not trusted from the stale candidate-row snapshot) — either missing →
   `LookupError` (a net can disappear between detection and decision if
   `run_pipeline` ran again in between, though nets aren't currently
   deleted by that path).
   - **Already-merged short-circuit**: if `row_a["my_net_id"] ==
     row_b["my_net_id"]` (this pair already ended up in the same group via
     some *other* approved pair — see the incremental 3-way example below),
     there's nothing left to move: just record `status="approved"` and
     return with `nets_moved=0`.
   - **Otherwise, fold groups**: `survivor, absorbed = sorted((my_a, my_b))`
     — the numerically **lower** `my_net_id` always wins, regardless of
     which net was `net_a`/`net_b` or which was approved more recently.
     Fetch every row in `ff_net_building` where `my_net_id == absorbed`
     (`find(filters={"my_net_id": absorbed}, limit=10000)` — every net
     currently in that group, not just the two nets in this pair), and
     `update` each one's `my_net_id` to `survivor` (plus a fresh
     `updated_at`). Then mark the candidate row `approved`, and return
     `{net_a, net_b, decision, survivor_my_net_id, absorbed_my_net_id,
     nets_moved}`.

**Why whole groups move, not just the pair**: this is what lets a
three-or-more-net merge happen incrementally without ever re-deciding an
already-approved pair. Approve `(A, B)` → both now share `my_net_id =
min(my_A, my_B)`. Later approve `(B, C)` → `B`'s group (which by now
includes `A`) gets folded into `C`'s group, or vice versa, purely by
comparing `my_net_id`s at decision time — `A` moves along with `B`
automatically because the fold operates on "every net sharing this
`my_net_id`," not on the specific two `net_id`s in the candidate row. See
`MERGE_PIPELINE_REFERENCE.md` §5.3 for the fully worked pseudocode/example.

---

## 3. How `views.py` wires these together

Each endpoint calls exactly one service function, with a shared
`cassandra_session.is_ready()` guard (503 if not) in front:

| Endpoint | View | Service call |
|---|---|---|
| `GET /api/ff_net/build/` | `NetBuildView` | `net_building_service.run_pipeline()` |
| `GET /api/ff_net/merge/candidates/` | `NetMergeCandidatesView` | `net_merging_service.list_pending_candidates()` |
| `POST /api/ff_net/merge/candidates/<net_a>__<net_b>/approve/` | `NetMergeApproveView` | `net_merging_service.apply_decision(net_a, net_b, "approve")` |
| `POST /api/ff_net/merge/candidates/<net_a>__<net_b>/reject/` | `NetMergeRejectView` | `net_merging_service.apply_decision(net_a, net_b, "reject")` |

`_NetMergeDecisionBase._parse_pair` splits the `<net_a>__<net_b>` path
segment and int-coerces both halves (400 on a malformed pair or non-integer
parts) before calling `apply_decision`; its `try/except` translates that
function's `ValueError`/`LookupError` into 400/404 respectively — the two
exception types `apply_decision` deliberately raises for exactly that
purpose (see §2.3 steps 1, 3, 5).

Neither view does any Cassandra I/O itself, constructs a `MatchConfig`/
`MergeConfig` (both services default to `cfg=None` → the algorithm modules'
own defaults), or touches the DataFrame layer — that separation is the whole
point of having a service module in between.
