# ff_net API contract — quick reference

**Source of truth is the generated OpenAPI spec, not this file.** This is a
human-readable summary for quick reading; if the two ever disagree, trust
the generated spec — it's produced directly from the `@extend_schema`
annotations in `views.py` / `apps/ff_net/serializers.py`, this file is
hand-maintained and can drift.

```bash
# interactive docs (try-it-out UI)
http://<host>:8000/api/schema/swagger-ui/
http://<host>:8000/api/schema/redoc/

# raw OpenAPI 3.0 spec (YAML) -- import into Postman/Insomnia/codegen tools
curl http://<host>:8000/api/schema/ -o ff_net_openapi.yaml

# or generate it offline without a running server
python manage.py spectacular --file ff_net_openapi.yaml
```

Every endpoint below is `Content-Type: application/json`, no auth (see
`REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]` in settings — this repo
has none configured yet).

---

## `GET /api/ff_net/build/`

Runs the net-building pipeline. See [PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md).

**200:**
```json
{
  "generated_at": "2026-08-21T04:32:59.888884Z",
  "processed": {"new": 0, "collapsed": 12, "skipped": 210},
  "nets": [
    {"my_net_id": 1, "net_id": 100001, "ff_id": 56829, "frequency": 1311.616,
     "bandwidth": 31.588, "rdfs": "RDFS1", "fti": "2024-06-09", "lti": "2024-06-09",
     "...": "...many more domain fields, see PIPELINE_REFERENCE.md §3.3"}
  ]
}
```

**503:** `{"detail": "Cassandra is not available"}`

---

## `GET /api/ff_net/merge/candidates/`

The commander alert feed — poll this. Detects anything new every call. See
[MERGE_PIPELINE_REFERENCE.md](MERGE_PIPELINE_REFERENCE.md).

**200:**
```json
{
  "generated_at": "2026-08-21T06:16:55.400503Z",
  "newly_detected": 0,
  "pending_count": 1,
  "candidates": [
    {
      "net_a": 100003, "net_b": 100007,
      "status": "pending",
      "my_net_id_a": 3, "my_net_id_b": 7,
      "rdfs_a": "RDFS2", "rdfs_b": "RDFS3",
      "frequency_a": 3805.792, "frequency_b": 3805.795,
      "lti_a": "2024-11-16", "lti_b": "2024-11-16",
      "freq_gap_mhz": 0.003, "freq_tol_mhz": 0.117,
      "lti_gap_sec": 0.0, "lti_tol_sec": 5.0,
      "distance_m": 0.0, "loc_tol_m": 100.0,
      "detected_at": "2026-08-21T06:15:41.249000",
      "decided_at": null
    }
  ]
}
```

`candidates` only ever contains `status: "pending"` rows — approved/
rejected ones simply stop appearing here (query Cassandra directly for the
full history, see MERGE_PIPELINE_REFERENCE.md §7).

**503:** `{"detail": "Cassandra is not available"}`

---

## `POST /api/ff_net/merge/candidates/{net_a}__{net_b}/approve/`
## `POST /api/ff_net/merge/candidates/{net_a}__{net_b}/reject/`

No request body. `{net_a}__{net_b}` are the two `net_id` integers from a
candidate row above, smaller first, joined by `__` — e.g.
`/api/ff_net/merge/candidates/100003__100007/approve/`.

**200 (approve):**
```json
{"net_a": 100003, "net_b": 100007, "decision": "approve",
 "survivor_my_net_id": 3, "absorbed_my_net_id": 7, "nets_moved": 1}
```

**200 (reject):**
```json
{"net_a": 100003, "net_b": 100007, "decision": "reject"}
```

**400** — pair already decided (approved or rejected):
```json
{"detail": "(100003, 100007) was already approved"}
```

**404** — pair was never proposed (never call approve/reject on a pair the
`GET /merge/candidates/` response didn't actually list):
```json
{"detail": "no merge candidate recorded for (100003, 100007)"}
```

**503:** `{"detail": "Cassandra is not available"}`

---

## Suggested frontend polling pattern

```
every N seconds:
    GET /api/ff_net/merge/candidates/
    diff response.candidates against what's currently shown
    → new pending rows: raise an alert/toast for the commander
    → rows that disappeared since last poll: they were decided elsewhere
      (another operator, or via direct DB access) -- just drop them from
      the UI, no separate "who decided it" info is exposed by this API
```

There is no push/websocket channel — this is a plain polling API, matching
every other endpoint in this backend (see `serve.py`'s single-process
Waitress setup; no async/streaming infrastructure exists here).

---

## Testing this contract

See [TESTING.md](TESTING.md) for the full guide (automated pytest suite,
curl smoke script, and a fully manual curl/cqlsh walkthrough with expected
output at each step). Quick version:

```bash
python -m pytest apps/ff_net/tests/          # automated, asserts exact values
bash scripts/test_merge_smoke.sh              # curl-only, no Python deps
```

Both need `serve.py` (or `manage.py runserver`) running and Cassandra
reachable first.
