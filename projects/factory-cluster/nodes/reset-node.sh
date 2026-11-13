#!/usr/bin/env bash
# Remove a previous Kubernetes installation from a factory home node, in place.
#
# DESTRUCTIVE. Deletes every container, image, volume and etcd member on the
# node it runs on: the old k3s install, Longhorn replicas, EmberVM scratch and
# the Cilium sysctl overrides left by the retired home cluster (#6914). It keeps
# the OS, SSH, Tailscale and node-1's host cloudflared (bastion.jomcgi.dev).
#
# Run on the node itself, as root, with the node's hostname as confirmation:
#   sudo ./reset-node.sh --yes node-2
set -euo pipefail

if [[ "${1:-}" != "--yes" || "${2:-}" != "$(hostname)" ]]; then
	echo "usage: $0 --yes $(hostname)" >&2
	exit 2
fi
[[ "$(id -u)" == 0 ]] || {
	echo "run as root" >&2
	exit 2
}

if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
	/usr/local/bin/k3s-uninstall.sh || true
elif [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
	/usr/local/bin/k3s-agent-uninstall.sh || true
fi
rm -rf /etc/systemd/system/k3s.service.d /etc/systemd/system/k3s-agent.service.d

if mountpoint -q /var/lib/embervm/scratch; then
	umount /var/lib/embervm/scratch
fi
sed -i '\#/var/lib/embervm/#d' /etc/fstab
rm -rf /var/lib/embervm /var/lib/longhorn /var/lib/kubelet /var/lib/rancher /etc/rancher /var/lib/cni /etc/cni
rm -f /etc/sysctl.d/99-zzz-override_cilium.conf
# Cilium's links and BPF state outlive k3s.
for link in cilium_host cilium_net cilium_vxlan cilium_wg0; do
	ip link delete "${link}" 2>/dev/null || true
done
rm -rf /var/run/cilium /sys/fs/bpf/cilium /sys/fs/bpf/tc/globals/cilium_* 2>/dev/null || true
systemctl daemon-reload
sysctl --system >/dev/null
echo "$(hostname): reset complete"
