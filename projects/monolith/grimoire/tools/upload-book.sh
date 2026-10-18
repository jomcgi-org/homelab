#!/usr/bin/env bash
#
# upload-book.sh - push a third-party (Marker/Datalab) book extraction to the
# grimoire S3 bucket, in the layout the loader expects.
#
#   s3://<GRIMOIRE_S3_BUCKET>/books/<book-id>/raw/output.json.gz (verbatim, gzipped)
#   s3://<GRIMOIRE_S3_BUCKET>/books/<book-id>/raw/output.metadata.json
#   s3://<GRIMOIRE_S3_BUCKET>/books/<book-id>/raw/img/<original filename>
#   s3://<GRIMOIRE_S3_BUCKET>/books/<book-id>/chunks/chunks.ndjson (if present)
#
# The chunks NDJSON is produced separately by the converter:
#   python3 projects/monolith/grimoire/marker.py <dir>/output.json \
#       --book-id <book-id> -o <dir>/chunks.ndjson
#
# Uses the configured HTTPS R2 endpoint and existing monolith-r2-s3 identity.
# See README.md here for operator-only credential injection and configuration.
# Requires: rclone, gzip. The destination bucket must already exist.
#
# Usage: upload-book.sh <book-id> <local-book-dir>
set +x
set -euo pipefail

fail() {
	echo "upload-book: $1" >&2
	exit 1
}

[ "$#" -eq 2 ] || fail "usage: upload-book.sh <book-id> <local-book-dir>"
BOOK_ID="$1"
SRC_DIR="$2"
case "$BOOK_ID" in
"" | . | .. | */*) fail "book-id must be a nonempty single path component" ;;
esac
[ -d "$SRC_DIR" ] || fail "source directory not found"
[ -r "$SRC_DIR/output.json" ] && [ -f "$SRC_DIR/output.json" ] || fail "readable output.json required"
for optional in output.metadata.json chunks.ndjson; do
	if [ -e "$SRC_DIR/$optional" ]; then
		[ -f "$SRC_DIR/$optional" ] && [ -r "$SRC_DIR/$optional" ] || fail "optional $optional must be a readable file"
	fi
done

# Validate before any external command, and never print credential values.
for required in SEAWEEDFS_S3_ENDPOINT AWS_REGION GRIMOIRE_S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY; do
	[ -n "${!required:-}" ] || fail "$required is required"
done
[[ "$SEAWEEDFS_S3_ENDPOINT" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?/?$ ]] || fail "SEAWEEDFS_S3_ENDPOINT must be an HTTPS endpoint without a path or credentials"
case "$GRIMOIRE_S3_BUCKET" in
*/* | *:*) fail "GRIMOIRE_S3_BUCKET must be a bucket name" ;;
esac
for tool in rclone gzip mktemp; do
	command -v "$tool" >/dev/null || fail "$tool is required"
done
# An absolute source also disambiguates local paths containing a colon.
SRC_DIR="$(cd -- "$SRC_DIR" && pwd -P)"
BUCKET="$GRIMOIRE_S3_BUCKET"

# The remote exists only in this process environment. Disable bucket creation
# even inside rclone's copy operations, and never read a user's config file.
export RCLONE_CONFIG_GRIM_TYPE=s3
export RCLONE_CONFIG_GRIM_PROVIDER=Cloudflare
export RCLONE_CONFIG_GRIM_ACCESS_KEY_ID="$S3_ACCESS_KEY_ID"
export RCLONE_CONFIG_GRIM_SECRET_ACCESS_KEY="$S3_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_GRIM_ENDPOINT="$SEAWEEDFS_S3_ENDPOINT"
export RCLONE_CONFIG_GRIM_REGION="$AWS_REGION"
export RCLONE_CONFIG_GRIM_FORCE_PATH_STYLE=true
upload() {
	rclone --config /dev/null --s3-no-check-bucket "$@"
}

DEST="GRIM:${BUCKET}/books/${BOOK_ID}"

UPLOAD_TMP="$(mktemp -d)"
cleanup() {
	rm -rf -- "$UPLOAD_TMP"
}
trap cleanup EXIT

echo "archiving raw -> ${BUCKET}/books/${BOOK_ID}/raw/ ..." >&2
gzip -c "$SRC_DIR/output.json" >"$UPLOAD_TMP/output.json.gz"
upload copyto "$UPLOAD_TMP/output.json.gz" "${DEST}/raw/output.json.gz"
if [ -f "$SRC_DIR/output.metadata.json" ]; then
	upload copyto "$SRC_DIR/output.metadata.json" "${DEST}/raw/output.metadata.json"
fi
# Cropped illustrations (flat in the source dir) -> raw/img/. Match any image
# extension: image_ref is built from the verbatim filename Marker emitted in the
# <img src>, which is not guaranteed to be *_img.jpg for every book/vendor.
upload copy "$SRC_DIR" "${DEST}/raw/img/" \
	--include "*.jpg" --include "*.jpeg" --include "*.png" \
	--include "*.gif" --include "*.webp"

if [ -f "$SRC_DIR/chunks.ndjson" ]; then
	echo "uploading chunks -> ${BUCKET}/books/${BOOK_ID}/chunks/ ..." >&2
	upload copyto "$SRC_DIR/chunks.ndjson" "${DEST}/chunks/chunks.ndjson"
else
	echo "note: no chunks.ndjson in $SRC_DIR (run marker.py first to load this book)" >&2
fi

echo "done. raw archived; image_ref base = s3://${BUCKET}/books/${BOOK_ID}/raw/img/" >&2
