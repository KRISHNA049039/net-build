# ff_net net-merging pipeline — internals reference

Companion to [PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md) (read that first
for the net-*building* pipeline — this doc assumes you already know what
`ff_net_building`, `my_net_id`/`MY_NET`, and `net_id`/`SYSTEM_NET` are).
See also [API_CONTRACT.md](API_CONTRACT.md) for the endpoint request/
response shapes, and [TESTING.md](TESTING.md) for how to test any of this
yourself (automated pytest suite, curl smoke script, and a manual
curl/cqlsh walkthrough).

Written for debugging in an airgapped environment with no access back to
whoever made these changes.

---

## 1. What this is, and why it's separate from net-building

`net_building.py`/`net_building_service.py` (§ see the other doc) fold raw
intercept reports into nets **fully automatically** — a report either
updates an existing net or spawns a new one, no human involved, every time
`GET /api/ff_net/build/` runs.

Net-*merging* answers a different question: two nets that `net_building`
built **separately** (different `RDFS`, so they were never eligible to
collapse into each other) might still be the *same tactical net*, seen by
two different intercepting stations at the same time and place. Recognizing
that is a judgment call with real consequences (misattributing or
conflating distinct emitters), so **this pipeline never merges anything on
its own.** It only:

1. **Detects** candidate pairs and raises them as alerts.
2. Waits for a **commander's explicit approve or reject.**
3. Only on approval does it actually change data (`my_net_id` on the
   affected `ff_net_building` rows).

This is the direct implementation of the domain requirement: *net merging
is manual and needs command approval; the system's job is to alert, not to
decide.*

**New files, following the same architecture as the build pipeline:**

| File | Role |
|---|---|
| `submodules/net_merging.py` | Pure algorithm (no Cassandra/Django) — the 3-condition match test and candidate search. Mirrors `net_building.py`'s separation of concerns. |
| `submodules/net_merging_service.py` | Cassandra glue — detect/persist/list candidates, apply a commander's decision. Mirrors `net_building_service.py`. |
| `submodules/ff_net_repository.py` | Gained `FfNetMergeCandidatesRepository` / `ff_net_merge_candidates_repository`. |
| `submodules/cassandra.sql` | Gained the `ff_net_merge_candidates` table. |
| `views.py` | Gained `NetMergeCandidatesView`, `NetMergeDecisionView`. |
| `urls.py` | Gained the three `merge/...` routes (§3). |

**Ported from:** the standalone `net_merging_df.py` (Excel CLI, manual
y/N/q confirmation) and `net_merging_json.py` (DataFrame-in/JSON-out,
auto-applies all matches) reference scripts — same 3-condition matching
math, same "survivor = smallest id, absorbed group folds in" semantics.
**Not reused as-is**, because those scripts assume a different storage
shape: a flat Excel sheet where `MY_NET` is filled on a "header" row and
blank on rows that belong to the net above it (so one `MY_NET` can span
several sheet rows). Our Cassandra `ff_net_building` has no such
convention — every net is already exactly one addressable row keyed by
`net_id`, with its own `my_net_id` column always populated. So instead of
blanking/reordering rows, "net A joins net B's group" here is simply: `UPDATE
ff_net_building SET my_net_id = <survivor> WHERE net_id = <A's net_id>`.
Also: the reference scripts read `Latitude`/`Longitude` (capitalized);
`net_merging.py` reads lowercase `latitude`/`longitude` to match what the
existing Cassandra↔DataFrame column mapping (`net_building_service.py`'s
`CASSANDRA_TO_DF`) actually produces.

---

## 2. Data flow, end to end

