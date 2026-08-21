# ff_net net-building pipeline — internals reference

Written for debugging in an airgapped environment with no access back to
whoever made these changes. Read top to bottom once; after that, use the
"Debugging" section as a lookup table.

Covers the 2026-08-21 migration from the old `net_id`-only / NET_SUMMARY+
MEMBER model to the current `MY_NET`/`SYSTEM_NET` flat model, and documents
the full pipeline as it now stands.

---

## 1. What changed, and why

**Old model** (before this migration):
- One `net_id` per net.
- A net could contain multiple records: one `NET_SUMMARY` row (latest by
  LTI) plus `MEMBER` rows for other RDFS stations.
- A report could fold into an existing net two ways: `collapsed` (same RDFS,
  same station re-intercepted) or `joined`/`merged` (different RDFS, but
  frequency fell within tolerance of an existing net — this could even
  bridge/merge two previously-separate nets into one).
- `ff_net_building` primary key: `(net_id, row_type, lti, ff_id)`.

**New model** (current):
- Every net has **two** ids, created together: `my_net_id` (`MY_NET`, e.g.
  `00001`) and `net_id` (`SYSTEM_NET`, e.g. `100001`). They are 1:1 in this
  version of the algorithm — every `new` net gets a fresh pair at the same
  time — but they're stored as separate columns so that can change later
  without a schema migration.
- **A net is exactly one record.** There is no more NET_SUMMARY/MEMBER
  split. `ff_net_building` is a flat table: one row per net.
- A report only updates an existing net (`collapsed`) when **both**:
  - same `RDFS` (same intercepting station), **and**
  - either the exact same `FF_ID`, **or** its frequency band overlaps the
    net's full observed band (widened across every sighting, not just the
    latest one — see §3.4).
- Anything else — different RDFS, or same RDFS but no match — always spawns
  a brand-new `my_net_id`/`net_id` pair (`new`). **There is no more
  `joined`/`merged`.** Frequency overlap alone, across different RDFS
  stations, no longer merges nets.
