# Testing airgap-readiness on one internet-connected machine

The goal here is narrow: prove, on a single machine that still has
internet, that nothing in this stack needs the internet to run —
*before* you're standing in front of 5 PCs with no internet finding out
the hard way. Two parts: (1) a single-host test that exercises the same
software/config as the real cluster, and (2) a checklist of what to
gather/fix/generate ahead of time so the physical transfer isn't where
you discover a gap.

This assumes you've read [`casssndra/RECOVERY.md`](casssndra/RECOVERY.md),
[`casssndra/AUTH.md`](casssndra/AUTH.md), and
[`logging/CENTRALIZED_LOGGING.md`](logging/CENTRALIZED_LOGGING.md) — this
doc is "how do I know all of that actually works with the network cable
pulled," not a repeat of what's in them.

---

## Part 1 — Single-machine test

### Why single-machine, and why it's still a valid test

`casssndra/docker-compose.cluster.yml` already runs the full 5-node ring
on one Docker host (each node is a separate container on one bridge
network) — same image, same `NetworkTopologyStrategy`/RF=3, same auth
entrypoint wrapper as `dis/docker-compose.node.yml`. The only thing it
*doesn't* test is real network partitions between physical machines
(that's what `AWS_RUNBOOK.md`'s rack-loss drill is for, on real separate
hosts). What it's perfect for is everything else: does the auth
bootstrap work, does the app actually connect and serve, do logs show up
in Grafana, does everything come up from cold with zero internet access.

**Test topology** (all on one machine, one Docker network `backend-network`):
```
casssndra/docker-compose.cluster.yml   5-node Cassandra ring
microservices/docker-compose.yml       catalog-service + ff-net-service + Kong + promtail
  (or backend/ run directly instead, if you're testing the monolith path)
logging/docker-compose.yml             loki + grafana
```

### Step 1 — Actually cut off the internet for the test

Not optional — the whole point is to catch a hidden dependency, and
those hide perfectly well if the machine can quietly reach out and
succeed. Options, easiest first:
- Disconnect Wi-Fi/Ethernet after images are pulled and packages are
  downloaded (Part 2 covers what to pre-fetch).
- Or: `docker network disconnect bridge <container>` per container /
  firewall the host's outbound traffic — more faithful but more setup.
- Bare minimum: watch `docker stats`/host network activity while things
  boot and note anything unexpected reaching out — better than nothing,
  but prefer actually cutting the connection.

### Step 2 — Bring up Cassandra, bootstrap auth

```bash
cd casssndra
docker compose -f docker-compose.cluster.yml up -d
docker exec cassandra-1 nodetool status      # wait for 5x UN
```
Then the full sequence from `AUTH.md` §2: fix `system_auth`'s RF,
rotate the superuser password, run `scripts/create-app-roles.sh` to
create `catalog_app`/`ff_net_app`. Load schema (`cqlsh -u cassandra -p
<rotated password> -f cassandra/schema.sql`, and
`backend/apps/ff_net/submodules/cassandra.sql`).

**This is the step most likely to surface something** — if `cqlsh`,
`nodetool`, or the auth bootstrap silently depend on something reachable
only because your machine still has internet (unlikely, but this is
exactly the kind of thing to distrust until proven), you'll see it here.

### Step 3 — Bring up the app (pick whichever you're actually deploying)

