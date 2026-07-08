#!/usr/bin/env bash
#
# upload-book.sh - push a third-party (Marker/Datalab) book extraction to the
# grimoire S3 bucket, in the layout the loader expects.
#
#   s3://grimoire/books/<book-id>/raw/output.json.gz     (verbatim, gzipped)
#   s3://grimoire/books/<book-id>/raw/output.metadata.json
#   s3://grimoire/books/<book-id>/raw/img/<hash>_img.jpg
#   s3://grimoire/books/<book-id>/chunks/chunks.ndjson    (if present in <dir>)
#
# The chunks NDJSON is produced separately by the converter:
#   python3 projects/monolith/grimoire/marker.py <dir>/output.json \
#       --book-id <book-id> -o <dir>/chunks.ndjson
#
# SeaweedFS S3 is in-cluster only (ClusterIP seaweedfs-s3:8333), so this script
# opens a kubectl port-forward for the duration of the upload and tears it down
# on exit. Requires: rclone, kubectl (pointed at the homelab cluster), gzip.
#
# Usage: upload-book.sh <book-id> <local-book-dir>
set -euo pipefail

BOOK_ID="${1:?usage: upload-book.sh <book-id> <local-book-dir>}"
SRC_DIR="${2:?usage: upload-book.sh <book-id> <local-book-dir>}"

BUCKET="${GRIMOIRE_S3_BUCKET:-grimoire}"
S3_NS="${SEAWEEDFS_NS:-seaweedfs}"
S3_SVC="${SEAWEEDFS_SVC:-seaweedfs-s3}"
S3_PORT="${SEAWEEDFS_S3_PORT:-8333}"
LOCAL_PORT="${LOCAL_S3_PORT:-8333}"

# rclone SeaweedFS remote, configured entirely via env (no rclone config file
# needed). Path-style + a dummy region keep the S3 client happy; the shared
# duckdb/duckdb identity matches SEAWEEDFS_S3 creds on the monolith deployment.
export RCLONE_CONFIG_GRIM_TYPE=s3
export RCLONE_CONFIG_GRIM_PROVIDER=Other
export RCLONE_CONFIG_GRIM_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:-duckdb}"
export RCLONE_CONFIG_GRIM_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:-duckdb}"
export RCLONE_CONFIG_GRIM_ENDPOINT="http://localhost:${LOCAL_PORT}"
export RCLONE_CONFIG_GRIM_REGION="us-east-1"
export RCLONE_CONFIG_GRIM_FORCE_PATH_STYLE=true

DEST="GRIM:${BUCKET}/books/${BOOK_ID}"

[ -d "$SRC_DIR" ] || {
	echo "source dir not found: $SRC_DIR" >&2
	exit 1
}
[ -f "$SRC_DIR/output.json" ] || {
	echo "output.json not found in $SRC_DIR" >&2
	exit 1
}

TMP="$(mktemp -d)"
PF_PID=""
cleanup() {
	[ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
	rm -rf "$TMP"
}
trap cleanup EXIT

echo "port-forwarding ${S3_NS}/${S3_SVC}:${S3_PORT} -> localhost:${LOCAL_PORT} ..." >&2
kubectl port-forward -n "$S3_NS" "svc/${S3_SVC}" "${LOCAL_PORT}:${S3_PORT}" >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 40); do
	if nc -z localhost "$LOCAL_PORT" 2>/dev/null; then break; fi
	sleep 0.25
done

# Create the bucket if the identity is allowed to (SeaweedFS makes it on first
# write for an admin identity); ignore "already exists".
rclone mkdir "GRIM:${BUCKET}" 2>/dev/null || true

echo "archiving raw -> ${BUCKET}/books/${BOOK_ID}/raw/ ..." >&2
gzip -c "$SRC_DIR/output.json" >"$TMP/output.json.gz"
rclone copyto "$TMP/output.json.gz" "${DEST}/raw/output.json.gz"
if [ -f "$SRC_DIR/output.metadata.json" ]; then
	rclone copyto "$SRC_DIR/output.metadata.json" "${DEST}/raw/output.metadata.json"
fi
# Cropped illustrations (flat in the source dir) -> raw/img/. Match any image
# extension: image_ref is built from the verbatim filename Marker emitted in the
# <img src>, which is not guaranteed to be *_img.jpg for every book/vendor.
rclone copy "$SRC_DIR" "${DEST}/raw/img/" \
	--include "*.jpg" --include "*.jpeg" --include "*.png" \
	--include "*.gif" --include "*.webp"

if [ -f "$SRC_DIR/chunks.ndjson" ]; then
	echo "uploading chunks -> ${BUCKET}/books/${BOOK_ID}/chunks/ ..." >&2
	rclone copyto "$SRC_DIR/chunks.ndjson" "${DEST}/chunks/chunks.ndjson"
else
	echo "note: no chunks.ndjson in $SRC_DIR (run marker.py first to load this book)" >&2
fi

echo "done. raw archived; image_ref base = s3://${BUCKET}/books/${BOOK_ID}/raw/img/" >&2
