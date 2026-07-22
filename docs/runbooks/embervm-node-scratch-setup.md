# EmberVM node scratch setup (pre-cloud-init)

EmberVM's noded needs `/var/lib/embervm/scratch` to be a correctly-sized ext4
mount on every Firecracker node (label `homelab.io/firecracker=true`). This is
provisioned ONCE per node, out of band:

- **Homelab today:** the manual host setup below, over SSH with sudo.
- **Cloud later:** cloud-init / a machine template does the equivalent (attach +
  format a dedicated volume, or the same loop file), so noded never runs a
  privileged provisioning pod.

The old `scratchPrep` runtime DaemonSet was dropped: the masters' minimal host
image lacks `losetup`/`mkfs.ext4`/`mount` on the container nsenter PATH, and the
container-vs-host path split kept surfacing a new failure per layer. Doing it
once on the host with the host's own tools is simpler and robust.

## The cap

Each node gets `/var/lib/embervm/scratch` as a **35 GiB ext4 mount**, so a warmth
leak is physically unable to starve the root/etcd disk. Two shapes:

- **node-4:** a dedicated NVMe (`/dev/nvme0n1`) is mounted there. Nothing to do.
- **masters (node-1/2/3):** no spare disk and the VG is fully allocated, so use a
  35 GiB **loop-file** on root (the software equivalent of a dedicated volume).

## Per-master setup

SSH in as your user and run as root. Idempotent: safe to re-run.

```bash
sudo bash -euo pipefail <<'EOF'
SCRATCH=/var/lib/embervm/scratch
IMG=/var/lib/embervm/scratch.img
SIZE_GI=35

mkdir -p /var/lib/embervm

# If scratch is already the loop cap, done.
if mountpoint -q "$SCRATCH" && losetup -j "$IMG" | grep -q .; then
  echo "scratch already the capped loop mount; nothing to do"; df -h "$SCRATCH"; exit 0
fi

# If scratch is mounted but NOT our loop cap (e.g. an earlier uncapped root-LV
# bind), unmount it first. noded must not be actively using it: cordon/unlabel
# the node (kubectl label node <n> homelab.io/firecracker-) and wait for the
# noded pod to drain BEFORE running this, then re-label after.
if mountpoint -q "$SCRATCH"; then
  echo "unmounting existing (uncapped) scratch mount"; umount "$SCRATCH"
fi

# Create + format the capped backing file (only if absent).
if [ ! -f "$IMG" ]; then
  fallocate -l "${SIZE_GI}G" "$IMG"
  mkfs.ext4 -q -F "$IMG"
fi

mkdir -p "$SCRATCH"
mount -o loop "$IMG" "$SCRATCH"

# Persist across reboots.
grep -qF "$IMG $SCRATCH" /etc/fstab || \
  echo "$IMG $SCRATCH ext4 loop,defaults 0 0" >> /etc/fstab

df -h "$SCRATCH"
echo "scratch cap ready on $(hostname)"
EOF
```

## Onboarding a master into the fleet

1. Run the setup above on the node (scratch mount ready).
2. `kubectl label node <node> homelab.io/firecracker=true` so noded schedules there.
3. Confirm noded goes `1/1 Running` and the control plane builds the node's
   per-vendor bases (Intel on the masters, ~11 GiB), hydrating the rest from S3.
4. Watch the node stays `Ready` with no `DiskPressure`.

To revert: `kubectl label node <node> homelab.io/firecracker-` drains noded;
the loop mount can stay (harmless) or be removed (`umount`, remove the fstab
line, `rm` the `.img`).
