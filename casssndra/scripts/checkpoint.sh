#!/usr/bin/env bash
# Snapshot this node, export it, hash it, and record a WATERMARK -- a
# small JSON file next to the tarball saying exactly what was captured,
# when, and its checksum. consolidate-checkpoint.sh (run on the backup
# host) reads these across all 5 nodes to decide whether a given tag is
# a genuine, verified, cluster-wide recovery point.
#
# Supersedes the old backup.sh -- same core snapshot/export logic, plus
# the watermark and optional off-site push.
#
# Usage: checkpoint.sh <container-name> [backup-dir] [tag]
#   backup-dir defaults to ./backups
#   tag defaults to now (UTC) -- but pass the SAME tag to every node's
#   invocation when running a coordinated cluster-wide checkpoint (see
#   ../DISASTER_RECOVERY.md), so all 5 nodes' watermarks line up as one
#   recovery point instead of 5 unrelated ones.
#
# Env vars:
#   CHECKPOINT_REMOTE   e.g. backupuser@backuphost:/backups/cassandra/
#                       if set, scp's the tarball + watermark there after
#                       a successful local export. This is NOT the same
#                       as true off-site -- see DISASTER_RECOVERY.md.
set -euo pipefail

NODE_NAME="${1:?usage: checkpoint.sh <container-name> [backup-dir] [tag]}"
BACKUP_DIR="${2:-./backups}"
TAG="${3:-$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p "$BACKUP_DIR"
echo "[$(date -u +%FT%TZ)] checkpoint start: $NODE_NAME tag=$TAG"

docker exec "$NODE_NAME" nodetool snapshot -t "$TAG"

OUT="$BACKUP_DIR/${NODE_NAME}-${TAG}.tar.gz"
docker exec "$NODE_NAME" bash -c \
  "cd /var/lib/cassandra/data && tar czf - \$(find . -type d -path \"*/snapshots/$TAG\")" \
  > "$OUT"

SHA256="$(sha256sum "$OUT" | cut -d' ' -f1)"
SIZE="$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT")"
KEYSPACES="$(docker exec "$NODE_NAME" bash -c \
  "cd /var/lib/cassandra/data && find . -maxdepth 1 -mindepth 1 -type d -printf '%f\n'" \
  | tr '\n' ',' | sed 's/,$//')"

WATERMARK="$BACKUP_DIR/${NODE_NAME}-${TAG}.json"
cat > "$WATERMARK" <<JSON
{
  "node": "$NODE_NAME",
  "tag": "$TAG",
  "completed_at": "$(date -u +%FT%TZ)",
  "keyspaces": "$KEYSPACES",
  "tarball": "$(basename "$OUT")",
  "sha256": "$SHA256",
  "size_bytes": $SIZE
}
JSON
echo "[$(date -u +%FT%TZ)] checkpoint exported: $OUT (sha256 $SHA256)"

docker exec "$NODE_NAME" nodetool clearsnapshot -t "$TAG"
echo "[$(date -u +%FT%TZ)] on-node snapshot cleared (kept only in $OUT)"

if [ -n "${CHECKPOINT_REMOTE:-}" ]; then
  echo "[$(date -u +%FT%TZ)] pushing to $CHECKPOINT_REMOTE"
  scp "$OUT" "$WATERMARK" "$CHECKPOINT_REMOTE"
else
  echo "NOTE: CHECKPOINT_REMOTE not set -- this checkpoint is still only on"
  echo "      this host's disk, same as the live data. Not a real recovery"
  echo "      point until it's somewhere else. See ../DISASTER_RECOVERY.md."
fi
