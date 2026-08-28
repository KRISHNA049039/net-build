# Cassandra authentication

Every node used to run with the default `AllowAllAuthenticator` — no
username or password, anyone who could reach port 9042 had full
read/write on every keyspace. This document covers what changed, how to
bootstrap it on a fresh cluster, and the one failure mode that's easy to
get wrong (`system_auth`'s own replication factor).

---

## 1. What changed

`PasswordAuthenticator` + `CassandraAuthorizer` are now enabled on every
node, across all three compose setups (`docker-compose.cluster.yml`,
`docker-compose.djnago.yaml`, `dis/docker-compose.node.yml`) — dev and
the real cluster alike, so an auth-related bug shows up in dev instead of
being the first thing that breaks on the airgapped hardware.

**How it's enabled**: the official Cassandra Docker image has env vars
for DC/rack/seeds/etc., but none for `authenticator`/`authorizer`.
`docker/auth-entrypoint.sh` is a small wrapper — mounted in and set as
each node's `entrypoint:` — that does a targeted `sed` on those two lines
in `cassandra.yaml` (the same technique the image's own entrypoint uses
for its substitutions) and then hands off to the real entrypoint
unchanged. Nothing else about how a node boots is touched.

---

## 2. Bootstrapping a fresh cluster

Do this once, right after the ring is fully formed (`nodetool status` →
all `UN`) and before you point any real application traffic at it:

**1. The default superuser already exists.** Cassandra auto-creates a
`cassandra` / `cassandra` role the first time `system_auth` is
queryable (a few seconds after the first node is up). This is how
`docker-entrypoint.sh`'s own healthcheck and `cassandra-web-ui` connect
by default — see `CASSANDRA_HEALTHCHECK_USER`/`PASSWORD` and
`CASSANDRA_WEBUI_USER`/`PASSWORD` in the compose files (both default to
`cassandra`/`cassandra`).

**2. Fix `system_auth`'s replication factor.** It defaults to RF=1,
completely independent of the RF=3 you set for `django_platform`/
`ans_transformed` — meaning as configured, losing the ONE node holding
the `system_auth` replica locks out authentication for the **entire
cluster**, regardless of how well-replicated your actual data is. This
is the one step that's easy to skip and the one that matters most:

```sql
ALTER KEYSPACE system_auth WITH replication =
  {'class': 'NetworkTopologyStrategy', 'datacenter-1': 3};
```
then run `nodetool repair -- system_auth` on every node (or roll it into
the regular weekly repair from `RECOVERY.md`, `system_auth` isn't special
there, it just also needs the RF fix first).

**3. Rotate the superuser password.** `cassandra`/`cassandra` is a public,
well-known default the moment you enable auth — using it beyond
bootstrap defeats the point:
```sql
ALTER ROLE cassandra WITH PASSWORD = '<new password, keep it out of git>';
```
Then update `CASSANDRA_HEALTHCHECK_PASSWORD`/`CASSANDRA_WEBUI_PASSWORD`
in whichever `.env` files reference it (they default to the old
`cassandra`/`cassandra` — see §3) and redeploy those containers, or point
them at a dedicated low-privilege role instead of the superuser.

**4. Create the application roles.** The app should never connect as the
superuser — `scripts/create-app-roles.sh` creates one role per service,
scoped to exactly the keyspace it owns (reinforcing the same boundary
`microservices/ARCHITECTURE.md` §2 already draws in code):
```bash
CATALOG_APP_PASSWORD='...' FF_NET_APP_PASSWORD='...' \
  ./scripts/create-app-roles.sh cassandra-1 cassandra '<superuser password from step 3>'
```
This creates `catalog_app` (all permissions on `django_platform` only)
and `ff_net_app` (all permissions on `ans_transformed` only). Passwords
are read from the environment and never written to disk by the script.

**5. Point the app(s) at their role.** Set in each service's `.env`:
```
CASSANDRA_USERNAME=catalog_app     # or ff_net_app
CASSANDRA_PASSWORD=<the password from step 4>
```
Leaving `CASSANDRA_USERNAME` empty connects unauthenticated — only valid
if you haven't enabled auth on that cluster at all (there's no partial
state; it's cluster-wide).

---

## 3. Where credentials live

| Consumer | Credential | Where set | Default |
|---|---|---|---|
| Django app (each service) | `catalog_app` / `ff_net_app` | service's `.env` → `CASSANDRA_USERNAME`/`PASSWORD` → `cassandra_session.py`'s `PlainTextAuthProvider` | none (must set) |
| Container healthcheck | superuser (until rotated) | node's `.env` → `CASSANDRA_HEALTHCHECK_USER`/`PASSWORD` | `cassandra`/`cassandra` |
| `cassandra-web-ui` | superuser (until rotated) | compose `.env` → `CASSANDRA_WEBUI_USER`/`PASSWORD` | `cassandra`/`cassandra` |

Nothing here is committed to git — every `*_PASSWORD` is read from an
`.env` file, all of which are gitignored (`.env`/`.env.*`, see
`.gitignore`). `create-app-roles.sh` takes passwords as environment
variables at run time, never writes them to a file.

---

## 4. Why healthcheck/web-UI still default to the superuser

Using a freshly-created dedicated role for the healthcheck would create
a bootstrap deadlock: `cassandra-2`'s `depends_on: cassandra-1:
condition: service_healthy` has to pass before `cassandra-2` even starts,
but a dedicated role can only be created *after* enough of the ring is up
to run `cqlsh` against it — chicken, egg. The built-in superuser exists
from the first node's first boot, so the healthcheck works immediately
with zero manual steps, and you rotate its password once the ring is up
(§2 step 3) rather than trying to avoid ever using it.

---

## 5. Driver behavior

`apps/core/cassandra_session.py` (in `backend/` and both copies under
`microservices/services/*/apps/core/`) builds a `PlainTextAuthProvider`
only if `CASSANDRA_USERNAME` is non-empty:
```python
auth_provider = (
    PlainTextAuthProvider(username=cfg["USERNAME"], password=cfg["PASSWORD"])
    if cfg["USERNAME"]
    else None
)
```
So the same code path supports both an auth-enabled cluster (the real
target) and a plain unauthenticated one (only if you've deliberately
skipped enabling `PasswordAuthenticator` there) without a code change —
just whether `.env` sets a username.

---

## 6. Related docs

- [`RECOVERY.md`](RECOVERY.md) — replication/repair/backup; `system_auth`'s
  RF fix (§2 step 2 above) is the auth-specific piece of the same "what
  survives a node going down" story.
- [`RUNBOOK.md`](dis/RUNBOOK.md) / [`AWS_RUNBOOK.md`](AWS_RUNBOOK.md) — the
  bootstrap steps above are step 6 in the first-time bring-up sequence.
