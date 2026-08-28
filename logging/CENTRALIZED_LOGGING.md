# Centralized logging (+ Cassandra replication/restoration recap)

Self-hosted log aggregation for both the `backend/` monolith and the
`microservices/` split — one place to search logs across whichever of
the two architectures (or both, side by side) you're running, and across
however many hosts they're spread over in the airgapped deployment.
Built the same way as everything else in this repo: Docker Compose,
filesystem storage, zero calls out to the internet or a managed cloud
service.

```
   backend/serve.py (bare host, e.g. a Windows PC)          microservices/ (Docker host)
   writes: backend/logs/{app,requests}.log                  catalog-service, ff-net-service
              |                                              write: named volume /app/logs each
              v                                                         |
   +---------------------+                                              v
   | promtail-monolith    |                              +----------------------+
   | (logging/promtail-   |                              | promtail-microservices|
   |  monolith/)          |                               | (in microservices/    |
   +----------+------------+                              |  docker-compose.yml)  |
              |                                            +-----------+-----------+
              |                                                        |
              +--------------------- pushes to ------------------------+
                                          |
                                          v
                          +----------------------------+
                          |   loki  (logging/)          |   :3100
                          |   filesystem storage only    |
                          +--------------+---------------+
                                         |
                                         v
                          +----------------------------+
                          |   grafana  (logging/)       |   :3000
                          |   Loki datasource pre-wired  |
                          +----------------------------+
```

---

## 1. How it's built

Three moving pieces, matching the "one thing per concern" layout the
rest of this repo already uses:

1. **`logging/`** — the central stack. `loki` (log storage + query API)
   and `grafana` (the UI you actually search logs in). Run this **once**,
   on one designated host, reachable over the LAN from every other host.
2. **A Promtail per log source** — the shipper. Promtail tails log
   *files* (not stdout, not a socket) and pushes them to Loki's HTTP
   push API. Two of them exist:
   - `microservices/docker-compose.yml`'s `promtail` service — reads
     `catalog-service`'s and `ff-net-service`'s log volumes.
   - `logging/promtail-monolith/` — a separate, standalone compose file
     you run **on whichever host runs `backend/serve.py`**, since that's
     typically a different machine than the Docker host running the
     microservices or the central Loki.
3. **A file for every process to write to** — this is the part that
   actually required a code change. Before this, only the per-request
   log line (`apps/core/request_logging.py`) went to a file
   (`logs/requests.log`); everything else (startup, Cassandra
   connect/reconnect, warnings, tracebacks) only went to `stdout`. That
   was invisible to Promtail for anything not running under Docker (a
   bare `python serve.py` on a Windows PC has no "container stdout" for
   anything to scrape). Fixed by adding an `app_file` handler to each
   settings.py's `LOGGING["root"]`, so **everything** now lands in
   `logs/app.log` too, regardless of how the process was launched.

**Why file-tailing instead of scraping container stdout via the Docker
socket:** it's the same mechanism for the monolith (bare host, no Docker
socket to speak of) and the microservices (Docker containers) — one
thing to explain, one thing to debug, and it doesn't require handing
Promtail read access to the Docker socket (a bigger permission than a
read-only log volume mount).

---

## 2. Running it

**Central stack** (once, on the log host):
```bash
cd logging
cp .env.template .env    # GRAFANA_ADMIN_PASSWORD
docker compose up -d
# http://<log-host>:3000  (admin / whatever you set)
# Loki push API: http://<log-host>:3100
```

**Microservices' logs** (on the microservices Docker host):
```bash
cd microservices
# LOKI_URL defaults to http://loki:3100 -- only override if the central
# stack ISN'T on this same backend-network (see docker-compose.yml)
docker compose up -d      # promtail comes up alongside the two services
```

**Monolith's logs** (on whichever host runs `backend/serve.py`, needs
Docker Desktop there):
```bash
cd logging/promtail-monolith
cp .env.template .env    # LOKI_URL -- almost always a different host's IP
docker compose up -d
```
**Without Docker** on that host: run the `promtail` binary directly
(Grafana ships Windows/Linux/macOS builds) with
`logging/promtail-monolith/promtail-config.yml` and `LOKI_URL` set as an
environment variable before launch — same config file, no compose needed.

---

## 3. Finding your logs

In Grafana (`:3000`) → Explore → Loki, every log stream is labeled by
`service` and `env`:

