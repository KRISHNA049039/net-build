#!/usr/bin/env bash
# Snapshot every keyspace/table on this node and export the snapshot as a
# tarball on the host. Run before any risky operation (schema change,
# repair after a long outage, Cassandra version upgrade) and on a nightly
# schedule. Restore procedure: see ../RECOVERY.md
#
# Usage: backup.sh <container-name> [backup-dir]
set -euo pipefail
NODE_NAME="${1:?usage: backup.sh <container-name> [backup-dir]}"
BACKUP_DIR="${2:-./backups}"
TAG="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"
echo "[$(date -u +%FT%TZ)] snapshot $NODE_NAME -> tag $TAG"
docker exec "$NODE_NAME" nodetool snapshot -t "$TAG"

OUT="$BACKUP_DIR/${NODE_NAME}-${TAG}.tar.gz"
docker exec "$NODE_NAME" bash -c \
  "cd /var/lib/cassandra/data && tar czf - \$(find . -type d -path \"*/snapshots/$TAG\")" \
  > "$OUT"
echo "[$(date -u +%FT%TZ)] snapshot exported: $OUT"

# Copy $OUT off this host now (rsync/scp to a backup host on the airgapped
# LAN, or `aws s3 cp` for the AWS test) -- it lives on the same disk as the
# live data until you do, which defeats the point of a backup.

docker exec "$NODE_NAME" nodetool clearsnapshot -t "$TAG"
echo "[$(date -u +%FT%TZ)] on-node snapshot cleared (kept only in $OUT)"