```
        ┌──────────────────────┐
        │ ff_net_building        │   one row per net, already built by
        │ (read: current nets)   │   the build pipeline
        └───────────┬─────────────┘
                     │
      GET /api/ff_net/merge/candidates/
                     │
                     ▼
        net_merging.propose_pairs()
        (pure in-memory O(n^2) scan, §4)
                     │
       ┌─────────────┴──────────────┐
       │ for each NEW qualifying     │
       │ pair not already recorded: │
       ▼                            │
┌────────────────────────┐          │
│ ff_net_merge_candidates  │◄─────────┘
│ INSERT status='pending'  │
└────────────┬─────────────┘
             │
             │ response: every row still status='pending'
             ▼
     commander reviews the alert list
             │
             │ POST .../<net_a>__<net_b>/approve/  or  .../reject/
             ▼
┌─────────────────────────────────────────┐
│ net_merging_service.apply_decision()      │
│                                            │
│  reject: UPDATE ff_net_merge_candidates    │
│          SET status='rejected'             │
│                                            │
│  approve: SELECT net_id FROM ff_net_building│
│           WHERE my_net_id = <absorbed group>│
│           (ALLOW FILTERING)                 │
│           UPDATE ff_net_building            │
│           SET my_net_id = <survivor>        │
│           WHERE net_id = ?   (one per row)  │
│           UPDATE ff_net_merge_candidates    │
│           SET status='approved'             │
└─────────────────────────────────────────┘
```

Like the build pipeline, everything happens synchronously within one HTTP
request — no background job, no message queue. `ff_net_merge_candidates`
plays the same dual role `ff_net_report_history` plays for the build
pipeline: it's both the commander-facing **alert queue** (`status='pending'`
rows) and the **idempotency ledger** (a pair recorded once, decided or not,
is never re-proposed).

---

## 3. Endpoints

```
GET  /api/ff_net/merge/candidates/
     Detects anything new, returns every still-pending candidate.
     Safe to poll repeatedly (a frontend's alert feed would call this).

POST /api/ff_net/merge/candidates/<net_a>__<net_b>/approve/
POST /api/ff_net/merge/candidates/<net_a>__<net_b>/reject/
     net_a, net_b are net_id (SYSTEM_NET) integers, smaller one first
     (matches the order every response/detection already uses). No request
     body needed -- the decision is in the URL, matching how
     .../reports/<pk>/ identifies a row by composite key in the path
     rather than the body.
```

**Example response from `GET .../candidates/`:**

```json
{
  "generated_at": "2026-08-21T06:16:55.400503Z",
  "newly_detected": 0,
  "pending_count": 1,
  "candidates": [
    {
      "net_a": 100003, "net_b": 100007,
      "my_net_id_a": 3, "my_net_id_b": 7,
      "rdfs_a": "RDFS2", "rdfs_b": "RDFS3",
      "frequency_a": 3805.792, "frequency_b": 3805.795,
      "lti_a": "2024-11-16", "lti_b": "2024-11-16",
      "freq_gap_mhz": 0.003, "freq_tol_mhz": 0.117,
      "lti_gap_sec": 0.0, "lti_tol_sec": 5.0,
      "distance_m": 0.0, "loc_tol_m": 100.0,
      "status": "pending",
      "detected_at": "2026-08-21T06:15:41.249000",
      "decided_at": null
    }
  ]
}
```

**Example response from an approve:**

```json
{
  "net_a": 100003, "net_b": 100007, "decision": "approve",
  "survivor_my_net_id": 3, "absorbed_my_net_id": 7, "nets_moved": 1
}
```

`nets_moved` is how many `ff_net_building` rows had their `my_net_id`
reassigned — every net_id that was in the *absorbed* `my_net_id` group at
the moment of approval, not just `net_b`. See §5 for why.

---

## 4. The matching algorithm (`net_merging.py`)

### 4.1 The three conditions — `same_net(a, b, cfg)`

All three must hold for `(a, b)` to be a merge candidate:

1. **Frequency** — bands overlap:
   `|freq_a - freq_b| <= (bw_a/2 + bw_b/2) + pad` (MHz). Identical math to
   `net_building.py`'s `tune_tol`/`freq_match` (same `MatchConfig`-style
   knobs, just renamed `MergeConfig`).
2. **Time** — `|lti_a - lti_b| <= lti_tol_sec` (default 5 seconds).
3. **Location** — great-circle distance between `(latitude, longitude)` of
   the two nets `<= loc_tol_m` (default 100 metres), via the haversine
   formula.

