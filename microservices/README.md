# net-build-microservices

A microservices extraction of the `backend/` monolith, in its own
directory — `backend/` and `casssndra/` are untouched. Two independently
deployable services, split along the same boundary that already existed
in the monolith's code (`apps/catalog` and `apps/ff_net` never imported
from each other — see the "confirm microservices" discussion that led
here).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how this was built, the
exact service boundary, the interfaces each service exposes, how to wire
up communication between services if you add a third one, and a
line-by-line walkthrough of how `serve.py` boots the WSGI process.

| Service | Extracted from | Port | Keyspace |
|---|---|---|---|
| `catalog-service` | `backend/apps/catalog` + `backend/apps/core` | 8001 | `django_platform` |
| `ff-net-service` | `backend/apps/ff_net` + `backend/apps/core` | 8002 | `ans_transformed` |

`apps/core` (Cassandra session, health/metrics, request logging) is
**duplicated** into each service rather than shared as a library — that's
deliberate, not an oversight. Real microservices don't share a runtime
codebase; if `core` changes, it changes independently in each service
(or you version and publish it as a package later). Sharing it via a
relative import the way the monolith does would just be the monolith
again with extra steps.

## What's genuinely different from the monolith now

- **Separate processes, separate deploys.** Each service has its own
  `manage.py`/`serve.py`/`settings.py`/`.env` and can be built, deployed,
  restarted, and scaled independently of the other.
- **Separate failure domains.** `ff-net-service` crashing doesn't take
  `catalog-service` down with it (in the monolith, one process, one
  crash takes everything).
- **A gateway sits in front** (`gateway/nginx.conf`) so callers hit one
  origin (`/catalog/...`, `/ff_net/...`) instead of knowing two ports.

## What's intentionally NOT split

- **Same physical Cassandra cluster.** Both services talk to the cluster
  built in `../casssndra/` (5 nodes, RF=3 — see `../casssndra/RECOVERY.md`),
  just different keyspaces. Strict "database per service" would mean two
  separate clusters; that's real infrastructure cost for a boundary
  that's already enforced at the keyspace level, so it wasn't done here.
  If you need that isolation later, point one service's `CASSANDRA_HOSTS`
  at a second cluster — nothing else changes.
- **No message queue / event bus.** Neither service currently needs to
  call or notify the other (confirmed: no cross-imports between
  `apps/catalog` and `apps/ff_net` in the original monolith). If that
  changes, add it then — don't build it speculatively now.

## Running it

**Without Docker** (same pattern as the monolith's `backend/`):
```bash
# terminal 1
cd services/catalog-service
cp .env.template .env    # edit CASSANDRA_HOSTS etc.
pip install -r requirements.txt
python serve.py           # :8001

# terminal 2
cd services/ff-net-service
cp .env.template .env
pip install -r requirements.txt
python serve.py           # :8002
```

**With Docker Compose** (services + gateway; Cassandra is separate):
```bash
# 1. bring up Cassandra first (see ../casssndra/README or RUNBOOK)
cd ../casssndra && docker compose -f docker-compose.cluster.yml up -d && cd ../microservices

# 2. fill in each service's .env (CASSANDRA_HOSTS should be the cluster's
#    container names, e.g. cassandra-1,cassandra-2,cassandra-3,cassandra-4,cassandra-5)
cp services/catalog-service/.env.template services/catalog-service/.env
cp services/ff-net-service/.env.template services/ff-net-service/.env

# 3. up
docker compose up -d --build
```

Then:
- `http://localhost:8080/catalog/api/catalog/orders/` (via gateway)
- `http://localhost:8080/ff_net/api/ff_net/reports/` (via gateway)
- `http://localhost:8080/catalog/health/`, `/ff_net/health/`
- Direct, bypassing the gateway: `:8001`, `:8002`
- Swagger: `:8080/catalog/api/schema/swagger-ui/`, `:8080/ff_net/api/schema/swagger-ui/`

## Applying schema

Same schema files as the monolith — nothing new was written, since the
keyspace boundary already matched the service boundary:
```
docker exec -it cassandra-1 cqlsh -f /path/to/casssndra/cassandra/schema.sql       # catalog-service
docker exec -it cassandra-1 cqlsh -f /path/to/backend/apps/ff_net/submodules/cassandra.sql  # ff-net-service
```

## Deploying to the airgapped cluster

Same `../casssndra/dis/` Cassandra cluster and `../casssndra/AWS_RUNBOOK.md`
validation process apply unchanged — this only changes how the Django
*application* is deployed, not the datastore. Build each service's Docker
image, get it onto the airgapped network the same way you'd get any
other artifact there, and run `docker compose up -d` per service (or per
host) pointing `CASSANDRA_HOSTS` at the airgapped ring.
