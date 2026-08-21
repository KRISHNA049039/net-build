# Cassandra 5-Node Cluster Across Two PCs — Runbook

Cluster: 5 nodes, RF 3 (SimpleStrategy), one ring over the LAN.
- PC .100 (192.168.4.100): cassandra-1 (seed, 7000/9042), cassandra-2 (7001/9043), web UI :8890
- PC .102 (192.168.4.102): cassandra-3 (seed, 7000/9042), cassandra-4 (7001/9043), cassandra-5 (7002/9044)

Golden rules:
- pc100.yml runs ONLY on 192.168.4.100. pc102.yml runs ONLY on 192.168.4.102.
- Always check the machine first:  ipconfig | findstr IPv4
- First-ever startup order: bring up .100 fully (2x UN, no UJ) BEFORE starting .102.
- No --build (image is pulled, nothing to build). Never use "old" copies of the files.

--------------------------------------------------------------------
## 0. Firewall — run once, elevated PowerShell (Admin), on BOTH PCs
--------------------------------------------------------------------
New-NetFirewallRule -DisplayName "Cassandra Internode 7000-7002" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7000-7002
New-NetFirewallRule -DisplayName "Cassandra CQL 9042-9044"      -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9042-9044

# verify rules exist
Get-NetFirewallRule -DisplayName "Cassandra*" | Format-Table DisplayName, Enabled, Direction, Action

--------------------------------------------------------------------
## 1. Bring up the cluster (first time)
--------------------------------------------------------------------
# --- on 192.168.4.100 ---
ipconfig | findstr IPv4
docker compose -f docker-compose.pc100.yml up -d
docker exec cassandra-1 nodetool status      # wait for 2x UN, NO UJ

# --- then on 192.168.4.102 ---
ipconfig | findstr IPv4
docker compose -f docker-compose.pc102.yml up -d
docker logs -f cassandra-3                    # watch it join; Ctrl+C to stop tailing

# --- verify full ring from either PC ---
docker exec cassandra-1 nodetool status       # target: 5x UN (2 on .100, 3 on .102)

--------------------------------------------------------------------
## 2. Connectivity checks (after containers are up)
--------------------------------------------------------------------
# from .100, reach .102:
7000,7001,7002,9042,9043,9044 | ForEach-Object { Test-NetConnection 192.168.4.102 -Port $_ } | Format-Table RemotePort, TcpTestSucceeded
# from .102, reach .100:
7000,7001,9042,9043 | ForEach-Object { Test-NetConnection 192.168.4.100 -Port $_ } | Format-Table RemotePort, TcpTestSucceeded
# True = open. False usually means that node isn't up yet, not a firewall problem.

--------------------------------------------------------------------
## 3. Inspect / debug
--------------------------------------------------------------------
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker logs cassandra-3 --tail 80
docker inspect cassandra-3 --format "{{.State.OOMKilled}}"   # true = out of RAM
docker exec cassandra-1 nodetool status

# remove a ghost/stale node still showing as DN (use its Host ID):
docker exec cassandra-1 nodetool removenode <HOST_ID>

--------------------------------------------------------------------
## 4. Full wipe and restart (POC reset)
--------------------------------------------------------------------
# on .100
docker compose -f docker-compose.pc100.yml down -v
# on .102
docker compose -f docker-compose.pc102.yml down -v
# if anything lingers (run the relevant names per machine):
docker rm -f cassandra-1 cassandra-2 cassandra-3 cassandra-4 cassandra-5
docker volume rm cassandra-data-1 cassandra-data-2 cassandra-data-3 cassandra-data-4 cassandra-data-5
# then repeat section 1 (start .100 first).

--------------------------------------------------------------------
## 5. Create keyspace + load data (once ring is 5x UN)
--------------------------------------------------------------------
docker exec -it cassandra-1 cqlsh
  CREATE KEYSPACE IF NOT EXISTS django_platform
  WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 3};

# schema + seed: run schema.cql, then seed_data.py pointed at 192.168.4.100:9042
