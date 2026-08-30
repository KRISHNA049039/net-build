# net-build

Django REST API backed entirely by Cassandra (no relational DB — Cassandra
is the only datastore, including for Django's own internals). Two app
domains:

- **`catalog`** — CRUD over orders/products/inventory/shipments.
- **`ff_net`** — COMINT FF-net pipeline: folds raw intercept reports into
  "nets" automatically, then a separate human-approved workflow for
  merging nets that different stations independently built for what's
  actually the same tactical net.

## Layout

```
backend/            Django project (the actual API)
  apps/
    core/             health/metrics, shared Cassandra session + repository base
    catalog/           orders/products/inventory/shipments CRUD
    ff_net/             the net-building + net-merging pipeline (see its own docs below)
  serve.py             production-style entrypoint (waitress, no autoreload)
  manage.py            Django management commands / dev server (autoreload)
  .env.template         copy to .env and fill in for your environment

casssndra/            Cassandra cluster setup (Docker Compose)
  docker-compose.djnago.yaml     single-node dev Cassandra + web UI
  docker-compose.cluster.yml     5-node, 2-rack local dev cluster (simulates the
                                    full ring as separate containers on one machine)
  dis/                           multi-node (5-node, 2-rack) LAN cluster + RUNBOOK.md
  cassandra/                     schema.sql / seed.sql / seed_data.py
  scripts/                       repair.sh / checkpoint.sh / consolidate-checkpoint.sh /
                                    verify-restore.sh / create-app-roles.sh
  RECOVERY.md                    node/rack fault tolerance: replication/repair/backup/restore
  DISASTER_RECOVERY.md           whole-cluster loss: checkpoints, watermarks, rebuild runbook
  AUTH.md                        PasswordAuthenticator bootstrap, per-service roles
  AWS_RUNBOOK.md                 validate the cluster on AWS before the airgapped one

microservices/       Same catalog/ff_net domains, split into two independently
                       deployable services + a Kong gateway (see ARCHITECTURE.md)

logging/              Centralized logging (Loki+Promtail+Grafana) for both
                        backend/ and microservices/ -- see CENTRALIZED_LOGGING.md
```

## Running it

**1. Start Cassandra** (single node + web UI, for local dev):

```bash
cd casssndra
docker compose -f docker-compose.djnago.yaml up -d
```

(For a local 5-node ring on one machine instead, use
`docker-compose.cluster.yml`; for the real multi-node LAN setup, see
`casssndra/dis/RUNBOOK.md`.)

**2. Apply schema** — see each app's own docs for its specific tables;
`ff_net`'s is at
[backend/apps/ff_net/submodules/cassandra.sql](backend/apps/ff_net/submodules/cassandra.sql).
Auth is on by default (`cqlsh -u cassandra -p cassandra -f ...` — the
built-in superuser; see [casssndra/AUTH.md](casssndra/AUTH.md) before
this goes anywhere near real traffic).

**3. Configure and run the backend:**

```bash
cd backend
cp .env.template .env    # then edit CASSANDRA_HOSTS, CASSANDRA_USERNAME/PASSWORD etc.
pip install -r requirements.lock.txt

python manage.py runserver     # dev, autoreloads
# or
python serve.py                 # production-style (waitress), no autoreload --
                                  # restart it by hand after code changes
```

**4. API docs (OpenAPI/Swagger):**

```
http://127.0.0.1:8000/api/schema/swagger-ui/
http://127.0.0.1:8000/api/schema/redoc/
```

## ff_net docs

The `ff_net` app has its own set of docs, since it's the most involved
piece:

- [backend/apps/ff_net/README.md](backend/apps/ff_net/README.md) — setup/quickstart
- [backend/apps/ff_net/PIPELINE_REFERENCE.md](backend/apps/ff_net/PIPELINE_REFERENCE.md) — net-building internals
- [backend/apps/ff_net/MERGE_PIPELINE_REFERENCE.md](backend/apps/ff_net/MERGE_PIPELINE_REFERENCE.md) — net-merging internals
- [backend/apps/ff_net/API_CONTRACT.md](backend/apps/ff_net/API_CONTRACT.md) — endpoint request/response reference
- [backend/apps/ff_net/TESTING.md](backend/apps/ff_net/TESTING.md) — how to test it (pytest suite, smoke script, manual walkthrough)

## Testing

```bash
cd backend
python -m pytest apps/ff_net/tests/     # needs Cassandra reachable -- integration tests, not mocked
bash scripts/test_merge_smoke.sh          # curl-only smoke test, no Python deps
```

## Other architectures / cross-cutting docs

- [microservices/](microservices/) — the same two domains as
  independently deployable services + gateway. `backend/` above is
  unaffected either way; run whichever fits what you're testing.
- [logging/CENTRALIZED_LOGGING.md](logging/CENTRALIZED_LOGGING.md) —
  searchable logs across either architecture, plus a recap of Cassandra
  replication/restoration (full detail in `casssndra/RECOVERY.md`).
- [OBSERVABILITY_STACK.md](OBSERVABILITY_STACK.md) — how Kong, Loki,
  Prometheus, and Grafana actually work and interact (push vs. pull,
  what each one stores, end-to-end request trace, verification commands).
- [AIRGAP_SETUP.md](AIRGAP_SETUP.md) — the step-by-step strategy for
  going from a connected machine to running airgapped PCs: every image/
  tool to download, in what order, and where each other doc fits in.
- [AIRGAP_TESTING.md](AIRGAP_TESTING.md) — how to prove this whole stack
  needs zero internet, on a single connected machine, before it goes
  anywhere near the airgapped PCs; plus the pre-transfer checklist
  (images, Python deps, secrets, clock sync).
