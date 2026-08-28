# Gateway + observability stack: Kong, Loki, Prometheus, Grafana

Companion to [`microservices/ARCHITECTURE.md`](microservices/ARCHITECTURE.md) and
[`logging/CENTRALIZED_LOGGING.md`](logging/CENTRALIZED_LOGGING.md) (which each
cover their own piece in depth). This doc is the "how the four pieces fit
together" reference: what each one actually is under the hood, how they
talk to each other and to the app, and how to prove each link in the chain
actually works rather than just trusting it's wired up right.

```
                                clients
                                   |
                                   v
                       +-----------------------+
                       |   gateway (Kong)        |  :8080 -- public entry point
                       |   /catalog/* /ff_net/*  |  DB-less, kong.yml is the config
                       +-----------+-------------+
                                   |
                     +-------------+--------------+
                     v                             v
        +----------------------+        +----------------------+
        |   catalog-service     |        |   ff-net-service      |
        |   :8001                |        |   :8002                |
        |   writes logs/*.log    |        |   writes logs/*.log    |
        |   serves /metrics/     |        |   serves /metrics/     |
        +-----------+------------+        +-----------+------------+
                     |  tailed by                      |  tailed by
                     |  promtail                        |  promtail
                     |  (push)                           |  (push)
                     +----------------+------------------+
                                      v
                          +------------------------+
                          |   loki  :3100            |  log storage + query API
                          +-----------+--------------+
                                      ^  (query, on read)
        +-----------------------------+
        |                              scrapes (pull, every 15s)
        |                    +-------------------------------+
        |                    v                               v
        |         +----------------------+        +----------------------+
        |         |  catalog-service       |        |  ff-net-service       |
        |         |  GET /metrics/         |        |  GET /metrics/        |
        |         +----------------------+        +----------------------+
        |                    ^
        |                    |
        |          +------------------------+
        +--------->|   prometheus  :9090      |  metrics storage + query API
                    +-----------+--------------+
                                ^  (query, on read)
                                |
                    +------------------------+
                    |   grafana  :3000         |  Loki + Prometheus datasources
                    |   (dashboards, Explore)  |  provisioned declaratively
                    +------------------------+
```

The two arrows into Loki/Prometheus are drawn differently on purpose —
that difference (push vs. pull) is the single most important thing to
understand about how this stack is wired, and §2/§3 below explain why.

---

## 1. Kong — the gateway

**What it is.** Kong is nginx (OpenResty) + a Lua runtime for request
processing, running in **DB-less mode** here (`KONG_DATABASE: "off"`) —
there's no Postgres backing it. Normally Kong's config (services, routes,
plugins) lives in a database you edit via its Admin API at runtime; DB-less
mode instead loads one YAML file (`microservices/gateway/kong.yml`) at
startup and treats it as the *entire* config, immutable until you reload.
That's a deliberate fit for this repo's airgapped target — one file to
version-control and copy to the airgapped side, no extra database to run,
back up, or restore.

**Core concepts** (all declared in `kong.yml`):
- **Service** — an upstream to proxy to (`catalog-service` →
  `http://catalog-service:8001`).
- **Route** — a path/host/method pattern that, when matched, sends the
  request to that service (`/catalog` → the `catalog-service` Service).
- **`strip_path: true`** — the matched route prefix is removed before
  forwarding, so `/catalog/api/catalog/orders/` reaches the service as
  `/api/catalog/orders/`, unchanged from what it'd be if you called the
  service directly. Kong only routes; it never rewrites a service's own
  URL structure.
- **Plugins** — request/response middleware (auth, rate-limiting,
  metrics...) attached to a Service or Route. `rate-limiting` is enabled
  on both routes (see §6); no auth plugin (see §7).

**How it interacts with the rest of the stack:** purely as the front
door for HTTP traffic into `catalog-service`/`ff-net-service` — it does
**not** talk to Loki, Prometheus, or Grafana. Kong's own access/error logs
go to `stdout`/`stderr` only (see `KONG_PROXY_ACCESS_LOG`/`ERROR_LOG` in
`microservices/docker-compose.yml`), so unlike the app services, Kong's
own request log currently isn't shipped into Loki (there's no file for
Promtail to tail, and Kong isn't in either Promtail's `docker-compose.yml`
volume mounts).

**Editing the config:** change `kong.yml`, then either
`docker exec gateway kong reload` (in-place, no downtime) or
`docker compose restart gateway` — Kong re-reads the file on either. There
is no "apply" step beyond that; the file **is** the running config, so a
YAML typo shows up as a startup failure (Kong validates before it starts
serving), not a silent partial apply.

