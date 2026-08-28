#!/usr/bin/env bash
# Anti-entropy repair for this host's Cassandra node.
#
# Hinted handoff only covers outages shorter than max_hint_window (default
# 3h) -- anything longer, or any write missed while a node was fully down,
# only gets reconciled by repair. Run this weekly per node, staggered
# across the 5 hosts so repairs don't overlap and saturate the cluster.
#
# Cron example (this host is cassandra-1, Sundays 01:00 UTC; stagger the
# other 4 hosts by +2h each -- 01:00, 03:00, 05:00, 07:00, 09:00):
#   0 1 * * 0  /opt/casssndra/scripts/repair.sh cassandra-1 >> /var/log/cassandra-repair.log 2>&1
set -euo pipefail
NODE_NAME="${1:?usage: repair.sh <container-name>}"

echo "[$(date -u +%FT%TZ)] repair start: $NODE_NAME"
# -pr: only repair this node's primary token range -- run on every node so
# the union of runs covers the whole ring exactly once, instead of each
# node repairing all its replicated ranges (which triples the work at RF=3).
docker exec "$NODE_NAME" nodetool repair -pr
echo "[$(date -u +%FT%TZ)] repair done: $NODE_NAME"
