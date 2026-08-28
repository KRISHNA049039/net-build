# Microservices architecture

Companion to [`README.md`](README.md) (what/why + run instructions). This
doc is the "how it actually works" reference: how the services were
built, where the boundary is and why, what interface each service
exposes, how to wire up communication if you add a third service, and
exactly how the WSGI process starts up.

```
                              clients
                                 |
                                 v
                     +-----------------------+
                     |   gateway (nginx)      |   :8080, public entry point
                     |   /catalog/*  -------- +---+
                     |   /ff_net/*   -------- +-+ |
                     +-----------------------+ | |
                                                | |
                     +--------------------------+ |
                     |                            |
                     v                            v
        +----------------------+       +----------------------+
        |   catalog-service     |       |   ff-net-service      |
        |   Django+DRF  :8001   |       |   Django+DRF  :8002   |
        |   apps/core (own copy)|       |   apps/core (own copy)|
        |   apps/catalog        |       |   apps/ff_net         |
        +-----------+-----------+       +-----------+-----------+
                    |                               |
                    |     backend-network (Docker)  |
                    +---------------+----------------+
                                    v
        +------------------------------------------------------+
        |          Cassandra 5-node cluster, RF=3               |
        |  keyspace django_platform   |  keyspace ans_transformed|
        +------------------------------------------------------+
```

---

## 1. How it's built

Each service is a **complete extraction**, not a shared codebase with
feature flags. It was built by literally copying the relevant Django app
directories out of the monolith (`backend/apps/catalog`,
`backend/apps/ff_net`, and `backend/apps/core` into *both*, since both
domains depend on it) into their own project skeleton, then giving each
one its own settings/urls/wsgi instead of sharing `backend/backend/*`.

```
services/<name>-service/
  <name>_service/          # the Django "project" package (was `backend/backend/`)
    settings.py              # own SECRET_KEY, own CASSANDRA config, own INSTALLED_APPS
    urls.py                  # only mounts this service's own app(s)
    wsgi.py
  apps/
    core/                     # copy of apps/core -- Cassandra session, health, metrics,
                                # request logging. Duplicated on purpose, see §2.
    <domain>/                 # this service's actual business logic, copied as-is
                                # (repository.py, views.py, urls.py, submodules/...)
  manage.py                  # dev server / management commands
  serve.py                   # production entrypoint (waitress) -- see §6
  requirements.txt           # portable pip deps (NOT backend/requirements.lock.txt,
                                # which is a conda lockfile pinned to local build-cache
                                # paths and won't install anywhere else)
  Dockerfile
  .env.template
  pytest.ini
```

Nothing in `apps/<domain>` was rewritten — the view/repository/serializer
code is byte-for-byte what ran in the monolith. Only the four files that
define *how the process boots* (`settings.py`, `urls.py`, `wsgi.py`,
`serve.py`) are new, because each service now needs to boot on its own.

The **gateway** (`gateway/nginx.conf`) and **`docker-compose.yml`** are
the only genuinely new infrastructure — the monolith had neither, since
one process on one port didn't need routing.

---

## 2. Service boundary

The boundary was drawn where the monolith's code *already* had one, not
along an idealized "domain-driven design" line invented for this
exercise: `apps/catalog` and `apps/ff_net` never imported from each
other in the original codebase (verified — the only cross-app import
anywhere was both of them importing `apps.core`). That's what made a
clean 2-way split possible without writing a single line of
inter-service glue.

**What's separate per service (the actual boundary):**
- Process — own `serve.py`, own port, own container.
- Django settings — own `SECRET_KEY`, own `CORS_ALLOWED_ORIGINS`, own
  `INSTALLED_APPS` (catalog-service doesn't even have `apps.ff_net`
  installed, and vice versa — they're not just *unused*, they're not
  present).
- Data — own Cassandra keyspace (`django_platform` vs `ans_transformed`).
  This is the real ownership boundary: catalog-service is the only thing
  with `CASSANDRA_KEYSPACE=django_platform` in its env, so it's the only
  thing that can honestly claim to own that data.
- Deployment — own Dockerfile, own image, scales independently.

**What's intentionally shared (infrastructure, not code):**
- The physical Cassandra cluster (`../casssndra/`). Two keyspaces on one
  cluster, not two clusters. See `../casssndra/RECOVERY.md` for what that
  cluster's replication/recovery guarantees are — both services inherit
  them identically.
- The Docker network (`backend-network`) both services and the Cassandra
  nodes attach to.