**Microservices:**
```bash
cd microservices
cp services/catalog-service/.env.template services/catalog-service/.env   # fill in CASSANDRA_* incl. USERNAME/PASSWORD
cp services/ff-net-service/.env.template services/ff-net-service/.env
docker compose up -d --build
```
`--build` here is doing real work: it's the first proof that
`pip install -r requirements.txt` and `collectstatic` (which now bakes
in `drf-spectacular-sidecar`'s Swagger UI/Redoc assets, see §Fixed below)
both complete without touching PyPI or a CDN at *runtime* — building the
image still needs internet once (see Part 2 for making even that
optional), but a built image should never need it again.

**Monolith** (if that's the target instead): `cd backend`, fill in
`.env`, `pip install -r requirements.lock.txt` (or your regenerated,
portable version — see Part 2), `python manage.py collectstatic
--noinput`, `python serve.py`.

### Step 4 — Bring up logging

```bash
cd logging
cp .env.template .env
docker compose up -d
cd promtail-monolith   # only if testing the monolith path
cp .env.template .env  # LOKI_URL=http://<this-machine's-IP>:3100
docker compose up -d
```

### Step 5 — Smoke test, with the network still cut

1. **Swagger UI actually renders**: `http://localhost:8080/catalog/api/schema/swagger-ui/`
   (via gateway) or `:8001/api/schema/swagger-ui/` direct. This is the
   one that silently breaks without `drf-spectacular-sidecar` — a blank
   page or console errors about `cdn.jsdelivr.net`/`unpkg.com` failing
   means the sidecar/whitenoise wiring isn't actually working, not just
   a cosmetic issue.
2. **CRUD works**: hit a catalog endpoint (`POST /api/catalog/products/`
   etc.) and an ff_net endpoint, confirm data round-trips through
   Cassandra.
3. **`/health/` reports `ok`**, not `degraded` — confirms the app
   connected to Cassandra *with* the dedicated role's credentials, not
   just that a container is running.
4. **Logs show up in Grafana**: `http://localhost:3000` → Explore → Loki
   → `{service="catalog-service"}` — confirms Promtail found the log
   volumes and Loki ingested them, all without internet.
5. **Repair + checkpoint scripts run clean**: `casssndra/scripts/repair.sh
   cassandra-1`, `scripts/checkpoint.sh cassandra-1` — confirms `nodetool`
   works against the now-authenticated cluster. See
   `casssndra/DISASTER_RECOVERY.md` for the full checkpoint/watermark
   model and whole-cluster rebuild runbook.
6. **Kill a node**: `docker stop cassandra-3`, confirm the app keeps
   answering (LOCAL_QUORUM, 2 of 3 replicas still up), `docker start
   cassandra-3`, confirm it rejoins as `UN` on its own.

If all 6 pass with the network cable out, the *software* is airgap-ready.
Part 2 is what's left — the stuff that's about the transfer itself, not
the code.

---

## Part 2 — Pre-transfer checklist

Everything below has to be true before this leaves an internet-connected
machine. Each item says how to verify it on the single-machine test rig.

### Docker images — every one, pre-pulled and exportable

```
cassandra:5.0.7-bookworm
ipushc/cassandra-web:v1.1.5
kong:3.7
grafana/loki:3.1.1
grafana/grafana:11.2.0
grafana/promtail:3.1.1
python:3.11-slim          (base image for both microservices' Dockerfiles)
```
```bash
docker pull <image>          # for each, on the connected machine
docker save <image> -o <name>.tar
# ... transfer the .tar files to the airgapped side by whatever physical
# media your process uses ...
docker load -i <name>.tar    # on each airgapped host that needs it
```
**Practice this on the single-machine rig**: `docker save` every image
above, `docker rmi` them, `docker load` back from the tarball, confirm
`docker compose up` still works with zero pulls. That's the actual
transfer mechanism you'll use for real — cheaper to find a broken image
tarball now than on-site.

### Host tools — vendored, not just installed here

Whichever host runs `casssndra/scripts/consolidate-checkpoint.sh` /
`verify-restore.sh` (see `DISASTER_RECOVERY.md`) needs **`jq`** on its
`PATH` — a single small static binary, easy to miss since everything
else here is Docker-image-based. Grab it on the connected machine and
carry it over with everything else; don't assume it's already on the
airgapped host.

### Python dependencies — no `pip install` reaching PyPI

**Microservices** (`requirements.txt`, plain pip):
```bash
pip download -r microservices/services/catalog-service/requirements.txt -d wheels/catalog/
pip download -r microservices/services/ff-net-service/requirements.txt -d wheels/ff-net/
# transfer wheels/ alongside the code, then on the airgapped side:
pip install --no-index --find-links=wheels/catalog -r requirements.txt
```
(Or just build the Docker images on the connected machine and transfer
*those* via `docker save` — simpler, since it sidesteps pip entirely on
the airgapped side. Prefer this unless you have a reason to run the
monolith/services outside Docker there.)

**Monolith** (`backend/requirements.lock.txt`): this file is a **conda**
lockfile pinned to local build-cache paths (`D:\bld\...`,
`/home/conda/...`) — it was never portable even to another connected
machine, let alone airgapped. `backend/environment.yml` is the real
source of truth (now includes `drf-spectacular-sidecar` and `whitenoise`
— regenerate `requirements.lock.txt` from it before transfer). For an
airgapped Windows PC, the practical options are: transfer the whole
built conda environment (`conda-pack`, or just copy the
`D:\anaconda\envs\backend5` directory if the target has the same OS/arch),
or containerize the monolith the same way the microservices already are
(a Dockerfile following the same pattern) so it's one `docker save`/`load`
like everything else — recommended if you're setting this up fresh,
since it collapses two different offline-transfer stories into one.

### Fixed this round — confirm on the single-machine test, don't just take it on faith

- **`drf-spectacular` CDN dependency**: previously loaded Swagger
  UI/Redoc's JS/CSS from `cdn.jsdelivr.net` at *page load time* — would
  render blank/broken on any airgapped browser regardless of how well
  the backend itself worked. Fixed via `drf-spectacular-sidecar`
  (vendors the assets) + `whitenoise` (serves them, since nothing else
  in this stack serves static files in production — `DEBUG=False` means
  Django itself won't). Verify: Part 1 §5 item 1, with devtools network
  tab open, confirm zero requests to any external host.
- **Grafana telemetry**: `GF_ANALYTICS_REPORTING_ENABLED`/
  `GF_ANALYTICS_CHECK_FOR_UPDATES` set to `false` (`logging/docker-compose.yml`)
  — otherwise it phones home usage stats and checks for updates on
  every start.
- **Kong telemetry**: `KONG_ANONYMOUS_REPORTS: off`
  (`microservices/docker-compose.yml`) — same idea.
- **Grafana plugins**: `GF_INSTALL_PLUGINS: ""` — already set, just
  don't add a plugin list later without also getting the airgapped side
  the plugin `.zip`s some other way; `GF_INSTALL_PLUGINS` normally
  fetches from grafana.com.

### Secrets and config — generate before you go, never commit

- `DJANGO_SECRET_KEY` (each Django process needs a real one, not the
  Docker build-time placeholder — see the `ENV DJANGO_SECRET_KEY=...`
  line in each Dockerfile, which only covers the `collectstatic` build
  step and gets overridden by `.env` at runtime).
- Cassandra: rotated superuser password, `catalog_app`/`ff_net_app`
  passwords (`AUTH.md`).
- `GRAFANA_ADMIN_PASSWORD`.
- Generate all of these ahead of time, carry them over on the same
  physical media as everything else (or a separate, more controlled
  channel if your policy requires it) — never via git, none of this is
  in a `.env` file that's tracked (`.gitignore` already excludes
  `.env`/`.env.*`).

### Cluster-specific, not covered by the single-machine test

These only show up with real separate hosts, so the single-machine test
*can't* catch them — plan for them separately:
- **Clock sync across the 5 PCs.** Cassandra uses timestamps for
  conflict resolution; meaningful clock skew between nodes causes subtle
  data bugs, not a clean failure. An airgapped LAN has no path to public
  NTP servers — set up a local NTP source (one PC serving time to the
  other 4, or a dedicated time appliance if your environment has one)
  and confirm `w32tm /query /status` (or `timedatectl` on Linux) shows
  small offsets on every PC before trusting write ordering.
- **Static IPs/hostnames match `casssndra/dis/envs/pcNNN.env`.** Verify
  each PC's actual IP against the table in `dis/RUNBOOK.md` — a mismatch
  here is a silent gossip failure, not an obvious error.
- **Firewall rules** — `dis/RUNBOOK.md` §0, run on every PC, before
  section 1's bring-up.
- **Rack-loss / multi-host failure drills** — these need real separate
  machines; `AWS_RUNBOOK.md` §8's checklist is written for exactly this,
  run it there first since AWS is disposable and 5 airgapped PCs aren't.

### Final go/no-go

Don't transfer until: Part 1's 6-item smoke test passes with the network
cable physically out, every image in the list above has a verified-good
tarball, Python deps are resolved one of the two ways above, all secrets
are generated (not defaults), and the cluster-specific items have an
actual plan (not just "we'll figure it out on site").
