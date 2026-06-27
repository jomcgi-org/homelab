#!/usr/bin/env bash
# Step 3f: boot a real Firecracker microVM via kata at the containerd layer.
# No k8s/kubectl/RuntimeClass needed. Proves kata + firecracker + devmapper work.
set -uo pipefail
FCCFG=/opt/kata/share/defaults/kata-containers/configuration-fc.toml
IMG=docker.io/library/busybox:latest
NS=kata-spike # isolated ctr namespace for this test

echo "host kernel (node-4): $(uname -r)"
echo "kata guest kernel image: $(ls -la /opt/kata/share/kata-containers/vmlinux.container 2>/dev/null)"
echo

echo "=== pull + unpack busybox into the devmapper snapshotter ==="
k3s ctr -n "$NS" image pull "$IMG"
k3s ctr -n "$NS" image unpack --snapshotter devmapper "$IMG" || true

echo
echo "=== boot Firecracker microVM (kata) and inspect from inside ==="
k3s ctr -n "$NS" container rm kataspike 2>/dev/null || true
set -x
k3s ctr -n "$NS" run --rm \
	--runtime io.containerd.kata.v2 \
	--runtime-config-path "$FCCFG" \
	--snapshotter devmapper \
	"$IMG" kataspike /bin/sh -c 'echo "=== INSIDE THE microVM ==="; echo "guest_kernel=$(uname -r)"; echo "guest_uname=$(uname -a)"; echo "vcpus=$(nproc)"; echo "mem:"; free -m 2>/dev/null | head -2; echo "is this a VM? dmesg hints:"; dmesg 2>/dev/null | grep -iE "firecracker|kvm|virtio" | head -3'
rc=$?
set +x
echo "=== ctr run exit code: $rc ==="
[ "$rc" = "0" ] && echo "If guest_kernel above != $(uname -r) -> a Firecracker microVM booted. SUCCESS." ||
	echo "FAILED (rc=$rc) - capture the error above; likely a runtime-config-path / kata-fc config detail to adjust."