**What's duplicated, not shared, on purpose:**
`apps/core` exists twice, once per service, as plain copied files — not
a shared pip package, not a git submodule, not a symlink. This is a
deliberate microservices tradeoff, not laziness:
- A shared *runtime* import (`from apps.core import ...` resolving to
  one physical file both services load) would silently re-couple the two
  services' deploys — you couldn't change `cassandra_session.py` for one
  without redeploying both, which is exactly the coupling splitting them
  was supposed to remove.
- The alternative that keeps them decoupled *and* avoids copy-paste is
  publishing `apps/core` as its own **versioned** package (internal PyPI
  index, or even a private git repo pinned by commit/tag in each
  service's `requirements.txt`). Worth doing once you have a 3rd or 4th
  service and `apps/core` starts drifting between copies in ways that
  matter. Not worth the infrastructure for 2 services today — see §5 for
  the same build-vs-buy judgment call applied to messaging.

---

## 3. Interfaces used

Every service exposes the same shape of interface — this consistency is
itself part of the contract, so a new service should match it:

| Endpoint | Purpose | Defined in |
|---|---|---|
| `GET /health/` | Liveness + Cassandra readiness (200 `ok` / 503 `degraded`) | `apps/core/health.py` |
| `GET /metrics/` | Prometheus exposition format (`http_requests_total`, `http_request_duration_seconds`, `cassandra_up`) | `apps/core/metrics_view.py` |
| `GET /api/schema/` | Raw OpenAPI 3 schema (JSON) | drf-spectacular, per-service `urls.py` |
| `GET /api/schema/swagger-ui/`, `/redoc/` | Human-browsable API docs | same |
| `GET/POST/... /api/<domain>/...` | The actual business API (DRF `ViewSet`s via `DefaultRouter`) | `apps/<domain>/urls.py` |

Everything is **HTTP/JSON via Django REST Framework** — no gRPC, no
GraphQL. `drf_spectacular`'s generated OpenAPI schema at
`/api/schema/` *is* the formal interface contract for each service: if
you're calling another service (§5) or handing this API to a frontend
team, that schema is the thing to version and diff, not the Python code.

The **gateway convention** is: `/<service-name>/<service's own native
path>`. The gateway does not rewrite paths inside a service, it only
prefixes-and-forwards (see `gateway/nginx.conf`) — so
`/catalog/api/catalog/orders/` on the gateway is exactly
`/api/catalog/orders/` on catalog-service, verbatim. This matters for
consistency: a new service's Swagger UI, health check, and API all show
up at predictable gateway paths without the service needing to know
anything about the gateway.

---

## 4. Interfaces NOT used (currently)

- No message broker (Kafka/RabbitMQ/Redis Streams) is deployed.
- No service mesh, no gRPC, no shared event schema registry.
- No API gateway auth/rate-limiting (nginx here is a plain reverse
  proxy — see "Not done here" in §6 before adding a 3rd service if you
  need auth).

These are absent because nothing in the current 2-service system needs
them (§2 — zero cross-calls today). Don't add them speculatively; add
them when a real cross-service need shows up (§5 tells you which one to
reach for when that happens).

---

## 5. How services would communicate (if you add one that needs to)

Today, **catalog-service and ff-net-service never call each other** —
that's not a limitation, it's the reason the split was low-risk. The
moment a new service needs data or an action from an existing one, you
have to choose a communication style. Choose per-interaction, not
once-globally:

### 5a. Synchronous request/response (default choice)

Use plain HTTP, service-to-service, **over the internal Docker network,
not through the public gateway**:

```python
# inside, say, a new "fulfillment-service" that needs catalog data
import requests

resp = requests.get(
    "http://catalog-service:8001/api/catalog/orders/42/",
    timeout=3,
)
resp.raise_for_status()
order = resp.json()
```

Why `catalog-service:8001` and not `gateway:8080/catalog/...`: going
through the gateway from inside the cluster is an unnecessary hop (the
gateway exists to give *external* callers one origin; internal callers
are already on `backend-network` and can resolve `catalog-service` and
`ff-net-service` directly via Docker's embedded DNS — that's what
`container_name:` in `docker-compose.yml` buys you, zero extra
infrastructure).

Use this when the caller needs an answer before it can proceed (a
request that blocks on the result). Always set a `timeout` — an
internal HTTP call without one turns "ff-net-service is slow" into
"catalog-service hangs too."

### 5b. Asynchronous / one-way (when the caller shouldn't block or care about the result)

Example that would actually come up here: net-merging approving a merge
in `ff-net-service` probably shouldn't make `catalog-service`'s response
time depend on a live HTTP call to `ff-net-service`, if in the future
approving a net should also, say, reserve related inventory.

Nothing in this stack does this yet — no broker is deployed. When you
need it, the standard options, roughly in order of "how much
infrastructure it costs you":
1. **Redis Streams / Pub-Sub** — lightest weight, and you may already
   want Redis for caching by the time you need this.
2. **RabbitMQ** — if you need real work queues (retry, dead-lettering,
   multiple consumers per event) rather than just fan-out notifications.
3. **Kafka** — only if you need replayable event history / multiple
   independent consumer groups reading the same stream at their own
   pace. Overkill below a handful of services.

Whichever you pick, add it as its own service in `docker-compose.yml`
(same pattern as `gateway`), and treat the message schema the same way
as the HTTP schema in §3 — version it, don't let it drift silently.

### 5c. Service discovery

Currently **static**: Docker Compose's embedded DNS resolves
`catalog-service` / `ff-net-service` to the one running container of
each, because `docker-compose.yml` names them explicitly and there's
exactly one replica of each. This stops working the moment you need
**multiple replicas of the same service** (horizontal scaling) — at
that point `catalog-service` needs to resolve to *one of several*
healthy instances, which plain Docker Compose DNS doesn't load-balance.
That's the point to move to Docker Swarm mode, Kubernetes (`ClusterIP`
Services + DNS), or put every internal call through a proper
service-aware proxy — not before, since none of that buys you anything
at 1-replica-per-service.

