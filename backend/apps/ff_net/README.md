# ff_net — net building pipeline

Folds raw FF intercept reports from Cassandra into "nets" (one record per
net; a report only updates an existing net when it's the same emitter
re-intercepted — same RDFS plus band overlap or a matching FF_ID), stores the
result, and serves it as JSON.

- **Source:** `ans_transformed.ff_net_report` (raw reports, already populated)
- **Output:** `ans_transformed.ff_net_building` (derived — one row per net,
  keyed by `net_id`; rebuilt/synced on every run)
- **History:** `ans_transformed.ff_net_report_history` (append-only audit
  trail of every report ever folded in)
- **Algorithm:** [submodules/net_building.py](submodules/net_building.py)
  (unchanged, format-agnostic) — the frequency/bandwidth matching and
  re-intercept collapsing logic.
- **Cassandra glue:** [submodules/net_building_service.py](submodules/net_building_service.py)
  — reads/writes the tables above and produces the JSON hierarchy.

## Net identity: MY_NET / SYSTEM_NET

Every net gets two ids, created together: `my_net_id` (`MY_NET`, e.g.
`00001`) and `net_id` (`SYSTEM_NET`, e.g. `100001`). A net is **exactly one
record** — distinct emitters are never clubbed together, even when their
frequencies fall within tolerance of each other. A new report only updates
an existing net's record (`collapsed`) when it matches on the same RDFS
*and* either shares the net's `FF_ID` or overlaps its observed frequency
band; otherwise it always spawns a brand-new net (`new`). There is no more
cross-net `joined`/`merged` bridging on frequency alone.

## 1. Start Cassandra

```bash
cd ../../casssndra
docker compose -f docker-compose.cluster.yml up -d
```

Use whichever compose file matches your setup (single-node vs. the
multi-node ones under `casssndra/dis/`).

## 2. Apply the schema

```bash
cqlsh -f apps/ff_net/submodules/cassandra.sql
```

This creates the `ans_transformed` keyspace and all three tables
(`ff_net_report`, `ff_net_building`, `ff_net_report_history`). Only the
keyspace uses `IF NOT EXISTS`; the `CREATE TABLE` statements don't, so
re-running the whole file against an existing keyspace errors on the first
table that already exists (`cqlsh` doesn't stop there, so tables after it
still get created if missing).

If `ff_net_report` doesn't have data yet, insert some via `cqlsh`, e.g.:

```sql
INSERT INTO ans_transformed.ff_net_report
  (ff_id, frequency, bandwidth, rdfs, fti, lti, nti, modulation, signal_type)
VALUES
  (101, 245.500, 12.5, 'RDFS1', '2026-08-10', '2026-08-10', 3, 'AM', 'voice');
```

## 3. Configure environment

Copy `.env.template` to `.env` (from the `backend/` directory) and point it
at your Cassandra cluster:

```
CASSANDRA_HOSTS=127.0.0.1
CASSANDRA_PORT=9042
CASSANDRA_LOCAL_DC=datacenter-1
```

`CASSANDRA_KEYSPACE` doesn't need to be `ans_transformed` — every ff_net
query is keyspace-qualified in the CQL itself.

## 4. Install dependencies and run

```bash
cd backend
pip install -r requirements.lock.txt

# dev server
python manage.py runserver

# or the production-style waitress entrypoint
python serve.py
```

## 5. Trigger the pipeline

```bash
curl http://127.0.0.1:8000/api/ff_net/build/
```

Every call re-reads all of `ff_net_report`, skips reports it has already
folded in before (tracked via `ff_net_report_history`), and returns:

```json
{
  "generated_at": "2026-08-14T09:00:00Z",
  "processed": {"new": 1, "collapsed": 0, "skipped": 4},
  "nets": [
    {
      "my_net_id": 1,
      "net_id": 100001,
      "ff_id": 101,
      "frequency": 245.5,
      "rdfs": "RDFS1",
      "lti": "2026-08-10",
      "...": "..."
    }
  ]
}
```

`nets` is flat — one entry per net (`ff_net_building` row). A report only
folds into an existing net (`collapsed`) when it's the same emitter
re-intercepted (same RDFS, plus matching `FF_ID` or overlapping band);
anything else spawns a brand-new `my_net_id`/`net_id` pair (`new`).

Re-running with no new rows in `ff_net_report` returns the same nets with
`processed` showing only `skipped` counts — it's idempotent, `ff_net_building`
and `ff_net_report_history` are left unchanged.

## 5b. Net-to-net merging (manual, needs command approval)

`build/` never merges nets across different RDFS stations, even if two nets
are clearly the same tactical net seen by two different intercept sites —
that call needs a human. See
[MERGE_PIPELINE_REFERENCE.md](MERGE_PIPELINE_REFERENCE.md) for the full
internals; short version:

```bash
# 1. list merge candidates (also detects any new ones) -- poll this for alerts
curl http://127.0.0.1:8000/api/ff_net/merge/candidates/

# 2. a commander approves or rejects one, by its two net_ids (smaller first)
curl -X POST http://127.0.0.1:8000/api/ff_net/merge/candidates/100003__100007/approve/
curl -X POST http://127.0.0.1:8000/api/ff_net/merge/candidates/100003__100007/reject/
```

Approving folds the higher `my_net_id` group into the lower one in
`ff_net_building` (no rows deleted, `net_id`s untouched); rejecting just
records the decision so that pair is never proposed again.

**Frontend contract:** see [API_CONTRACT.md](API_CONTRACT.md) for a quick
reference, or the generated OpenAPI spec / Swagger UI at
`/api/schema/swagger-ui/` for the authoritative one.

**Testing:** see [TESTING.md](TESTING.md) — `python -m pytest apps/ff_net/tests/`
(real Cassandra required), `bash scripts/test_merge_smoke.sh` for a
curl-only smoke test, or a fully manual curl/cqlsh walkthrough.

### Other endpoints

Plain CRUD over the raw source table (mirrors the `catalog` app's pattern):

```
GET    /api/ff_net/reports/               list (paginated: ?page_size=&cursor=)
POST   /api/ff_net/reports/               create
GET    /api/ff_net/reports/<ff_id>__<lti>/    retrieve
PATCH  /api/ff_net/reports/<ff_id>__<lti>/    update
DELETE /api/ff_net/reports/<ff_id>__<lti>/    delete
```

## 6. Verify end-to-end

1. `cqlsh` → confirm `ff_net_building` rows match the JSON response, and
   `ff_net_report_history` has one row per source report with a plausible
   `action` (`new`/`collapsed`).
2. Insert a report on the same RDFS with a matching `FF_ID` or an
   overlapping frequency band, re-`curl` the build endpoint → it should fold
   into the existing net (`collapsed`, same `my_net_id`/`net_id`) instead of
   creating a new one, and exactly one new row should appear in
   `ff_net_report_history`.
3. Insert an exact duplicate of an already-ingested report, re-`curl` →
   `processed.skipped` increments and nothing else changes (idempotency
   guard from `net_building._report_signature`).

## Notes

- `ff_net_building` is fully derivable from `ff_net_report` +
  `ff_net_report_history` — safe to drop and let the next `build/` call
  repopulate it.
- `ff_net_report_history` is append-only by design (that's the "historic net
  report" table) — nothing in this pipeline ever deletes or rewrites it.
- The original Excel-based CLI (`python -m apps.ff_net.submodules.net_building <input>`)
  still works unchanged for offline/manual use — it's a separate code path
  from the Cassandra pipeline described here.