Any of the three can be disabled via `MergeConfig(use_time_tol=False, ...)`
etc., same pattern as `net_building.py`'s `MatchConfig`. Defaults (what the
endpoints actually use): all three on, `freq` via band-overlap (no flat
window), `lti_tol_sec=5.0`, `loc_tol_m=100.0`.

**This is intentionally a different, looser test than `net_building`'s
RDFS+band collapse.** `net_building` only ever recognizes a re-intercept
from the *same* station in real time. Merging is specifically for
*different*-station corroboration, which is why it additionally checks time
and location — frequency overlap alone across stations is nowhere near
enough evidence, hence why this needs a human to actually approve it.

### 4.2 Candidate search — `propose_pairs(nets, cfg)`

```
for every pair (a, b) of ff_net_building rows, a before b:
    skip if a.my_net_id == b.my_net_id      # already the same group
    if same_net(a, b, cfg):
        (lo, hi) = sorted(a.net_id, b.net_id)
        record (lo, hi, reasons)
```

O(n²) over every net currently in `ff_net_building`. Fine at current data
volumes (hundreds of nets); if this ever needs to scale to tens of
thousands of nets, this is the first thing to revisit (e.g. bucket by
approximate frequency band before the pairwise scan).

`reasons` (`match_reasons`) is the human-readable numbers behind the match
— the actual gap and the tolerance it passed against, for each of the three
conditions — captured into the candidate row precisely so a commander
reviewing the alert can see *why* it was proposed without recomputing
anything.

---

## 5. Cassandra glue (`net_merging_service.py`)

### 5.1 `detect_new_candidates(cfg)`

```python
nets = _fetch_nets()                       # all of ff_net_building, DF convention
seen_pairs = _existing_pairs()             # every (net_a, net_b) already in
                                            # ff_net_merge_candidates, ANY status
for net_a, net_b, reasons in nm.propose_pairs(nets, cfg):
    if (net_a, net_b) in seen_pairs:
        continue                            # already alerted (or decided) -- skip
    INSERT ff_net_merge_candidates (..., status='pending', detected_at=now)
```

Note this checks membership in `ff_net_merge_candidates` **regardless of
status** — a `rejected` pair is just as "seen" as a `pending` or `approved`
one, so a commander's "no, keep these separate" call sticks permanently.
There is currently no way to re-open a rejected pair through the API; doing
so would mean manually deleting/updating its row in Cassandra directly.

### 5.2 `list_pending_candidates(cfg)`

Calls `detect_new_candidates` first (so every call is fully up to date with
the current `ff_net_building` state), then returns every row with
`status == 'pending'`. This is the one function `GET /merge/candidates/`
calls.