- `ff_net_building` primary key: `net_id` only (single partition key, no
  clustering — there's only ever one row per net now).

**Files touched by this migration:**

| File | What changed |
|---|---|
| `submodules/net_building.py` | Full replacement — new matching/collapsing algorithm, `MY_NET`/`SYSTEM_NET` model. Still storage-agnostic (Excel/CSV CLI path untouched). |
| `submodules/net_building_service.py` | Cassandra glue rewritten: member loading, building-table sync, history append, JSON shaping — all adapted for the flat model. |
| `submodules/ff_net_repository.py` | `FfNetBuildingRepository.primary_key_columns` changed from `("net_id", "row_type", "lti", "ff_id")` to `("net_id",)`. |
| `submodules/cassandra.sql` | `ff_net_building` schema rewritten (dropped `row_type`, added `my_net_id`, `PRIMARY KEY (net_id)`). `ff_net_report_history` gained a `my_net_id` column. |
| `views.py` | Unchanged — still just `GET /api/ff_net/build/` → `net_building_service.run_pipeline()`. |

**Live DB migration applied at the time:** `DROP TABLE ff_net_building`
(old schema, incompatible partition key — Cassandra can't `ALTER` a
partition key) then recreated with the new schema. `ff_net_report_history`
got `ALTER TABLE ... ADD my_net_id int` (additive, no data loss). Per the
design, `ff_net_building` is fully derived from `ff_net_report` +
`ff_net_report_history`, so dropping it is safe — the next `build/` call
repopulates it from scratch.

---

## 2. Data flow, end to end

```
                 ┌─────────────────────┐
                 │  ff_net_report       │   raw intercepts, already populated
                 │  PK: (ff_id, lti)    │   externally (not written by this
                 └──────────┬───────────┘   pipeline)
                             │
                             │ GET /api/ff_net/build/
                             ▼
                 ┌─────────────────────────────┐
                 │ views.NetBuildView.get()     │
                 │  → net_building_service.     │
                 │      run_pipeline()          │
                 └──────────┬───────────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
       ▼                     ▼                     ▼
┌─────────────┐     ┌──────────────────┐   ┌────────────────────┐
│ff_net_building│    │ff_net_report_     │   │ff_net_report        │
│(read: existing│    │history            │   │(read: every raw     │
│ nets so far)  │    │(read: which raw   │   │ report ever taken)  │
└──────┬────────┘    │ reports already   │   └──────────┬──────────┘
       │             │ processed)        │              │
       │             └─────────┬─────────┘              │
       │                       │                        │
       └───────────┬───────────┴────────────────────────┘
                    ▼
     net_building.assign_net() per report
     (pure in-memory DataFrame algorithm, §3)
                    │
       ┌────────────┴─────────────┐
       ▼                          ▼
┌─────────────────┐     ┌──────────────────────┐
│ ff_net_building   │     │ ff_net_report_history │
│ (write: sync the  │     │ (write: append one     │
│ full net table)   │     │ row per newly-        │
│                   │     │ processed report)      │
└───────────────────┘     └────────────────────────┘
                    │
                    ▼
          JSON response (flat list of nets)
```

Everything happens **in one HTTP request** — there is no background job.
`GET /api/ff_net/build/` synchronously: reads all three tables in full,
runs the whole matching algorithm in memory over a pandas DataFrame, writes
back to two tables, and returns the result. There is no pagination on the
reads (`find_all()` — see §5), so this does not scale indefinitely, but for
the current data volumes (hundreds of reports) it's fine.

---

## 3. The matching algorithm (`net_building.py`)

This is the part of the codebase your COMINT domain logic actually lives
in. Everything else is plumbing around it.

### 3.1 Vocabulary

| Term | Meaning |
|---|---|
| `RDFS` | The intercepting station. Two reports can only be considered "the same emitter re-intercepted" if they came from the **same** RDFS. |
| `FF_ID` | A real emitter id, when the source data has one. If two reports share the same RDFS and the same FF_ID, they are *always* the same emitter — no frequency check needed. |
| Band | `[frequency - bandwidth/2, frequency + bandwidth/2]` in MHz, derived from `Frequency_MHz`/`Bandwidth_kHz`. |
| `my_net_id` / `MY_NET` | 5-digit zero-padded id, starts at `00001`. |
| `net_id` / `SYSTEM_NET` | 6-digit id, starts at `100001`. |
| `members` | The in-memory DataFrame of "one row per net so far", used while processing a batch. Columns: whatever the source report columns are (`FF_ID`, `Frequency_MHz`, `RDFS`, `LTI`, ...) plus `my_net_id`, `net_id`, and two hidden columns `_f_lo`/`_f_hi`. |

### 3.2 `MatchConfig` (tunable knobs)

```python
use_band_tol: bool = True        # True: tolerance = BW/2 + BW/2 (+ pad). False: flat freq_tol_mhz window.
freq_tol_mhz: float = 0.4        # only used when use_band_tol=False
band_pad_mhz: float = 0.0        # extra slack added on top of the band-overlap test
freq_tol_override_mhz: Optional[float] = None   # override the freq_pad component
bw_tol_override_mhz: Optional[float] = None     # override each record's own BW/2
use_bw_guard: bool = False       # legacy extra check, off by default (band test already subsumes it)
bw_tol_frac: float = 0.15
match_signal_type: bool = False  # if True, also require Signal_Type to match
```

Default config (`MatchConfig()`) is what `views.py` uses — band-overlap
matching, no padding, no extra guards. If a match/no-match looks wrong in
prod, check whether something is constructing a non-default `MatchConfig`
before assuming the algorithm is broken.

### 3.3 Routing one report — `assign_net(members, new_report, cfg)`

Returns `(my_net_id, net_id, action, survivor_ffid)`, `action ∈ {"collapsed", "new"}`.

```
1. If members has no "RDFS" column, or new_report has no RDFS value:
   → always "new" (can't match without a station).

2. Filter members to same_rdfs = members where RDFS == new_report.RDFS.

3. For each row in same_rdfs, it's a candidate if EITHER:
     a) new_report.FF_ID == row.FF_ID (both present, same value)   -- direct match
     OR
     b) new_report's band overlaps row's FULL OBSERVED band        -- see §3.4
        i.e. (report_lo - pad) <= member_hi  AND  (report_hi + pad) >= member_lo

4. If there are candidates:
     - pick the one with the latest LTI ("newest")
     - collapse_into() merges new_report into that row in place (§3.5)
     - return that row's (my_net_id, net_id), action="collapsed"

5. Otherwise:
     - return (next_my_net_id, next_net_id), action="new"
       next_my_net_id = max(existing my_net_id) + 1, or 1 if none yet
       next_net_id    = max(existing net_id) + 1, or 100001 if none yet
```

**Important:** step 5's two counters are computed independently but always
called together on every `new` action — that's *why* they stay 1:1 in
practice, not because the code enforces it structurally. If you ever see
code that creates a `net_id` without also minting a `my_net_id` (or vice
versa), that's a change to watch for.

### 3.4 Band widening across sightings — `_f_lo` / `_f_hi`

A net's "observed band" is not just its latest report's band — it's the
union of every band it has ever been seen on. Two hidden columns track
this:

- When a net is first created (`action == "new"`), `_f_lo`/`_f_hi` are
  seeded from that first report's band (`_report_band`).
- Every time a report collapses into a net (`collapse_into`), the net's
  `_f_lo`/`_f_hi` are widened to `min(old_lo, new_lo)` / `max(old_hi, new_hi)`.
- Matching (`_member_band` in step 3b above) always checks against this
  widened band, not just the displayed `Frequency_MHz`/`Bandwidth_kHz`
  (which show the *newest* record's values — see §3.5).

**This means:** a net's displayed frequency can look like it "shouldn't"
match a new report by a naive `freq +/- BW/2` check, but still correctly
collapse — because the net's true matching band is wider than what's
displayed. If you're debugging "why did/didn't this collapse", always
recompute from `_f_lo`/`_f_hi` logic, not from the displayed frequency
alone.

**Caveat — this does NOT persist across separate pipeline runs.**
`_f_lo`/`_f_hi` are never written to `ff_net_building` (they're excluded
from `data_cols`, so `build_hierarchical` never emits them, and the
Cassandra sync never stores them). Every time `run_pipeline()` starts
fresh, `load_members()` reloads nets from Cassandra *without* `_f_lo`/
`_f_hi`, so `_member_band()` falls back to the single displayed record's
`freq +/- BW/2` for any net until it gets a *new* collapse within that same
run. **This is a limitation of the algorithm as given, not something this
migration introduced** — the Excel-based CLI path has the identical
limitation (the Nets sheet never stores `_f_lo`/`_f_hi` either). If you
need band-widening to survive across runs, that requires adding
`f_lo`/`f_hi` columns to the `ff_net_building` schema and threading them
through — not currently done.

### 3.5 Folding a report into a net — `collapse_into(members, midx, new_report)`

```
old = members.loc[midx]        # the net being updated

FTI  = min(old.FTI, new_report.FTI)     # earliest wins
LTI  = max(old.LTI, new_report.LTI)     # latest wins
NTI  = old.NTI + new_report.NTI         # SUMMED (see warning below)

if new_report.LTI >= old.LTI (i.e. new_report is the newer sighting):
    every OTHER field (frequency, bandwidth, modulation, branch, ...)
    is overwritten with new_report's value.
    # so the displayed record always reflects the newest sighting,
    # except FTI/LTI/NTI which are computed specially above.

_f_lo, _f_hi = union of old's and new_report's band   (§3.4)

survivor_ffid = the net's FF_ID after the update (for audit trail)
```

**Warning: NTI summing is not idempotent if a report is ever reprocessed.**
The signature-based dedup (§4) is what's supposed to prevent a report from
ever reaching `assign_net`/`collapse_into` more than once. If that dedup
ever fails for a given report (see the note in §6 about the trickle-down
behavior observed right after this migration), that report's NTI will get
added into its net's NTI *again* on every reprocess, silently inflating the
count. If a net's NTI looks implausibly high, check `ff_net_report_history`
for duplicate/near-duplicate entries for the same underlying report
(same `FF_ID`/`FTI`/`LTI`, different `processed_at`).

### 3.6 Per-net RDFS dedup — `dedup_rdfs_per_net`

After the whole batch is processed, for each `net_id`, keep only the row
with the latest `LTI` per `RDFS`. In the current model there is only ever
one live row per net already (one record per net, by construction), so in
practice this is a no-op safety net rather than something that actively
prunes anything — it mattered more under the old MEMBER-rows model.

### 3.7 Emitting the output table — `build_hierarchical(members, data_cols)`

Flat, one row per `(my_net_id, net_id)` pair, in ascending order. Columns:
`["MY_NET", "SYSTEM_NET"] + data_cols`. `MY_NET` is zero-padded to 5 digits
as a **string** (`"00001"`), `SYSTEM_NET` as a plain numeric string
(`"100001"`) — this matters for the Excel CLI path (keeps leading zeros
through a round-trip); the Cassandra glue converts both back to `int`
before writing (`my_net_id`, `net_id` columns).

### 3.8 Idempotency guard — signatures

```python
_SIG_COLS = ("FF_ID", "FTI", "LTI", "NTI", "Frequency_MHz")
```

A report's "identity" for dedup purposes is this 5-tuple, with FTI/LTI
normalized through the date parser (`_to_dt(...).isoformat()`) and numbers
coerced (`int`/`round(float, 6)`). Before processing a report, its
signature is checked against every signature already in
`ff_net_report_history` (`_seen_signatures`); if it matches, the report is
skipped (`action = "skipped"`, never reaches `assign_net`) and does **not**
get re-logged. This is what makes re-running the pipeline with no new
source data a no-op.

---

## 4. Cassandra glue (`net_building_service.py`)

`net_building.py` knows nothing about Cassandra or Django — it only
understands pandas DataFrames using its own column-naming convention
(`FF_ID`, `Frequency_MHz`, `RDFS`, `LTI`, ...). This module is the *only*
place that translates between that convention and Cassandra's lowercase
column names (`ff_id`, `frequency`, `rdfs`, `lti`, ...), and the only place
that talks to the three Cassandra tables.

### 4.1 Column name mapping

```python
CASSANDRA_TO_DF = {
    "ff_id": "FF_ID", "frequency": "Frequency_MHz", "bandwidth": "Bandwidth_kHz",
    "rdfs": "RDFS", "fti": "FTI", "lti": "LTI", "nti": "NTI",
    "modulation": "Modulation", "signal_type": "Signal_Type",
}
DF_TO_CASSANDRA = {v: k for k, v in CASSANDRA_TO_DF.items()}
```

Only these 8 fields need renaming. Everything else (`branch`,
`nationality`, `latitude`, `longitude`, `geo_distance`, `azimuth`,
`elevation`, `snr_db`, `cipher`, `force`, `language`, `net_level`,
`net_type`, and the pipeline's own `my_net_id`/`net_id`) already has the
same spelling on both sides and passes through unchanged.

`fti`/`lti` get special date handling both directions: Cassandra → pandas
via `_parse_ts` (→ `pd.Timestamp`), pandas → Cassandra via `nb._to_dt(v).date()`
(→ Python `date`, since the Cassandra columns are `date` type, not
`timestamp`).

### 4.2 `run_pipeline()` — the orchestration, step by step

```python
def run_pipeline(cfg=None):
    tally = {"new": 0, "collapsed": 0, "skipped": 0}

    building_rows = ff_net_building_repository.find_all()   # everything in ff_net_building
    members, data_cols = load_members(building_rows)         # -> DataFrame in nb's column convention
    prev_log = load_history()                                 # everything in ff_net_report_history
    new_df = fetch_new_reports()                               # everything in ff_net_report

    if new_df.empty and members.empty:
        return {...empty response...}

    if data_cols is None:                     # ff_net_building was empty -> bootstrap
        data_cols = <columns from new_df, minus net_id/my_net_id/MY_NET/SYSTEM_NET/Row_Type>
        members = <empty DataFrame with data_cols + ["my_net_id", "net_id"]>

    seen, seen_cols = nb._seen_signatures(prev_log)

    for each report in new_df:
        if its signature is already in `seen`: tally["skipped"] += 1; continue

        row = {the report's data_cols fields}
        my_net_id, net_id, action, survivor_ffid = nb.assign_net(members, row, cfg)

        if action == "new":
            row["my_net_id"], row["net_id"] = my_net_id, net_id
            seed row["_f_lo"], row["_f_hi"] from nb._report_band(row, cfg)
            append row to `members`
        # if action == "collapsed", nb.assign_net already mutated `members` in place
        # via collapse_into() — nothing more to do here.

        tally[action] += 1
        record an entry for ff_net_report_history (Action, my_net_id, net_id, Collapsed_FF_ID, Was_Collapsed)

    members = nb.dedup_rdfs_per_net(members)
    hier = nb.build_hierarchical(members, data_cols)   # flat table, MY_NET/SYSTEM_NET + data_cols

    _sync_building_table(hier, building_rows)   # diff against what was in Cassandra, upsert/delete
    _append_history(new_history_entries)         # insert only — never touches existing rows

    return {generated_at, processed: tally, nets: _hierarchy_to_json(hier)}
```

### 4.3 Syncing `ff_net_building` — `_sync_building_table`

```python
new_rows = _hier_to_cassandra_rows(hier)          # every net after this run
new_keys = {r["net_id"] for r in new_rows}
old_keys = {r["net_id"] for r in old_building_rows}   # every net before this run

for net_id in old_keys - new_keys:
    DELETE FROM ff_net_building WHERE net_id = ?      # net that no longer exists

for row in new_rows:
    INSERT INTO ff_net_building (...)                  # upsert every current net
```

In the current algorithm, a `net_id`, once created, is **never** removed
from `members` (only updated in place via `collapse_into`) — there's no
merge/delete path anymore. So in practice `old_keys - new_keys` is always
empty; the delete branch is defensive dead code that only matters if you
manually delete rows from `ff_net_report`/`ff_net_report_history` between
runs, or from some future change to the algorithm.

Cassandra `INSERT` is an upsert by primary key, so re-inserting a `net_id`
that already exists just overwrites its row — this is how an existing net
gets its fields updated when a new report collapses into it.

### 4.4 Appending to `ff_net_report_history` — `_append_history`

Pure insert, one row per newly-processed report (never per net — so a net
that has 5 reports folded into it over time has 5 history rows, all
sharing the same `net_id` but different `processed_at`/`ff_id`). Never
updates or deletes existing rows — this table is a permanent audit trail
by design.

### 4.5 JSON shaping — `_hierarchy_to_json`

Straight row-to-dict conversion of the flat `hier` table (one dict per
net), with `fti`/`lti` converted to ISO date strings and `MY_NET`/
`SYSTEM_NET` (zero-padded strings) converted back to `my_net_id`/`net_id`
integers. No more nested `summary`/`emitters` — every net is one flat
object in the `nets` array.

---

## 5. Schema (`cassandra.sql`)

Keyspace: `ans_transformed`, `SimpleStrategy`, RF=3 (single-node dev
cluster — RF=3 without 3 nodes just means writes need care about
consistency level, but doesn't break anything on 1 node at RF being higher
than node count for basic read/write at `ONE`/`LOCAL_QUORUM`... on a truly
single-node cluster with RF=3 configured, be aware `LOCAL_QUORUM`
writes/reads could behave unexpectedly if you ever scale down replicas;
not a concern introduced by this migration, inherited as-is).

```
ff_net_report            -- raw source, NOT written by this pipeline
  PRIMARY KEY ((ff_id), lti)

ff_net_building           -- derived, ONE row per net
  PRIMARY KEY (net_id)             <-- changed: was ((net_id), row_type, lti, ff_id)
  + my_net_id column                <-- new

ff_net_report_history     -- append-only audit trail
  PRIMARY KEY ((net_id), processed_at, ff_id)   <-- unchanged
  + my_net_id column                              <-- new (additive ALTER)
```

**Re-applying the schema file:** only the keyspace line has `IF NOT
EXISTS`. The three `CREATE TABLE` statements do not. Running
`cqlsh -f cassandra.sql` against a keyspace that already has any of these
tables will error on the first one it hits (typically `ff_net_report`,
since it's usually already populated), but `cqlsh` does **not** stop the
whole script on that error — it prints the error and continues with the
remaining statements. That's a property of `cqlsh`'s script-execution
mode, not something this file relies on intentionally — don't assume it
holds for every `cqlsh` invocation method (e.g. this behavior was observed
specifically running `cqlsh -f ...` inside the `cassandra-database` Docker
container; different `cqlsh` versions/flags may behave differently). If
you need a truly idempotent re-apply, add `IF NOT EXISTS` to each
`CREATE TABLE`, or drop the specific table you need to recreate and run
just its statement.

**Rebuilding `ff_net_building` from scratch** (e.g. schema drift, corrupt
data, or picking up an algorithm change that isn't backward-compatible with
existing rows):

```sql
DROP TABLE ans_transformed.ff_net_building;
-- then re-run just that CREATE TABLE statement from cassandra.sql
```

Safe — it's fully derived from `ff_net_report` + `ff_net_report_history`.
The next `GET /api/ff_net/build/` call repopulates it completely. **Do
not** drop `ff_net_report_history` the same way — it's the append-only
audit trail and the only thing making re-runs idempotent; dropping it
means the next `build/` call will reprocess every report in
`ff_net_report` as if it were brand new (re-creating nets, but from a
clean slate — not merged with whatever was there before).

---

## 6. Debugging — quick lookup

**Endpoint 404s (`api/ff_net/build/` not found).**
The Django URLconf changed but the running server process didn't restart.
This app runs on Waitress (`serve.py`), which has **no autoreload** —
unlike `manage.py runserver`. Check what's actually serving:
`docker`/process list for `python serve.py`, confirm it's using an
interpreter that has `waitress` installed (this repo uses a `backend5`
conda env; the bare `python` on PATH may not have it), and restart it.

**Endpoint 500s with `InvalidRequest ... table X does not exist`.**
The Cassandra schema hasn't been applied to whatever cluster
`CASSANDRA_HOSTS` points at. Run `cassandra.sql` (see §5 for the
not-fully-idempotent caveat) against that cluster.

**A report that should collapse into an existing net instead creates a
new one.**
Check, in order:
1. Same `RDFS` on both? Different RDFS *always* means `new` now — there's
   no cross-station frequency-only merge anymore (that's the biggest
   behavioral difference from the old algorithm).
2. If RDFS matches: does the report's `FF_ID` match the net's `FF_ID`
   exactly? If yes, it should always collapse regardless of frequency —
   if it didn't, something is wrong with `FF_ID` type comparison (check
   for `str` vs `int` mismatches; the code tries `int(...) == int(...)`
   first, falls back to `str(...) == str(...)`).
3. If no `FF_ID` match: recompute the frequency band overlap by hand using
   the net's **`_f_lo`/`_f_hi`** if available, not just its displayed
   `Frequency_MHz`/`Bandwidth_kHz` (see §3.4) — but remember `_f_lo`/`_f_hi`
   resets on every fresh pipeline run (doesn't persist to Cassandra), so
   for a net that was built entirely in *previous* runs, its effective
   matching band right now really is just its single displayed record's
   `freq +/- BW/2`. This is expected, not a bug.

**A net's `nti` looks too high.**
Check `ff_net_report_history` for multiple rows with the same underlying
`ff_id`/`fti`/`lti` but different `processed_at` — that means the same
source report was processed more than once, and `collapse_into` summed its
`NTI` in each time (see §3.5 warning). If you find this, the root cause is
the idempotency signature check (§3.8) failing to recognize a report it
had already logged — see the next entry.

**A handful of reports keep getting reprocessed instead of being skipped,
even though they're already in `ff_net_report_history`.**
Observed right after this migration was first applied: a small number of
reports (12, out of 222) were reprocessed on every call for several calls
in a row, with the count shrinking each time (12 → 4 → 2 → 1 → 0) before
settling into full idempotency. Root cause not fully diagnosed — most
likely explanation is stale/inconsistent signature data left over from
testing done with an earlier version of the algorithm/schema, which
resolved itself once those specific reports got a consistent history entry
under the current code. If you see this pattern again (a shrinking but
nonzero `collapsed`/`new` count on repeated calls with no new source data),
call the endpoint a few more times and check whether it converges to
`{"new": 0, "collapsed": 0, "skipped": <total report count>}`. If it does
NOT converge (stays flat or grows), that's a real bug in the signature
logic (§3.8) and needs investigation — start by comparing the raw
`ff_net_report` row for one of the stuck reports against its
`ff_net_report_history` entry field-by-field, particularly `fti`/`lti`
(date parsing) and `nti` (int coercion), since those are the fields most
likely to silently round-trip differently.

**Sanity-check queries** (via `cqlsh` against the target cluster):

```sql
USE ans_transformed;

-- how many nets exist, and their ids
SELECT net_id, my_net_id, ff_id, rdfs, frequency FROM ff_net_building;

-- how many raw reports have never been processed at all
-- (compare this count against ff_net_report_history's distinct ff_id/fti/lti)
SELECT COUNT(*) FROM ff_net_report;
SELECT COUNT(*) FROM ff_net_report_history;

-- history for one specific net, to see its full collapse chain
SELECT * FROM ff_net_report_history WHERE net_id = 100001;
```

`ff_net_building`/`ff_net_report_history` counts don't need to match
`ff_net_report`'s count 1:1 — one raw report becomes one history row, but
many raw reports can collapse into one `ff_net_building` row (a net).

**Full reset for a stuck/corrupt environment:**

```sql
DROP TABLE ans_transformed.ff_net_building;
-- re-run the ff_net_building CREATE TABLE statement from cassandra.sql
```

then `curl http://<host>:8000/api/ff_net/build/` to repopulate it from
`ff_net_report` + whatever `ff_net_report_history` already has (this will
NOT reprocess reports already in the history table — see the caveat in §5
about not dropping the history table if you want a truly from-scratch
rebuild that starts recreating nets for every report again).

---

## 7. Files map

```
apps/ff_net/
├── README.md                          setup/quickstart (start here for running it)
├── PIPELINE_REFERENCE.md              this file — internals, for debugging
├── views.py                           NetBuildView: GET -> run_pipeline()
├── urls.py                            /api/ff_net/build/, /api/ff_net/reports/...
├── repository.py                      (parent app) generic Cassandra CRUD base class
└── submodules/
    ├── net_building.py                 pure algorithm (§3) — DataFrame in, DataFrame out,
    │                                    no Cassandra/Django knowledge. Also has an Excel/CSV
    │                                    CLI entry point (`python -m ...net_building <input>`),
    │                                    a separate code path, untouched by this migration.
    ├── net_building_service.py         Cassandra glue (§4) — the only file that knows both
    │                                    net_building.py's column convention AND Cassandra's.
    ├── ff_net_repository.py            repository classes (table names, primary keys) +
    │                                    the ff_net_report CRUD viewset/serializer
    └── cassandra.sql                   schema (§5) — reference only, not auto-applied
```