---

## 6. Adding a new service — checklist

Follow the exact pattern `catalog-service`/`ff-net-service` already use;
don't invent a new one per service.

1. **Confirm the boundary first.** Does the new domain avoid importing
   from `apps.catalog` or `apps.ff_net`? If it needs their code, that's
   a signal it's not actually a separate service yet — either it's
   really part of an existing service, or it should talk to that
   service over HTTP (§5a) instead of importing its Python.
2. `services/<name>-service/apps/core/` — copy from an existing service
   (or, if you've since published `apps.core` as a real package per §2,
   add it to `requirements.txt` instead).
3. `services/<name>-service/apps/<domain>/` — the new business logic.
4. `services/<name>-service/<name>_service/{settings.py,urls.py,wsgi.py}`
   — copy an existing service's and adjust: `ROOT_URLCONF`,
   `WSGI_APPLICATION`, `INSTALLED_APPS` (only `apps.core` + this one
   domain app), `SPECTACULAR_SETTINGS["TITLE"]`.
5. `manage.py`, `serve.py`, `pytest.ini` — copy verbatim, only the
   `DJANGO_SETTINGS_MODULE` string and default `SERVE_PORT` change (next
   free port after `8002`).
6. `.env.template` — new `CASSANDRA_KEYSPACE`. Create that keyspace with
   `NetworkTopologyStrategy` / RF=3, matching the convention in
   `../casssndra/cassandra/schema.sql` and
   `../../backend/apps/ff_net/submodules/cassandra.sql` — don't use
   `SimpleStrategy`, see `../casssndra/RECOVERY.md` for why.
7. `requirements.txt` + `Dockerfile` — copy an existing service's,
   adjust the `EXPOSE`/default port.
8. Wire into root `docker-compose.yml`: new service block (mirror
   `catalog-service`'s), attached to `backend-network`, own
   `env_file`, own healthcheck hitting its `/health/`.
9. Add its route to `gateway/nginx.conf`: an `upstream` block + a
   `location /<name>/ { proxy_pass ...; }`, following the "prefix,
   don't rewrite" convention from §3.
10. If it needs to call an existing service, use §5a/§5b — never a
    Python import across the `services/` boundary.

**Not done here, worth doing before this goes past a handful of
services or past "trusted internal network":** auth between services
(mTLS or a shared internal token), centralized log aggregation (each
service currently logs to its own `logs/requests.log` + stdout,
independently), and distributed tracing (a call chain across 3+
services with only per-service logs gets hard to follow fast — this is
usually the actual trigger for adding OpenTelemetry, not a fixed service
count).

---

## 7. WSGI server: how it starts up cleanly

Every service's `serve.py` follows the identical sequence (shown here
for `catalog-service`; `ff-net-service`'s is byte-identical except
names):

