# Recovery strategy

Cluster: 5 nodes, 2 racks (rack1: cassandra-1/2/3, rack2: cassandra-4/5),
`datacenter-1`, `NetworkTopologyStrategy` RF=3 on both keyspaces
(`django_platform`, `ans_transformed`). App reads/writes at `LOCAL_QUORUM`
(`backend/.env` -> `CASSANDRA_CONSISTENCY_LEVEL`, applied in
`apps/core/cassandra_session.py`).

## What RF=3 + LOCAL_QUORUM actually buys you

- Any **1 node down**: reads/writes still succeed (2 of 3 replicas ack).
- With replicas spread across the 2 racks by `NetworkTopologyStrategy`,
  losing **1 whole rack** still leaves at least 1 replica of every
  partition reachable, but LOCAL_QUORUM (2 of 3) will fail for partitions
  whose 2-of-3 replica majority sat in the rack that just went down --
  expect a partial availability hit, not total. Confirm this empirically
  in the AWS test (see `AWS_RUNBOOK.md`, section "Rack-loss drill").
- Any **2 nodes down** (or 1 rack + 1 more node): LOCAL_QUORUM writes to
  the affected ranges fail until a node comes back or is replaced.

## Hinted handoff (automatic, first line of defense)

Enabled by default, covers outages under `max_hint_window_in_ms`
(default 3h): the coordinator stores missed writes and replays them when
the node returns. No action needed for short restarts/maintenance
windows. For a planned outage longer than 3h, either raise
`max_hint_window_in_ms` beforehand or plan on running repair (below)
after the node rejoins.

## Repair (weekly, required)

Anything hints don't cover (outage > hint window, or a node that was
replaced with an empty disk) only gets reconciled by anti-entropy repair.
Run `scripts/repair.sh <container-name>` on every node weekly, staggered
so they don't overlap:

```
0 1 * * 0  scripts/repair.sh cassandra-1   # Sun 01:00 UTC
0 3 * * 0  scripts/repair.sh cassandra-2   # Sun 03:00 UTC
0 5 * * 0  scripts/repair.sh cassandra-3   # Sun 05:00 UTC
0 7 * * 0  scripts/repair.sh cassandra-4   # Sun 07:00 UTC
0 9 * * 0  scripts/repair.sh cassandra-5   # Sun 09:00 UTC
```

On the airgapped Windows hosts, wire the same `docker exec <node> nodetool
repair -pr` command into a weekly Task Scheduler task instead of cron.

Always run a manual repair immediately after: replacing a node, an outage
longer than the hint window, or restoring from a snapshot.

## Backup / restore

**Backup** (`scripts/backup.sh <container-name> [backup-dir]`): snapshots
every keyspace via `nodetool snapshot`, tars it out of the container, then
clears the on-node snapshot. Copy the resulting tarball off the host
immediately (rsync to a separate backup host on the airgapped LAN, `aws
s3 cp` for the AWS test) -- until you do, it's on the same disk as the
live data and isn't a real backup yet.

**Restore** a single node from a snapshot tarball:

1. `docker compose --env-file envs/<node>.env -f docker-compose.node.yml stop <container-name>`
2. Extract the tarball; for each `<keyspace>/<table>-<uuid>/snapshots/<tag>/`
   directory, copy its SSTable files into the *live* table directory
   (`/var/lib/cassandra/data/<keyspace>/<table>-<uuid>/`), replacing what's
   there.
3. Start the node back up.
4. Run `nodetool repair -pr` on it (step above) -- the rest of the
   cluster kept moving during the restore window, so the restored node is
   stale until repair reconciles it against the other 2 replicas.

Because RF=3, you rarely need this for a single node -- rebuilding from
the other replicas (below) is usually simpler than restoring a snapshot.

## Replacing a dead node (disk loss, unrecoverable host)

1. Provision the replacement host with the same `CASSANDRA_RACK` as the
   node it's replacing (keeps rack balance intact), empty data volume.
2. Start it with `JVM_EXTRA_OPTS=-Dcassandra.replace_address_first_boot=<dead-node-broadcast-ip>`
   added to its env file. Cassandra streams the missing data from the
   other 2 replicas of every range instead of you restoring a backup.
3. Watch it join: `docker exec cassandra-1 nodetool status` until it
   shows `UN` (Up/Normal) instead of `UJ` (joining).
4. Run repair on it and its neighbors once fully joined.

## Rebuilding from scratch (POC reset only)

`docker compose --env-file envs/<node>.env -f docker-compose.node.yml down -v`
per host destroys that node's data volume permanently. Only use this for
a deliberate full reset, not as a substitute for the restore/replace
procedures above.
