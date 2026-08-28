#!/usr/bin/env bash
# Run on the backup host (wherever CHECKPOINT_REMOTE in checkpoint.sh
# points, or wherever you've manually collected the per-node tarballs +
# watermarks). Groups per-node watermarks by tag, verifies each
# tarball's checksum against its watermark, and marks a tag COMPLETE in
# manifest.json only once every expected node has a verified entry for
# it -- that completeness is the actual disaster-recovery watermark:
# the most recent complete tag is your true current recovery point, not
# just "the most recent backup that happened to run somewhere."
#
# Usage: consolidate-checkpoint.sh <backup-dir> [comma,separated,node,list]
# Requires: jq
set -euo pipefail

BACKUP_DIR="${1:?usage: consolidate-checkpoint.sh <backup-dir> [node1,node2,...]}"
EXPECTED_NODES="${2:-cassandra-1,cassandra-2,cassandra-3,cassandra-4,cassandra-5}"
MANIFEST="$BACKUP_DIR/manifest.json"

command -v jq >/dev/null || { echo "jq is required -- see ../../AIRGAP_TESTING.md"; exit 1; }

[ -f "$MANIFEST" ] || echo '{"checkpoints": []}' > "$MANIFEST"

# All distinct tags seen among the watermark files present.
TAGS="$(jq -r '.tag' "$BACKUP_DIR"/*.json 2>/dev/null | sort -u)"

for TAG in $TAGS; do
  PRESENT_NODES=""
  ALL_VERIFIED=true

  for WM in "$BACKUP_DIR"/*-"$TAG".json; do
    [ -f "$WM" ] || continue
    NODE="$(jq -r '.node' "$WM")"
    TARBALL="$BACKUP_DIR/$(jq -r '.tarball' "$WM")"
    EXPECTED_SHA="$(jq -r '.sha256' "$WM")"

    if [ ! -f "$TARBALL" ]; then
      echo "[$TAG] $NODE: MISSING tarball $TARBALL -- not counted"
      ALL_VERIFIED=false
      continue
    fi
    ACTUAL_SHA="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
    if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
      echo "[$TAG] $NODE: CHECKSUM MISMATCH (corrupt transfer or tampering) -- not counted"
      ALL_VERIFIED=false
      continue
    fi
    PRESENT_NODES="${PRESENT_NODES}${PRESENT_NODES:+,}$NODE"
  done

  # Set comparison: every expected node present, order-independent.
  COMPLETE=true
  IFS=',' read -ra EXP_ARR <<< "$EXPECTED_NODES"
  for N in "${EXP_ARR[@]}"; do
    case ",$PRESENT_NODES," in *",$N,"*) ;; *) COMPLETE=false ;; esac
  done
  [ "$ALL_VERIFIED" = true ] || COMPLETE=false

  jq --arg tag "$TAG" \
     --arg present "$PRESENT_NODES" \
     --arg expected "$EXPECTED_NODES" \
     --argjson complete "$COMPLETE" \
     --arg now "$(date -u +%FT%TZ)" '
    .checkpoints |= (map(select(.tag != $tag)) + [{
      tag: $tag,
      nodes_expected: ($expected | split(",")),
      nodes_present: ($present | split(",") | map(select(. != ""))),
      complete: $complete,
      consolidated_at: $now
    }])
  ' "$MANIFEST" > "$MANIFEST.tmp" && mv "$MANIFEST.tmp" "$MANIFEST"

  echo "[$TAG] complete=$COMPLETE nodes=$PRESENT_NODES"
done

echo
LATEST_COMPLETE="$(jq -r '[.checkpoints[] | select(.complete)] | sort_by(.tag) | last | .tag // "NONE"' "$MANIFEST")"
if [ "$LATEST_COMPLETE" = "NONE" ]; then
  echo "WATERMARK: no complete cluster checkpoint exists yet."
else
  echo "WATERMARK: latest complete checkpoint = $LATEST_COMPLETE"
  echo "This is your current true recovery point -- anything written after"
  echo "it is not covered by any verified, complete backup."
fi
