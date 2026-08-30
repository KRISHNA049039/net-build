# Airgap setup strategy: from a connected machine to running PCs

This is the map. Every individual step already has a doc that covers it
in real depth — this one exists so you have a single ordered path through
them, and a straight answer to "what do I actually need to download"
without hunting across seven files for it. If a step below just says
"see X," that doc is the source of truth; this one won't repeat its
commands, only where that step sits in the overall sequence.

**The strategy in one sentence:** build and prove everything on one
internet-connected machine first (Phases 1-3), package it (Phase 4),
carry it over on physical media (Phase 5), then bring it up for real on
isolated hardware in a fixed order — Cassandra ring, then auth, then
apps, then observability (Phase 6) — and verify it all over again on the
real hardware before trusting it (Phase 7).

---

## Phase 1 — Prerequisites on the connected machine

- **Docker Desktop** (Windows/Mac) or Docker Engine + Compose v2 (Linux)
  — everything in this repo runs in containers; there's no path that
  avoids needing this.
- **git** — to have the repo at all.
- **Python 3.11+** with `pip` — only needed if you're going the
  `pip download` route for the monolith instead of containerizing it (see
  Phase 4).
- **`jq`** — a single static binary, not a Docker image. Needed on
  whichever airgapped host runs `casssndra/scripts/consolidate-checkpoint.sh`
  / `verify-restore.sh`. Download it now; don't assume it's already on
  the target PC.
- Enough free disk to hold every image tarball **and** the repo **and**
  a Cassandra data volume with real test data, all at once, on this one
  machine — the single-machine test (Phase 3) runs the entire stack
  locally before anything ships.

---

## Phase 2 — The shopping list: every image and tool

Everything this repo runs, across every architecture (monolith,
microservices, both Cassandra topologies, logging/observability):

| Image | Used by | Purpose |
|---|---|---|
| `cassandra:5.0.7-bookworm` | `casssndra/*.yml`, `dis/docker-compose.node.yml` | The database, every topology |
| `ipushc/cassandra-web:v1.1.5` | same | Cassandra browsing UI |
| `kong:3.7` | `microservices/docker-compose.yml` | API gateway |
| `grafana/loki:3.1.1` | `logging/docker-compose.yml` | Log storage |
| `grafana/promtail:3.1.1` | `microservices/docker-compose.yml`, `logging/promtail-monolith/` | Log shippers (2 separate deployments, same image) |
| `prom/prometheus:v2.55.1` | `logging/docker-compose.yml` | Metrics storage |
| `grafana/grafana:11.2.0` | `logging/docker-compose.yml` | Dashboards/Explore UI |
| `python:3.11-slim` | both microservices' `Dockerfile`s | Base image, pulled at *build* time, not run directly |

Plus, **not a Docker image**: `jq` (Phase 1), and whichever Python
dependency set you choose in Phase 4 (wheels, or nothing at all if you
containerize the monolith too).

