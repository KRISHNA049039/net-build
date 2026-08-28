#!/usr/bin/env bash
# Automated restore-verification drill: takes the latest COMPLETE
# checkpoint from manifest.json (or a specific tag), extracts every
# node's tarball, and runs Cassandra's own sstableverify tool against
# the SSTable data -- checksums and structure, the same check Cassandra
# itself would do reading them for real. Closes the "untested backups
# aren't real backups" gap: this actually opens the files, not just
# checks they exist.
#
# What this does NOT prove: that a live node can rejoin the cluster and
# serve traffic from this data (auth roles, gossip state, and token
# ownership aren't exercised by this check). Do a full live restore
# drill by hand periodically too -- see DISASTER_RECOVERY.md.
#
# Usage: verify-restore.sh <backup-dir> [tag]
# Requires: jq, docker
set -euo pipefail

BACKUP_DIR="${1:?usage: verify-restore.sh <backup-dir> [tag]}"
MANIFEST="$BACKUP_DIR/manifest.json"
IMAGE="cassandra:5.0.7-bookworm"

command -v jq >/dev/null || { echo "jq is required -- see ../../AIRGAP_TESTING.md"; exit 1; }

TAG="${2:-}"
if [ -z "$TAG" ]; then
  [ -f "$MANIFEST" ] || { echo "No manifest.json -- run consolidate-checkpoint.sh first."; exit 1; }
  TAG="$(jq -r '[.checkpoints[] | select(.complete)] | sort_by(.tag) | last | .tag // empty' "$MANIFEST")"
  [ -n "$TAG" ] || { echo "No complete checkpoint in manifest.json -- nothing to verify."; exit 1; }
fi
echo "Verifying checkpoint tag: $TAG"

FAIL=0
for WM in "$BACKUP_DIR"/*-"$TAG".json; do
  [ -f "$WM" ] || continue
  NODE="$(jq -r '.node' "$WM")"
  TARBALL="$BACKUP_DIR/$(jq -r '.tarball' "$WM")"
  SCRATCH="$(mktemp -d)"
  trap 'rm -rf "$SCRATCH"' EXIT

  echo "--- $NODE ---"
  tar xzf "$TARBALL" -C "$SCRATCH"

  # Reassemble snapshot dirs into the layout sstableverify expects
  # (<keyspace>/<table>-<uuid>/*.db, not nested under snapshots/<tag>/).
  RESTORED="$SCRATCH/restored"
  mkdir -p "$RESTORED"
  find "$SCRATCH" -type d -path "*/snapshots/$TAG" | while read -r SNAP_DIR; do
    TABLE_DIR="$(dirname "$(dirname "$SNAP_DIR")")"          # .../<keyspace>/<table>-<uuid>
    KEYSPACE="$(basename "$(dirname "$TABLE_DIR")")"
    TABLE="$(basename "$TABLE_DIR")"
    mkdir -p "$RESTORED/$KEYSPACE/$TABLE"
    cp "$SNAP_DIR"/*.db "$RESTORED/$KEYSPACE/$TABLE/" 2>/dev/null || true
  done

  if ! docker run --rm -v "$RESTORED:/var/lib/cassandra/data:ro" "$IMAGE" \
        bash -c '
          set -e
          cd /var/lib/cassandra/data
          for ks in */; do
            ks="${ks%/}"
            for tbl in "$ks"/*/; do
              tbl="${tbl%/}"
              cfname="$(basename "$tbl" | sed "s/-[0-9a-f]\{32\}$//")"
              echo "verifying $ks.$cfname"
              sstableverify "$ks" "$cfname" || exit 1
            done
          done
        '
  then
    echo "$NODE: FAILED sstableverify"
    FAIL=1
  else
    echo "$NODE: OK"
  fi
done

echo
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: checkpoint $TAG verified OK on all nodes."
else
  echo "RESULT: checkpoint $TAG FAILED verification -- do not rely on it."
  echo "Investigate before assuming any older complete checkpoint is safe either."
fi
exit "$FAIL"