| `service` label | `env` label | Source |
|---|---|---|
| `catalog-service` | `microservices` | `microservices/services/catalog-service` |
| `ff-net-service` | `microservices` | `microservices/services/ff-net-service` |
| `backend-monolith` | `monolith` | `backend/` |

Example LogQL queries:
```
{service="catalog-service"}                          # everything from catalog-service
{service="backend-monolith"} |= "ERROR"               # errors on the monolith
{env="microservices"} |= "cassandra"                   # cassandra_session log lines, either service
{service=~"catalog-service|ff-net-service"} | logfmt   # both services, parsed
```

Both `app.log` (root logger: startup, Cassandra connect/reconnect,
warnings, tracebacks) and `requests.log` (one line per HTTP request) are
shipped — they land as the same `service` label, differentiated by the
logger name embedded in each line (`{asctime} {levelname} {name}
{message}` — filter on `requests` vs anything else in `{name}` with
`|= "requests"` if you only want the per-request lines).

---

## 4. Adding a new service's logs

If you add a service per `microservices/ARCHITECTURE.md` §6, wire its
logs in the same pattern:
1. Give it the `app_file` handler in its `LOGGING["root"]` (copy from
   `catalog_service/settings.py`) — don't skip this, it's the one place
   this is easy to forget and end up with a service whose only logs are
   the ones that happen to hit a log line that also goes through
   `request_logging.py`.
2. Add a named volume for its `logs/` dir in `microservices/docker-compose.yml`,
   mounted into the service AND into `promtail` (read-only) at
   `/var/log/<name>-service`.
3. Add a `static_configs` block to `microservices/promtail-config.yml`
   with `service: <name>-service` as a label — that's what makes it
   show up as its own filterable stream in Grafana.

---

## 5. Cassandra replication & restoration (recap)

Full detail lives in [`../casssndra/RECOVERY.md`](../casssndra/RECOVERY.md)
and [`../casssndra/AWS_RUNBOOK.md`](../casssndra/AWS_RUNBOOK.md) — this is
the short version, since it's the other half of "how does this system
survive things going wrong" alongside logging:

- **Replication**: 5-node cluster, 2 racks, `NetworkTopologyStrategy`
  RF=3 on both keyspaces (`django_platform`, `ans_transformed`). Reads
  and writes go through the driver at `LOCAL_QUORUM` (2-of-3 replicas
  ack), so a single node down — or, with replicas spread across racks,
  most of a whole rack down — doesn't take the app down.
- **Repair**: hinted handoff only covers outages under ~3h; anything
  longer needs `casssndra/scripts/repair.sh`, run weekly per node,
  staggered so the 5 nodes don't repair simultaneously.
- **Backup/restore**: `casssndra/scripts/checkpoint.sh` snapshots +
  exports a tarball per node; restore procedure and dead-node
  replacement (`replace_address_first_boot`) are both in `RECOVERY.md`.
  Whole-cluster loss (checkpoints, watermarks, rebuild runbook) is
  `casssndra/DISASTER_RECOVERY.md`.

**Not currently wired into this logging stack**: Cassandra's own
container logs (gossip, compaction, GC) aren't shipped to Loki yet —
only the Django app layer is. Add a `static_configs` block to a Promtail
scraping `casssndra/dis/`'s nodes the same way if you need that; it
wasn't done here because nothing in the current recovery/repair
procedures depends on log search, only on `nodetool status`/`nodetool
repair` output, which you watch live rather than search after the fact.

---

## 6. What's not done here

- **No alerting.** Grafana can alert on Loki queries (e.g. "more than N
  `ERROR` lines in 5 minutes"), but no alert rules are configured. Worth
  doing once this is more than a couple of people watching dashboards by
  hand.
- **No log-based metrics extraction** (LogQL `unwrap`/recording rules
  turning log lines into Prometheus-style metrics). The app already
  exposes real Prometheus metrics directly (`/metrics/` on each
  service) — `casssndra/prometheus.yml` is a scrape config for those,
  but no Prometheus container actually runs anywhere yet. Wiring that up
  is a separate, smaller task from this one (metrics, not logs) if you
  want Grafana dashboards backed by both.
- **Retention is 7 days** (`logging/loki-config.yml`), filesystem-only,
  single replica — fine for an internal ops tool, not a compliance/audit
  log store. Raise `retention_period` (and disk accordingly) if you need
  longer.