```python
import logging
import os

from waitress import serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "catalog_service.settings")   # (1)

from catalog_service.wsgi import application                                  # (2)

log = logging.getLogger("serve")

if __name__ == "__main__":
    host = os.environ.get("SERVE_HOST", "127.0.0.1")                          # (3)
    port = int(os.environ.get("SERVE_PORT", "8001"))
    threads = int(os.environ.get("SERVE_THREADS", "8"))
    connection_limit = int(os.environ.get("SERVE_CONNECTION_LIMIT", "300"))

    log.info("Starting catalog-service (waitress) on %s:%s ...", host, port)  # (4)
    try:
        serve(application, host=host, port=port, threads=threads,            # (5)
          channel_timeout=15, cleanup_interval=5, connection_limit=connection_limit)
    except OSError:
        log.exception("Failed to bind %s:%s", host, port)                    # (6)
        raise
```

**(1) `DJANGO_SETTINGS_MODULE` is set before any Django import.** This
has to happen first — Django doesn't know which settings module to load
until this env var (or an explicit `django.setup()` argument) tells it,
and every subsequent import triggers Django machinery that needs it
already set.

**(2) Importing `wsgi.application` is what actually boots Django** — not
a separate explicit call. `catalog_service/wsgi.py` calls
`get_wsgi_application()`, which internally calls `django.setup()`. That
one call, in order:
- Loads `catalog_service/settings.py`, which itself calls
  `load_dotenv(BASE_DIR / ".env")` — so `.env` values become available
  to every `os.environ.get(...)` call in `settings.py` from this point on.
- Reads `SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]` — **not** `.get()`
  with a default. If `.env` is missing or incomplete, this raises
  `KeyError` immediately and the process never starts. That's
  deliberate: a service silently running with no secret key (or a
  hardcoded placeholder) is worse than a service that refuses to start.
- Populates `INSTALLED_APPS`, and for each app calls its `AppConfig.ready()`.
  `apps/core/apps.py`'s `ready()` is where `cassandra_session.start_poller()`
  gets called — a daemon thread that keeps trying to (re)connect to
  Cassandra every 10s if it isn't up yet. It's guarded against firing
  twice: once by a check against `manage.py`'s autoreload (only the
  reloaded child, `RUN_MAIN=true`, starts it — the parent watcher
  process doesn't), and once by a module-level `_poller_started` flag +
  lock. `serve.py` never uses the autoreloader at all, so in production
  that guard is simpler still: one process, one `ready()` call, one poller.

**(3)–(4) Config is read from the environment, then logged before
binding.** Reading `SERVE_HOST`/`PORT`/`THREADS`/`CONNECTION_LIMIT` happens
*after* step (2), not before — because step (2) is where `.env` actually
gets loaded into `os.environ` in the first place; reading server config
any earlier would silently ignore `.env` overrides. Logging the resolved
host/port/threads before calling `serve()` means a stuck or crashed
container's logs always show what it *tried* to bind, not just silence.

**(5) `waitress.serve(...)` blocks the main thread** — this is the
actual WSGI server loop, single process, `threads`-sized worker pool
(default 8) handling requests, with `channel_timeout=15` (idle sockets
killed after 15s instead of Waitress's ~60s default) and
`connection_limit` as backpressure so one bad client can't exhaust file
descriptors.

**(6) A failed bind (port already in use, no permission) is logged with
a full traceback, then re-raised — not swallowed.** Under Docker, that
makes the container exit non-zero, which is what makes
`docker-compose.yml`'s `restart: unless-stopped` and the healthcheck
`start_period` behave correctly — a service that failed to bind should
look *unhealthy/exited*, not silently hang forever pretending to be up.

**Why this is "clean" compared to `manage.py runserver`:** no
autoreload watcher process, no dev-only middleware, no duplicate poller
threads, no implicit `DEBUG=True` static file serving, and a startup
that either fully succeeds (one process, one Cassandra session, logged
bind confirmation) or fails loudly and immediately (missing secret key,
port conflict) rather than degrading silently. `manage.py runserver` is
for local iteration; `serve.py` is the only entrypoint meant to run
unattended, and every one of the choices above exists to make that
unattended failure mode "crash and get restarted" instead of "run in a
broken state nobody notices."

---

## Related docs

- [`README.md`](README.md) — what's built, why, how to run it.
- [`../casssndra/RECOVERY.md`](../casssndra/RECOVERY.md) — replication/repair/backup
  guarantees both services inherit from the shared cluster.
- [`../casssndra/AWS_RUNBOOK.md`](../casssndra/AWS_RUNBOOK.md) — validating the
  Cassandra cluster before the airgapped deploy (unaffected by this split).