This table is the same list `AIRGAP_TESTING.md`'s Part 2 keeps current in
detail (exact `docker save`/`load` commands, and a "practice this on the
single-machine rig before trusting it" step) — check that doc, not this
one, before you actually run the transfer, in case it's moved on since
this list was last synced.

---

## Phase 3 — Build and prove it, still connected

Don't skip to packaging. Prove the software itself has no hidden
internet dependency **before** you're on-site discovering one:
[`AIRGAP_TESTING.md`](AIRGAP_TESTING.md) Part 1 — bring up the full stack
on this one machine (5-node Cassandra ring included, via
`docker-compose.cluster.yml`, which simulates the ring on one Docker
host), physically cut the network, and run its 7-item smoke test (Swagger
UI, CRUD, health checks, logs in Grafana, metrics + dashboard, repair
scripts, killing a node).

If any of those 7 fail with the cable out, fix it here — that's exactly
what this phase is for, and it's a much cheaper place to find a gap than
five airgapped PCs.

---

## Phase 4 — Package for transfer

Once Phase 3 passes clean:

1. **Every image in Phase 2's table** — `docker save`/`docker load`,
   exact commands and the "practice the round-trip" step in
   `AIRGAP_TESTING.md` Part 2's "Docker images" section.
2. **Python dependencies** — for the microservices, prefer transferring
   the already-built Docker images (step 1 covers this) over vendoring
   wheels separately; for the monolith, `AIRGAP_TESTING.md`'s "Python
   dependencies" section covers both the wheel-vendoring path and why
   `backend/requirements.lock.txt` (a conda lockfile) isn't portable as-is.
3. **Secrets and config, generated fresh** — `DJANGO_SECRET_KEY` per
   service, Cassandra's rotated superuser password, `catalog_app`/
   `ff_net_app` passwords, `GRAFANA_ADMIN_PASSWORD`. Never generated
   on-site under time pressure, never committed, never the repo's
   `.env.template` defaults — see `AIRGAP_TESTING.md`'s "Secrets and
   config" section and `casssndra/AUTH.md` for what each one guards.
4. **The repo itself** — a clone (or a `git archive`/copy of the working
   tree) at the commit you actually validated in Phase 3. Bring the whole
   thing, not a hand-picked subset of files — `kong.yml`,
   `prometheus.yml`, the Grafana dashboard/datasource JSON, every
   `docker-compose.yml`, are all config-as-files by design specifically
   so "copy the repo" is the transfer mechanism, not a manual rebuild.
5. **`jq`**, vendored per Phase 1.

---

## Phase 5 — Physical transfer

Site/policy-specific, but regardless of the exact media (USB drive,
write-once optical, whatever your process mandates):
- Checksum every image tarball before and after copy (`sha256sum`) — a
  silently truncated `docker load` on-site, discovered only when a
  container won't start, is a bad way to learn media was bad.
- Carry secrets (Phase 4 item 3) via whatever channel your policy
  actually requires for credentials — not necessarily the same media as
  the bulk image tarballs, if your policy separates those.

---

## Phase 6 — Bring it up on the real hardware, in order

This order matters — each step depends on the previous one being up.

1. **Firewall rules, every PC** — `casssndra/dis/RUNBOOK.md` §0, before
   anything else starts.
2. **Clock sync across all PCs.** No path to public NTP on an airgapped
   LAN — stand up a local time source first. Cassandra's conflict
   resolution depends on this; skipping it causes silent data bugs, not a
   clean failure. See `AIRGAP_TESTING.md`'s "Cluster-specific" section.
3. **`docker load` every image tarball**, on every PC that actually needs
   it (not all 5 need all images — only whichever host(s) run the
   microservices/monolith/gateway/logging stack need those; every
   Cassandra PC needs the Cassandra + cassandra-web images).
4. **Bring up the Cassandra ring** — `dis/RUNBOOK.md` §1 (seeds first,
   confirm `UN` before adding the rest).
5. **Load schema and seed data** — `dis/RUNBOOK.md` §6.
6. **Auth bootstrap** — `dis/RUNBOOK.md` §7 / `casssndra/AUTH.md` §2:
   fix `system_auth`'s replication factor, rotate the superuser password,
   create `catalog_app`/`ff_net_app`. Do this **before** step 7 points any
   real app traffic at the cluster.
7. **Bring up the app** (whichever architecture — monolith or
   microservices + Kong), using the secrets from Phase 4 item 3, on
   whichever host(s) you designated.
8. **Bring up logging + observability** — `logging/docker-compose.yml`
   (Loki, Grafana, Prometheus) on the designated log host, then
   `promtail-microservices` (part of the app's own compose file) and/or
   `promtail-monolith` (`logging/promtail-monolith/`, on whichever host
   runs the monolith) pointed at that host's LAN IP. Full detail:
   `logging/CENTRALIZED_LOGGING.md`, `OBSERVABILITY_STACK.md`.

---

## Phase 7 — Verify for real, on the actual hardware

Phase 3 proved the *software*. This proves the *deployment* — re-run the
same checks, but now across real separate machines over the real LAN, not
`localhost`:
- Every item in `AIRGAP_TESTING.md` Part 1 §5, with URLs pointed at each
  host's real LAN IP instead of `localhost`.
- `nodetool status` from any Cassandra PC shows all 5 as `UN`, correct
  rack split (3 rack1 / 2 rack2 per `dis/RUNBOOK.md`'s table).
- **A real rack-loss drill**, not just `docker stop` on one container —
  pull the network cable (or power) on one whole PC, confirm the ring and
  the app both keep answering, confirm the PC rejoins cleanly when
  reconnected. `AWS_RUNBOOK.md` §8 has the checklist this is modeled on;
  run it there once on disposable AWS hardware before trying it on the
  real airgapped PCs, since AWS is cheap to break and rebuild and the
  physical PCs aren't.

Don't call the deployment done until this phase passes, not just Phase 3.

---

## Phase 8 — Ongoing: updates and recovery

This isn't a one-time process — treat future changes the same way:
- **Code or config changes**: repeat Phases 3-5 for whatever changed
  (rebuild/re-validate on the connected machine, re-package, re-transfer)
  rather than hand-editing files directly on the airgapped side, so the
  connected machine stays the single source of truth.
- **New image versions**: same — pull, validate, `save`/transfer/`load`,
  never `docker pull` directly on an airgapped host (it'll just fail, but
  don't rely on that as your safety net).
- **Backup/restore, repair, disaster recovery**: not part of this setup
  flow — see `casssndra/RECOVERY.md` and
  `casssndra/DISASTER_RECOVERY.md` once the cluster is live.

---

## Related docs

- [`AIRGAP_TESTING.md`](AIRGAP_TESTING.md) — Phase 3's smoke test in full,
  Phase 2/4's image and secrets checklists in full detail.
- [`casssndra/dis/RUNBOOK.md`](casssndra/dis/RUNBOOK.md) — Phase 6's
  Cassandra bring-up, section by section.
- [`casssndra/AUTH.md`](casssndra/AUTH.md) — the auth bootstrap in Phase 6
  step 6, and what each credential actually guards.
- [`casssndra/AWS_RUNBOOK.md`](casssndra/AWS_RUNBOOK.md) — Phase 7's
  rack-loss drill, validated on disposable hardware first.
- [`logging/CENTRALIZED_LOGGING.md`](logging/CENTRALIZED_LOGGING.md),
  [`OBSERVABILITY_STACK.md`](OBSERVABILITY_STACK.md) — Phase 6 step 8 in
  full.
- [`README.md`](README.md) — what this system is, before you set out to
  airgap it.
