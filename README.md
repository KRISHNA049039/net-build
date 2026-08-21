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
  docker-compose.cluster.yml    single-node dev cluster
  dis/                           multi-node (2-PC, 5-node) LAN cluster + RUNBOOK.md
  cassandra/                     schema.sql / seed.sql / seed_data.py
```

## Running it

**1. Start Cassandra** (single-node dev cluster):

```bash
cd casssndra
docker compose -f docker-compose.cluster.yml up -d
```

(For the multi-node LAN setup instead, see `casssndra/dis/RUNBOOK.md`.)

**2. Apply schema** — see each app's own docs for its specific tables;
`ff_net`'s is at
[backend/apps/ff_net/submodules/cassandra.sql](backend/apps/ff_net/submodules/cassandra.sql).

**3. Configure and run the backend:**

```bash
cd backend
cp .env.template .env    # then edit CASSANDRA_HOSTS etc. for your setup
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
