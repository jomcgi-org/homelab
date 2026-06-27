#!/usr/bin/env bash
# Seed shared images into the devmapper snapshotter so kata-fc pods can create
# sandboxes. kata-fc runs every image as a block device (devmapper); the kubelet
# unpacks an app's own images into devmapper automatically, but a SHARED image
# that predates devmapper (the pause/sandbox image especially) won't be there,
# and if its layer content was GC'd after the original overlayfs unpack, the
# kubelet sandbox create fails with "content digest ... not found".
#
# The --local flag uses containerd's legacy pull path, which honors --platform
# for the unpack step. The containerd 2.x default (transfer service) throws
# "no unpack platforms defined" with --snapshotter, so --local is required here.
set -uo pipefail

# The CRI sandbox/pause image (pinned_images.sandbox in containerd config).
IMAGES=("docker.io/rancher/mirrored-pause:3.6")

for IMG in "${IMAGES[@]}"; do
	echo "=== seeding $IMG into devmapper ==="
	k3s ctr -n k8s.io images pull --local --snapshotter devmapper --platform linux/amd64 "$IMG"
done
echo "=== devmapper snapshots ==="
k3s ctr -n k8s.io snapshot --snapshotter devmapper ls 2>/dev/null | head
