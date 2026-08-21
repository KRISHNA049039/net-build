# Testing net-merging

How to verify the merge alert/approve/reject workflow (see
[MERGE_PIPELINE_REFERENCE.md](MERGE_PIPELINE_REFERENCE.md) for how it
works, [API_CONTRACT.md](API_CONTRACT.md) for the endpoint shapes). Three
ways to test it, in order of how much you trust vs. want to understand:

| Method | Use when |
|---|---|
| §1 `pytest` suite | Normal development — fast, automated, asserts exact values. |
| §2 `scripts/test_merge_smoke.sh` | No Python dev deps installed, or sanity-checking a real deployment. |
| §3 Manual curl/cqlsh walkthrough | You don't trust either script yet, or you're debugging a failure and need to see every step by hand. |

**Prerequisite for all three:** Cassandra reachable (`docker ps` shows
`cassandra-database` healthy) and the app server running
(`python serve.py` from `backend/`, or `manage.py runserver`). Everything
below assumes `http://127.0.0.1:8000` and Cassandra at `127.0.0.1:9042`
(this repo's defaults — see `CASSANDRA_HOSTS`/`SERVE_HOST`/`SERVE_PORT` env
vars to override).

---

## 1. Automated: `pytest`

```bash
cd backend
python -m pytest apps/ff_net/tests/test_net_merging.py -v
```

**These are integration tests against your real Cassandra cluster — not
mocked.** There's no SQL DB in this app for Django's usual test-database
isolation (`DATABASES["default"]["ENGINE"]` is `django.db.backends.dummy`),
so `apps/ff_net/tests/conftest.py` gives tests their own isolation instead:
a `cassandra_ready` fixture that `pytest.skip()`s the whole session if
Cassandra isn't reachable, and factory fixtures that insert rows with a
reserved `net_id` range and delete them in teardown regardless of whether
the test passed or failed.

### What each test actually checks

| Test | Verifies |
|---|---|
| `test_candidates_detects_matching_pair` | Two nets, different `my_net_id`, identical freq/time/location → shows up as `pending` with `freq_gap_mhz`/`lti_gap_sec`/`distance_m` all `0.0`. |
| `test_candidates_skips_same_my_net_id_group` | Two nets already sharing a `my_net_id` are never proposed, even if they'd otherwise match. |
| `test_candidates_ignores_pair_outside_all_tolerances` | A frequency 4000 MHz apart (`1000.0` vs `5000.0`) never matches. |
| `test_candidates_is_idempotent` | Calling the endpoint twice doesn't change `detected_at` on an already-pending pair. |
| `test_approve_merges_group_and_records_decision` | Approving updates `ff_net_building.my_net_id` on the absorbed net, marks the candidate `approved`, and the pair disappears from the pending list. |
| `test_approve_already_decided_returns_400` | Approving the same pair twice → `400` with `"already"` in the message. |
| `test_approve_nonexistent_pair_returns_404` | Approving a pair that was never detected → `404`. |
| `test_reject_marks_rejected_and_is_never_reproposed` | Rejecting does **not** touch `ff_net_building`, and the pair never comes back as pending. |
| `test_incremental_transitive_merge` | Approve A↔B, then B↔C — all three end up under one `my_net_id`, even though A↔C was never directly decided (see MERGE_PIPELINE_REFERENCE.md §5.3 for why). |

### Running a subset

```bash
# one test
python -m pytest apps/ff_net/tests/test_net_merging.py::test_incremental_transitive_merge -v

# everything in ff_net (once other test files exist alongside this one)
python -m pytest apps/ff_net/tests/ -v
```

### Adding a new test

Use the `make_net` and `cleanup_candidates` fixtures from `conftest.py`:

```python
def test_something_new(make_net, cleanup_candidates):
    make_net(999201, my_net_id=201)             # override any field as a kwarg:
    make_net(999202, my_net_id=202, frequency=2000.0, latitude=5.0)
    cleanup_candidates(999201, 999202)            # registers cleanup even if asserts fail

    resp = client.get(candidates_url())
    ...
```

`make_net`'s defaults make every net mutually match every other net it
creates (same frequency/time/location) unless you override a field to push
two apart — that's what `test_candidates_ignores_pair_outside_all_tolerances`
does. `make_net` cleans up every `net_id` it created; `cleanup_candidates`
separately cleans up `ff_net_merge_candidates` rows (it doesn't know which
pairs your test will end up deciding, so you register them explicitly).

**Pick a `net_id` range nobody else is using** — see §4 below for the
reserved ranges. `pytest`'s own tests currently occupy `999101`–`999117`.

---

## 2. Smoke test: `scripts/test_merge_smoke.sh`

```bash
cd backend
bash scripts/test_merge_smoke.sh
```

Plain bash + `curl` + `docker exec cqlsh` — no Python dependencies beyond
what's already needed to run the server. Seeds its own nets
(`net_id` 999201–999206), runs through detect → approve → error cases →
reject, prints `PASS`/`FAIL` per check, and cleans up via a `trap` on exit
(so even a failed run doesn't leave data behind — verified by running it
twice in a row during development, see git history / session notes).

Exits non-zero if anything failed (`[ "$FAIL" -eq 0 ]` as the last line),
so it's usable as a CI step or a pre-deploy gate:

```bash
bash scripts/test_merge_smoke.sh && echo "merge workflow OK" || echo "merge workflow BROKEN"
```

Override the target if testing a non-default setup:

```bash
BASE_URL=http://192.168.1.50:8000 CASSANDRA_CONTAINER=my-cassandra bash scripts/test_merge_smoke.sh
```

If you see `docker: command not found` or a connection error, the script
assumes Cassandra is reachable via `docker exec <container> cqlsh` — if
your Cassandra isn't in Docker, run the `cql()` function's statements by
hand against your `cqlsh` instead (every statement it runs is plain CQL,
nothing Docker-specific about the CQL itself).