**Gotcha already hit once during development:** `ff_net_merge_candidates_
repository.find_all()` goes through `apps.core.execution.rows_to_dicts`,
which already coerces Cassandra `date`/`timestamp` columns to ISO strings
(see `apps/core/execution.py`'s `_coerce`). The first version of this code
called `.isoformat()` on those values again before returning them, which
crashed with `'str' object has no attribute 'isoformat'` — because they
were already strings by the time they left the repository layer. Fixed by
removing the redundant conversion. **If you add a new datetime/date field
to this table, don't re-convert it on the way out** — it's already
JSON-safe coming out of `find_all()`/`get()`. (This is the *opposite*
direction from `net_building_service.py`'s `_parse_ts`, which exists
specifically to turn those same already-stringified values *back* into
`pd.Timestamp` for use inside pandas — that's a different codepath solving
a different problem; don't conflate the two.)

### 5.3 `apply_decision(net_a, net_b, decision)`

```python
net_a, net_b = sorted(...)                     # canonical order
candidate = ff_net_merge_candidates_repository.get({"net_a": net_a, "net_b": net_b})
if candidate is None: raise LookupError(...)    # -> 404
if candidate.status != "pending": raise ValueError(...)   # -> 400

if decision == "reject":
    UPDATE ff_net_merge_candidates SET status='rejected', decided_at=now
    return

# decision == "approve":
row_a = ff_net_building.get(net_id=net_a)
row_b = ff_net_building.get(net_id=net_b)
if either is None: raise LookupError(...)        # -> 404, net got deleted since

if row_a.my_net_id == row_b.my_net_id:
    # already merged via some OTHER approved pair in the meantime
    UPDATE ff_net_merge_candidates SET status='approved', decided_at=now
    return  (nets_moved=0)

survivor, absorbed = sorted(row_a.my_net_id, row_b.my_net_id)
absorbed_rows = SELECT net_id FROM ff_net_building
                WHERE my_net_id = absorbed   -- ALLOW FILTERING
for each row in absorbed_rows:
    UPDATE ff_net_building SET my_net_id = survivor, updated_at = now
    WHERE net_id = row.net_id

UPDATE ff_net_merge_candidates SET status='approved', decided_at=now
```

**Why the whole `my_net_id` group moves, not just `net_b`:** approvals
happen incrementally, one pair at a time, and groups can already be larger
than 2 nets by the time a new pair involving them is approved. Example:

1. Nets 100001, 100002, 100003 all get built separately (`my_net_id` 1, 2, 3).
2. Commander approves (100001, 100002) → both now `my_net_id=1`. (`survivor=1, absorbed=2`)
3. Later, `100002` and `100003` get proposed as a pair (their own
   frequency/time/location happened to match). Commander approves.
   `apply_decision` looks up **current** `my_net_id` for net 100002 (which
   is now `1`, not `2` anymore!) and net 100003 (`3`). `survivor=1,
   absorbed=3`. It moves **every row currently at `my_net_id=3`** — which,
   if nothing else joined that group, is just net 100003 — into
   `my_net_id=1`. Net 100001 (never directly compared to 100003) ends up in
   the same group anyway, correctly, because it's really the transitive
   closure of approved pairs, computed lazily at each approval rather than
   recomputed as connected components up front (the way the reference
   `net_merging_df.py`/`net_merging_json.py` scripts do it in one batch
   with `networkx`). Functionally equivalent end state, computed
   incrementally instead of all at once — which is what "approve pairs one
   at a time over however long it takes a commander to review them"
   requires.

**Consequence to know about:** `survivor = min(my_net_id_a, my_net_id_b)`
is evaluated **fresh** at approval time, not fixed at detection time. If
you're trying to predict which `my_net_id` will "win" ahead of time, always
recompute from `ff_net_building`'s *current* state, not from what the
candidate row's `my_net_id_a`/`my_net_id_b` snapshot says (those are frozen
at detection time, for display/audit purposes only — see next point).

### 5.4 Why `ff_net_merge_candidates` stores a snapshot

`my_net_id_a`/`my_net_id_b`/`rdfs_a`/`rdfs_b`/`frequency_a`/`frequency_b`/
`lti_a`/`lti_b` are captured **at detection time** and never updated
afterward (even after approval changes the live `my_net_id` in
`ff_net_building`). This is deliberate: a commander reviewing a `pending`
alert, or auditing an old `approved`/`rejected` decision later, should see
the data *as it looked when the system raised the alert* — not a
retroactively-updated view that might not match what the reasons
(`freq_gap_mhz` etc.) were computed against. If you need the *current*
`my_net_id` for net_a/net_b, query `ff_net_building` directly by `net_id` —
don't trust the candidate row's snapshot columns for that.

---

## 6. Schema

```sql
CREATE TABLE ff_net_merge_candidates (
    net_a int,
    net_b int,
    status text,             -- 'pending' | 'approved' | 'rejected'
    my_net_id_a int,         -- snapshot at detection time (§5.4)
    my_net_id_b int,
    rdfs_a text,
    rdfs_b text,
    frequency_a double,
    frequency_b double,
    lti_a date,
    lti_b date,
    freq_gap_mhz double,
    freq_tol_mhz double,
    lti_gap_sec double,
    lti_tol_sec double,
    distance_m double,
    loc_tol_m double,
    detected_at timestamp,
    decided_at timestamp,
    PRIMARY KEY (net_a, net_b)
);
```

No changes to `ff_net_building`'s schema were needed — `my_net_id` already
existed as a plain column from the build-pipeline migration (see the other
doc, §1), which is exactly what makes merging here a simple `UPDATE` rather
than a schema change.

**Rebuilding this table from scratch:** safe to `DROP`/recreate, same
caveat as `ff_net_building` — it's derived from `ff_net_building`'s current
state via `propose_pairs`. The one thing you lose on a drop: every past
`rejected` decision. Since rejections are what keeps a pair from being
re-proposed forever, dropping this table means every previously-rejected
pair will alert again on the next `GET /merge/candidates/` call. It does
**not** un-merge anything already `approved` — those changes already live
permanently in `ff_net_building.my_net_id`.

---

## 7. Debugging — quick lookup

**A candidate that looks obviously right isn't showing up as pending.**
Check, in order:
1. Do the two nets already share the same `my_net_id`? `propose_pairs`
   silently skips same-group pairs — check `ff_net_building` directly.
2. Has this exact `(net_a, net_b)` pair already been recorded — approved
   *or rejected* — in `ff_net_merge_candidates`? Once decided, it's never
   re-proposed (§5.1). `SELECT * FROM ff_net_merge_candidates WHERE net_a =
   ? AND net_b = ?` (remember `net_a < net_b`, so try both orders if
   unsure which is which — actually only the sorted order was ever
   written, so use the smaller net_id as `net_a`).
3. Recompute the three conditions by hand from `ff_net_building`'s current
   `frequency`/`bandwidth`/`lti`/`latitude`/`longitude` for both nets
   against the default `MergeConfig` values (§4.1) — most likely culprit is
   the location check: a `NULL` `latitude`/`longitude` on either net makes
   `loc_match` return `False` (`_loc_dist_m` returns `None` on missing
   coordinates, and `None <= loc_tol_m` is never true), which fails the
   whole `same_net` even if frequency and time match perfectly.

**Approving a decision returns 404.**
Either: (a) the pair was never detected/never called `GET
/merge/candidates/` first (nothing to approve — hit the detect endpoint
first), or (b) one of the two `net_id`s no longer exists in
`ff_net_building` (e.g. schema was reset per §5 of the build-pipeline doc
between detection and decision) — `apply_decision` raises `LookupError` ->
`404` specifically distinguishing this from "never was a candidate."

**Approving a decision returns 400 `"was already <status>"`.**
Not a bug — the ledger is doing its job. Check
`ff_net_merge_candidates.status`/`decided_at` for that pair to see what
happened and when. There is no "undo" via the API; reversing an approved
merge means manually `UPDATE ff_net_building SET my_net_id = <old value>
WHERE net_id = ...` for the rows that moved, and manually resetting that
candidate row's `status` back to `pending` (or leaving it `approved` if you
don't want it re-alerted).

**Sanity-check queries:**

```sql
USE ans_transformed;

-- everything still waiting on a commander
SELECT * FROM ff_net_merge_candidates WHERE status = 'pending' ALLOW FILTERING;

-- full decision history for audit
SELECT net_a, net_b, status, detected_at, decided_at FROM ff_net_merge_candidates;

-- which net_ids currently share a my_net_id (i.e. already-merged groups)
SELECT net_id, my_net_id FROM ff_net_building;
-- (group by my_net_id client-side, or per group:)
SELECT net_id FROM ff_net_building WHERE my_net_id = 3 ALLOW FILTERING;
```

---

## 8. Files map (additions only — see the build-pipeline doc for the rest)

```
apps/ff_net/
├── MERGE_PIPELINE_REFERENCE.md         this file
├── views.py                             + NetMergeCandidatesView, NetMergeDecisionView
├── urls.py                              + merge/candidates/... routes
└── submodules/
    ├── net_merging.py                    pure algorithm (§4) -- no Cassandra/Django
    ├── net_merging_service.py            Cassandra glue (§5)
    ├── ff_net_repository.py              + FfNetMergeCandidatesRepository
    └── cassandra.sql                     + ff_net_merge_candidates (§6)
```
