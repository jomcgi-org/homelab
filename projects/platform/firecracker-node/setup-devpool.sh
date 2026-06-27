#!/usr/bin/env bash
# Step 1: create a containerd devmapper thin-pool on node-4.
# NON-DISRUPTIVE: creates a device-mapper device only; does NOT touch containerd.
# kata-fc needs block-device-backed snapshots (Firecracker can't use overlayfs).
set -euo pipefail

POOL_NAME=devpool
POOL_DIR=/disks/nvme-02/containerd-devmapper
DATA_SIZE=150G # sparse: consumes real space only as the pool fills
META_SIZE=4G

if dmsetup info "$POOL_NAME" >/dev/null 2>&1; then
	echo "thin-pool /dev/mapper/$POOL_NAME already exists:"
	dmsetup status "$POOL_NAME"
	exit 0
fi

modprobe dm_thin_pool
mkdir -p "$POOL_DIR"
[ -f "$POOL_DIR/data" ] || truncate -s "$DATA_SIZE" "$POOL_DIR/data"
[ -f "$POOL_DIR/meta" ] || truncate -s "$META_SIZE" "$POOL_DIR/meta"

# Reuse a loop device if one is already bound to the file, else attach a new one.
DATA_DEV=$(losetup -j "$POOL_DIR/data" -O NAME -n | head -1)
[ -n "$DATA_DEV" ] || DATA_DEV=$(losetup --find --show "$POOL_DIR/data")
META_DEV=$(losetup -j "$POOL_DIR/meta" -O NAME -n | head -1)
[ -n "$META_DEV" ] || META_DEV=$(losetup --find --show "$POOL_DIR/meta")

# A fresh thin-pool requires zeroed metadata or dmsetup refuses to create it.
dd if=/dev/zero of="$META_DEV" bs=4096 count=1 conv=notrunc status=none

SECTORS=$(($(blockdev --getsize64 "$DATA_DEV") / 512))
# table: 0 <len_sectors> thin-pool <meta_dev> <data_dev> <data_block_size=128 (64KiB)> <low_water_mark=32768>
dmsetup create "$POOL_NAME" --table "0 $SECTORS thin-pool $META_DEV $DATA_DEV 128 32768"

echo "=== created /dev/mapper/$POOL_NAME ==="
dmsetup status "$POOL_NAME"
echo "data:  $DATA_DEV  ($POOL_DIR/data)"
echo "meta:  $META_DEV  ($POOL_DIR/meta)"
