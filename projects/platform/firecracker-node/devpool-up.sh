#!/usr/bin/env bash
# Recreate the containerd devmapper thin-pool on boot from its on-disk backing
# files. Idempotent. Does NOT zero metadata (that would wipe the pool); the
# backing meta file holds the real thin-pool allocation table across reboots.
set -euo pipefail

POOL_NAME=devpool
POOL_DIR=/disks/nvme-02/containerd-devmapper

# Already present (e.g. manual start after step 1) -> nothing to do.
if dmsetup info "$POOL_NAME" >/dev/null 2>&1; then
	exit 0
fi

modprobe dm_thin_pool

DATA_DEV=$(losetup -j "$POOL_DIR/data" -O NAME -n | head -1)
[ -n "$DATA_DEV" ] || DATA_DEV=$(losetup --find --show "$POOL_DIR/data")
META_DEV=$(losetup -j "$POOL_DIR/meta" -O NAME -n | head -1)
[ -n "$META_DEV" ] || META_DEV=$(losetup --find --show "$POOL_DIR/meta")

SECTORS=$(($(blockdev --getsize64 "$DATA_DEV") / 512))
dmsetup create "$POOL_NAME" --table "0 $SECTORS thin-pool $META_DEV $DATA_DEV 128 32768"
echo "devpool restored: $(dmsetup status "$POOL_NAME")"