**Verifying it:**
```bash
docker logs gateway --tail 20 | grep "declarative config loaded"   # confirms kong.yml parsed clean
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/catalog/health/   # expect 200
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/ff_net/health/    # expect 200
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/                 # expect 404 -- no route matches "/", by design
```

---

## 2. Loki — log aggregation

**What it is.** Loki stores logs but, unlike Elasticsearch, **does not
index log content** — only the small set of *labels* attached to each log
stream (here: `service`, `job`, `filename`, `env`). Log lines within a
stream are stored compressed in "chunks" and scanned at query time (LogQL
filters like `|= "error"` run over chunk contents live, not against a
pre-built inverted index). That trade-off is exactly why it's cheap to run
self-hosted for this use case: indexing every word across every log line
scales very differently than indexing only `{service="catalog-service"}`.

**Ingestion is push, not pull.** Loki never reaches out to find logs —
something else (Promtail) tails log *files* and pushes batches to Loki's
HTTP API (`POST /loki/api/v1/push`). Two separate Promtail instances feed
the one Loki:
- `promtail-microservices` (defined in `microservices/docker-compose.yml`)
  — reads the named-volume log files `catalog-service`/`ff-net-service`
  write to, on `backend-network`, resolves Loki as `http://loki:3100`.
- `promtail-monolith` (`logging/promtail-monolith/`) — a **separate**
  compose file, meant to run on whatever host runs `backend/serve.py`
  (typically a different machine), so it points `LOKI_URL` at Loki's LAN
  IP:port instead of a container name (see `logging/promtail-monolith/.env`).

Full detail on why file-tailing (not Docker-socket/stdout scraping) was
chosen, and the app-side logging change that made every process's output
land in a file at all, is in
[`logging/CENTRALIZED_LOGGING.md`](logging/CENTRALIZED_LOGGING.md) §1 —
this doc only covers Loki/Grafana/Prometheus/Kong's own mechanics.

**Storage internals** (`logging/loki-config.yml`): filesystem-only (no
S3/GCS — required for airgapped), TSDB index format, 7-day
`retention_period`. One real gotcha hit while wiring this up, worth
knowing if you touch this file again: Loki 3.x refuses to start with
`compactor.retention_enabled: true` unless
`compactor.delete_request_store` is also set — retention deletion is
implemented via delete *requests*, which need somewhere to persist
pending requests, and there's no default. Fixed here with
`delete_request_store: filesystem`, matching the rest of this config's
"filesystem only" theme.

