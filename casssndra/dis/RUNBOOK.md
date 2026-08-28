# Cassandra 5-Node Cluster Across 5 PCs — Airgapped Runbook

Cluster: 5 nodes, 1 per physical PC, 2 racks, RF=3 (`NetworkTopologyStrategy`).
See `../RECOVERY.md` for repair/backup/restore and what RF=3 actually
protects against. See `../AWS_RUNBOOK.md` for the AWS test environment
this was validated against before running it here.

| PC          | Node        | Rack  | Seed? |
|-------------|-------------|-------|-------|
| 192.168.4.100 | cassandra-1 | rack1 | yes |
| 192.168.4.101 | cassandra-2 | rack1 | |
| 192.168.4.102 | cassandra-3 | rack1 | |
| 192.168.4.103 | cassandra-4 | rack2 | yes |
| 192.168.4.104 | cassandra-5 | rack2 | |

Every PC runs the same `docker-compose.node.yml`; only the `envs/pcNNN.env`
file passed via `--env-file` differs. If your PCs' IPs don't match the
table, edit the matching `envs/pcNNN.env` file's `BIND_IP` (and every
node's `SEED_IPS`, if you change a seed's IP) before starting anything.

Golden rules:
- `envs/pc100.env` runs ONLY on 192.168.4.100, `pc101.env` ONLY on
  192.168.4.101, etc. Always check the machine first: `ipconfig | findstr IPv4`.
- First-ever startup order: bring up the 2 seeds first (pc100, pc103),
  confirm each is `UN` in `nodetool status`, then bring up the rest.
- No `--build` (image is pulled, nothing to build).

--------------------------------------------------------------------
## 0. Firewall — run once, elevated PowerShell (Admin), on EVERY PC
--------------------------------------------------------------------
```
New-NetFirewallRule -DisplayName "Cassandra Internode 7000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7000
New-NetFirewallRule -DisplayName "Cassandra CQL 9042"       -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9042

# verify
Get-NetFirewallRule -DisplayName "Cassandra*" | Format-Table DisplayName, Enabled, Direction, Action
```

--------------------------------------------------------------------
## 1. Bring up the cluster (first time)
--------------------------------------------------------------------
```
# --- on 192.168.4.100 ---
ipconfig | findstr IPv4
docker compose --env-file envs/pc100.env -f docker-compose.node.yml up -d
docker exec cassandra-1 nodetool status      # wait for 1x UN

# --- on 192.168.4.103 (2nd seed) ---
ipconfig | findstr IPv4
docker compose --env-file envs/pc103.env -f docker-compose.node.yml up -d
docker exec cassandra-4 nodetool status      # wait for 2x UN, no UJ

# --- then on 192.168.4.101, .102, .104 (any order) ---
docker compose --env-file envs/pcNNN.env -f docker-compose.node.yml up -d
docker logs -f cassandraN                    # watch it join; Ctrl+C to stop tailing

# --- verify full ring from any PC ---
docker exec cassandra-1 nodetool status      # target: 5x UN, 3 on rack1, 2 on rack2
```

--------------------------------------------------------------------
## 2. Connectivity checks (after containers are up)
--------------------------------------------------------------------
```
# from any PC, reach every other PC:
7000,9042 | ForEach-Object { Test-NetConnection 192.168.4.101 -Port $_ } | Format-Table RemotePort, TcpTestSucceeded
# repeat for each other PC's IP. True = open. False usually means that
# node isn't up yet, not a firewall problem.
```

--------------------------------------------------------------------
## 3. Inspect / debug
--------------------------------------------------------------------
```
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker logs cassandra-3 --tail 80
docker inspect cassandra-3 --format "{{.State.OOMKilled}}"   # true = out of RAM
docker exec cassandra-1 nodetool status

# remove a ghost/stale node still showing as DN (use its Host ID):
docker exec cassandra-1 nodetool removenode <HOST_ID>
```

--------------------------------------------------------------------
## 4. Full wipe and restart (POC reset — destroys all data)
--------------------------------------------------------------------
```
# on each PC:
docker compose --env-file envs/pcNNN.env -f docker-compose.node.yml down -v
```
Then repeat section 1 (seeds first: pc100, then pc103, then the rest).

--------------------------------------------------------------------
## 5. Create keyspaces + load data (once ring is 5x UN)
--------------------------------------------------------------------
Auth is enabled from first boot (see `../AUTH.md`) — every `cqlsh` call
needs credentials, `-u cassandra -p cassandra` (the built-in superuser)
until you rotate it in section 6 below.
```
docker cp ../cassandra/schema.sql cassandra-1:/schema.sql
docker cp ../../backend/apps/ff_net/submodules/cassandra.sql cassandra-1:/ff_net.sql
docker exec -it cassandra-1 cqlsh -u cassandra -p cassandra -f /schema.sql
docker exec -it cassandra-1 cqlsh -u cassandra -p cassandra -f /ff_net.sql
```
Both keyspaces are `NetworkTopologyStrategy` / `datacenter-1: 3` — see
`schema.sql` and `apps/ff_net/submodules/cassandra.sql` for the source of
truth, don't hand-type the CQL here (that's how this drifted before).

Then seed data (from a machine with network access to the ring):
```
CASSANDRA_HOST=192.168.4.100 python ../cassandra/seed_data.py 20
```
Any node works as the initial contact point — the driver discovers the
rest via `system.peers`.

--------------------------------------------------------------------
## 6. Authentication bootstrap (once, right after section 5)
--------------------------------------------------------------------
Fix `system_auth`'s replication factor, rotate the superuser password,
and create per-service roles (`catalog_app`, `ff_net_app`) — full steps
in `../AUTH.md`. Do this before pointing real application traffic at the
cluster; skipping the `system_auth` RF fix means losing one node can
lock out authentication cluster-wide, RF=3 everywhere else notwithstanding.

--------------------------------------------------------------------
## 7. Repair / backup / restore
--------------------------------------------------------------------
See `../RECOVERY.md`.