---

## 3. Manual walkthrough (curl + cqlsh)

Useful when you don't trust the scripts yet, or you're debugging a
specific failure and need to see the raw state at each step. Uses
`net_id` 999301/999302 (pick your own from the unclaimed part of §4's
range if these are in use).

**Seed two nets that should match** (same frequency/time/location, so all
three merge conditions pass):

```bash
docker exec -i cassandra-database cqlsh -e "
USE ans_transformed;
INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti)
VALUES (999301, 301, 1000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS1', 1);
INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti)
VALUES (999302, 302, 1000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS2', 1);
"
```

**Detect and confirm it shows up as pending:**

```bash
curl -s http://127.0.0.1:8000/api/ff_net/merge/candidates/ | python -m json.tool
```

Look for `"net_a": 999301, "net_b": 999302, "status": "pending"` in the
`candidates` array, with `freq_gap_mhz`/`lti_gap_sec`/`distance_m` all
`0.0` (identical inputs).

**Confirm calling it again doesn't re-detect it** (`newly_detected` should
be `0` if this is the only pending pair in the whole table — otherwise
just check this pair's `detected_at` is unchanged from the first call):

```bash
curl -s http://127.0.0.1:8000/api/ff_net/merge/candidates/ | python -m json.tool
```

**Approve it:**

```bash
curl -s -X POST http://127.0.0.1:8000/api/ff_net/merge/candidates/999301__999302/approve/
# -> {"net_a":999301,"net_b":999302,"decision":"approve","survivor_my_net_id":301,"absorbed_my_net_id":302,"nets_moved":1}
```

**Verify the merge actually happened in Cassandra:**

```bash
docker exec -i cassandra-database cqlsh -e "
USE ans_transformed;
SELECT net_id, my_net_id FROM ff_net_building WHERE net_id IN (999301, 999302);
"
# both rows should now show my_net_id = 301
```

**Confirm it's gone from the pending list, and re-approving 400s:**

```bash
curl -s http://127.0.0.1:8000/api/ff_net/merge/candidates/ | grep 999301   # nothing
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  http://127.0.0.1:8000/api/ff_net/merge/candidates/999301__999302/approve/   # 400
```

**Clean up:**

```bash
docker exec -i cassandra-database cqlsh -e "
USE ans_transformed;
DELETE FROM ff_net_building WHERE net_id = 999301;
DELETE FROM ff_net_building WHERE net_id = 999302;
DELETE FROM ff_net_merge_candidates WHERE net_a = 999301 AND net_b = 999302;
"
```

To exercise the **reject** path instead of approve, swap the approve call
for `.../reject/`, then verify `my_net_id` on both rows is **unchanged**
(301 and 302, not merged) and the pair still never reappears as pending.

To test the **404** and **400** cases without seeding anything:

```bash
# never-proposed pair -> 404
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  http://127.0.0.1:8000/api/ff_net/merge/candidates/1__2/approve/

# malformed pair -> 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  http://127.0.0.1:8000/api/ff_net/merge/candidates/not-a-number/approve/
```

---

## 4. Reserved `net_id` ranges (avoid collisions between test methods)

| Range | Owner |
|---|---|
| `100001`+ | Real data — built by `GET /build/` from `ff_net_report` (`SYSTEM_NET_START` in `net_building.py`). Never touch these in a test. |
| `999101`–`999117` | `apps/ff_net/tests/test_net_merging.py` (pytest) |
| `999201`–`999206` | `scripts/test_merge_smoke.sh` |
| `999301`+ | Free for manual/ad-hoc testing (§3 above uses `999301`/`999302`) |

If you add more pytest cases or extend the smoke script, claim the next
unused numbers within your section rather than reusing another method's
range — otherwise a test run and the smoke script running around the same
time (or a crashed test that skipped cleanup) can stomp on each other's
data.

---

## Troubleshooting a failing test run

**Whole pytest session shows 1 skipped, nothing else ran.**
The `cassandra_ready` fixture couldn't reach Cassandra — check
`docker ps` for `cassandra-database` being `healthy`, and that
`CASSANDRA_HOSTS`/`CASSANDRA_PORT` (env or `.env`) point at it.

**A test fails with leftover data from a previous run** (e.g. "already
approved" on a pair a fresh test expects to be undecided).
A previous run crashed before its fixture teardown ran (a hard interrupt,
not a normal assertion failure — those still run teardown). Manually
delete the stale rows:

```sql
SELECT * FROM ff_net_merge_candidates WHERE net_a = <id> ALLOW FILTERING;
DELETE FROM ff_net_merge_candidates WHERE net_a = <id> AND net_b = <id>;
DELETE FROM ff_net_building WHERE net_id = <id>;
```

**Smoke script fails with a stray pair from an earlier run** — this
happened once during development (the cleanup step referenced the wrong
`net_id` pair, `999203/999204` instead of the actual `999204/999205`,
so a decided candidate row from a previous run survived and made the next
run's reject call 400 instead of 200; fixed in the script now, but if you
edit `test_merge_smoke.sh` and add a new seeded pair, **double check the
cleanup section references the exact same pair** — nothing enforces they
match).

**A merge test asserts a specific `nets_moved` / `survivor_my_net_id`
value and it's wrong.**
Re-read MERGE_PIPELINE_REFERENCE.md §5.3 — `survivor`/`absorbed` are
computed from `my_net_id`'s **current** value at approval time, not what a
candidate row's snapshot (`my_net_id_a`/`my_net_id_b`) says. If a previous
approval already moved one of the two nets into a different group, the
numbers you'd naively expect from the candidate listing won't match what
approving it actually does.