**LogQL basics** (used identically in Grafana Explore or the raw API):
- `{service="catalog-service"}` — a stream selector, always required first.
- `{service="catalog-service"} |= "error"` — substring filter.
- `{service="catalog-service"} | logfmt | duration > 100ms` — structured
  parsing + field filter (only useful if the log line is logfmt/JSON;
  these app logs are currently plain text, so this form isn't used yet).

**Verifying it:**
```bash
curl -s http://localhost:3100/ready                                  # "ready"
curl -s http://localhost:3100/loki/api/v1/label/service/values       # which services have shipped logs
curl -sG http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={service="catalog-service"}' --data-urlencode 'limit=5'
```

---

## 3. Prometheus — metrics

**What it is.** Prometheus stores **time series**: a metric name + a set
of labels + a numeric value at a timestamp (e.g.
`http_requests_total{service="catalog-service",status="200"} 4931`). It's
a pull-based system — the *opposite* direction from Loki/Promtail: instead
of the app pushing metrics out, Prometheus itself scrapes (HTTP `GET`) a
`/metrics/` endpoint on each target on a fixed interval
(`scrape_interval: 15s` in `casssndra/prometheus.yml`) and stores whatever
it finds. Each app service's `/metrics/` (`apps/core/metrics_view.py`)
returns the current values in **Prometheus exposition format** on every
request — the service doesn't know or care that anything is scraping it;
it just answers a plain GET.

**Targets configured** (`casssndra/prometheus.yml`):
- `catalog-service:8001/metrics/`, `ff-net-service:8002/metrics/` —
  reached by Docker container name, since Prometheus (like Loki/Grafana)
  runs on `backend-network` alongside them.
- `django-backend` at `host.docker.internal:8000/metrics/` — for the
  monolith path (`backend/serve.py`, run directly on the host, not in a
  container), reached the way a container reaches the Docker host itself.
  Shows `down`/connection-refused whenever you're running the
  microservices architecture instead — that's expected, not a fault.

**A real gotcha hit wiring this up, worth knowing:** the first scrape of
`ff-net-service` came back `400 Bad Request`, not a connection error.
Prometheus sends the scrape request with `Host: ff-net-service:8002` (the
target address), and Django's `ALLOWED_HOSTS` check rejects any request
whose `Host` header isn't in that list — the `.env` only had
`127.0.0.1,localhost,host.docker.internal`, never the container's own
name, because nothing had needed to call it by that name before. Fixed by
adding `catalog-service`/`ff-net-service` to each service's
`DJANGO_ALLOWED_HOSTS`. This is a general rule, not a one-off: **any**
target Prometheus scrapes by container/hostname needs that exact name in
`ALLOWED_HOSTS`, the same way `CORS_ALLOWED_ORIGINS` needs to know about
browser-facing origins.

**Internals:** samples are written to an on-disk TSDB
(`prometheus-data` volume, `--storage.tsdb.path=/prometheus`), organized
in 2-hour head blocks that later compact — this is what makes range
queries over recent data fast without an external database.

**PromQL basics:**
- `up` — 1/0 per target, the simplest possible health query (this is what
  `/api/v1/targets` also exposes via HTTP, used below).
- `rate(http_requests_total[5m])` — per-second request rate over a
  sliding 5-minute window; the standard shape for turning a counter into
  a graphable rate.

**Verifying it:**
```bash
curl -s http://localhost:9090/-/healthy
curl -s http://localhost:9090/api/v1/targets | python -c "
import json,sys
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(t['labels']['job'], '->', t['health'], t.get('lastError',''))
"
# expect catalog-service/ff-net-service -> up; django-backend -> down unless the monolith is also running
```

---

## 4. Grafana — the UI

**What it is.** Grafana stores none of your logs or metrics itself — it's
a query/render layer over datasources. It queries Loki/Prometheus live,
every time you load a panel or run an Explore query. The only thing
Grafana persists locally (`grafana-data` volume, an internal SQLite db) is
its **own** state: users, dashboards you've saved, and the datasource
config below.

**Datasources are provisioned declaratively**
(`logging/grafana-datasources.yml`, mounted read-only into
`/etc/grafana/provisioning/datasources/`), not clicked together in the
UI — consistent with this repo's "config as a file you can copy to the
airgapped side" pattern used everywhere else (Kong, Loki, Promtail). Two
are defined:
- **Loki** (`http://loki:3100`) — marked `isDefault: true`, so Explore
  opens on it by default.
- **Prometheus** (`http://prometheus:9090`).

Both use `access: proxy`, meaning your browser never talks to Loki or
Prometheus directly — it talks to Grafana, and Grafana's backend makes
the actual query call server-side (inside `backend-network`, where those
container names resolve). This is also why Grafana itself needs to be on
`backend-network`.

**Using it:**
- **Explore** (compass icon) — ad-hoc queries against either datasource,
  the fastest path to "show me recent logs/metrics for X," no dashboard
  needed.
- **Dashboards** — the "Microservices Overview" dashboard (§7) is
  pre-built and provisioned; open it directly instead of starting from
  Explore for routine checks.
- **Auth:** `admin`/`${GRAFANA_ADMIN_PASSWORD:-admin}` (from
  `logging/.env`), anonymous access disabled
  (`GF_AUTH_ANONYMOUS_ENABLED=false`), and `GF_INSTALL_PLUGINS=""` /
  analytics reporting disabled so nothing phones home — required for the
  airgapped target, harmless otherwise.

**Verifying it:**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/api/health   # expect 200
```
Then in the browser: Explore → Loki → `{service="catalog-service"}`, and
Explore → Prometheus → `up`.

---

## 5. Putting it together: one request, end to end

A client calls `GET http://localhost:8080/catalog/health/`:

1. **Kong** matches the `/catalog` route, strips the prefix, proxies to
   `http://catalog-service:8001/health/`.
2. **catalog-service** handles it, and as a side effect of
   `apps/core/request_logging.py` middleware: (a) appends a line to
   `logs/requests.log`, and (b) increments Prometheus counters exposed at
   `/metrics/` (nothing is pushed anywhere yet at this point — the metric
   just now exists in-process, waiting to be scraped).
3. **Promtail** (`promtail-microservices`), which has been tailing
   `logs/requests.log` continuously, picks up the new line within its
   batch interval and pushes it to **Loki**.
4. Independently, on its own 15s timer, **Prometheus** sends
   `GET /metrics/` to `catalog-service:8001` and stores whatever counter
   values it sees at that instant.
5. Sometime later, a person opens **Grafana**, queries Loki for
   `{service="catalog-service"}` and sees step 2's log line, and queries
   Prometheus for `rate(http_requests_total[5m])` and sees step 4's
   counters turned into a rate.

The key thing this makes concrete: **logs and metrics reach their stores
on entirely different schedules and by entirely different mechanisms**
(push-when-it-happens vs. pull-on-a-timer) — there's no shared pipeline
between them, no coupling, and no ordering guarantee between "the log line
exists in Loki" and "the metric bump is visible in Prometheus" for the
same request. They're two independent views of the same event.

---

## 6. Rate-limiting and the pre-built dashboard

Two of the original gaps below are now closed.

**Kong rate-limiting** (`microservices/gateway/kong.yml`): each service's
route carries a `rate-limiting` plugin, `minute: 300`, `policy: local`.
`policy: local` means each Kong instance counts requests **in its own
memory** — no Redis, no shared state, which is why it's safe with zero
extra infrastructure here. The trade-off, if this ever runs as more than
one Kong replica: each replica enforces its own 300/min independently, so
the *effective* cluster-wide limit becomes `300 × replica count`, not 300.
Fine at one gateway instance; revisit (`policy: redis`) before scaling
Kong horizontally. This was chosen over `key-auth` specifically because
it's non-breaking — no key required, so every existing caller (including
your own curl testing) keeps working exactly as before, just capped.
Verify it landed via the response headers on any request:
```bash
curl -si http://localhost:8080/catalog/health/ | grep -i ratelimit
# X-RateLimit-Limit-Minute: 300
# X-RateLimit-Remaining-Minute: 299
```

**Grafana dashboard** (`logging/grafana-dashboards/microservices-overview.json`,
provisioned the same declarative way as the datasources — via
`logging/grafana-dashboards-provider.yml`, a `type: file` provider Grafana
polls every 30s for changes, so editing the JSON and saving is enough,
no reload/restart needed). One dashboard, "Microservices Overview,"
covering both services identically: request rate by status code, p95
latency (`histogram_quantile` over the `http_request_duration_seconds`
histogram), a `cassandra_up` stat panel, and a live Loki logs panel —
Prometheus and Loki panels side by side on one screen, which is the
"correlate logs and metrics without leaving the page" workflow Explore
alone doesn't give you. Referenced by fixed datasource `uid`s (`loki`,
`prometheus`, set explicitly in `grafana-datasources.yml` rather than
left to Grafana's auto-generated ones) so the dashboard JSON's datasource
references stay stable across redeploys.

**Troubleshooting note:** if `admin`/`${GRAFANA_ADMIN_PASSWORD}` ever
stops working after recreating the `grafana` container, it's because that
env var only seeds the admin password on a *fresh* `grafana-data` volume
— it's not re-applied on every restart. Recover with:
```bash
docker exec grafana grafana-cli admin reset-admin-password admin
```

---

## 7. What's still not done

- **No Kong metrics into Prometheus.** Kong has a `prometheus` plugin
  that would expose Kong's own request/latency/status metrics at
  `/metrics` for scraping — not enabled, so you currently have no
  visibility into gateway-level behavior (only what each service's own
  `/metrics/` reports), only access/error logs on `stdout`.
- **No alerting.** Grafana can alert on either datasource (e.g. "error
  rate > 5% for 5m", "more than N ERROR log lines in 5m"), but no rules
  exist, and there's no notification channel (email/webhook) configured
  to send them to yet — that's a prerequisite decision, not just config.
- **No log-to-metric correlation** (LogQL `unwrap` / Prometheus recording
  rules) — logs and metrics are both queryable but not cross-linked (e.g.
  jumping from a Prometheus spike straight to the matching log lines).
- **No distributed tracing** — with only two services that don't call
  each other yet, this is a deliberate non-goal, not an oversight; per
  `microservices/ARCHITECTURE.md` §6, the usual trigger is an actual
  multi-service call chain getting hard to follow from logs alone, not a
  fixed service count. Revisit if/when that happens.
- **Kong has no auth plugin enabled** — rate-limiting (§6) is on, but
  `key-auth`/similar is deliberately not, since every current caller
  would need a key issued first. See `ARCHITECTURE.md` §4/§6 for when
  that trade-off is worth making.

---

## Related docs

- [`microservices/ARCHITECTURE.md`](microservices/ARCHITECTURE.md) — the
  services Kong routes to; §3 has the full gateway path convention.
- [`logging/CENTRALIZED_LOGGING.md`](logging/CENTRALIZED_LOGGING.md) — Loki
  ingestion path in full detail, including the app-side logging change
  that made file-tailing possible at all.
- [`README.md`](README.md) — overall repo layout and how to bring
  everything up.
