#!/usr/bin/env bash
# Install the pinned k3s server and gVisor on one factory home node.
#
# Idempotent: rerunning on an installed node rewrites the configuration and
# restarts k3s at the pinned version. Run as root from this directory
# (nodes/bootstrap-nodes.sh copies it to each node):
#
#   sudo ./install-node.sh init                      # first server only
#   sudo K3S_TOKEN=... ./install-node.sh join https://192.168.4.80:6443
set -euo pipefail

K3S_VERSION="v1.37.2+k3s1"
GVISOR_RELEASE="20261102.0"
GVISOR_SHA512="3c400f72be459b57b99b1c4f69be96bcd67eef3caf2078e595fc98e688b6b9a3af7802646b3dde7da8a9aba96d34f86be303e0430c7868ab2d13becdd403c0c1"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-}"
[[ "$(id -u)" == 0 ]] || {
	echo "run as root" >&2
	exit 2
}
case "${MODE}" in
init) ;;
join)
	SERVER="${2:?join needs the server URL}"
	: "${K3S_TOKEN:?join needs K3S_TOKEN}"
	;;
*)
	echo "usage: $0 init | join <server-url>" >&2
	exit 2
	;;
esac

install -m 0644 "${HERE}/99-factory.conf" /etc/sysctl.d/99-factory.conf
# Tailscale (node-1) sets src_valid_mark=1 whenever it manages iptables, which
# breaks Cilium's proxies (see 99-factory.conf). With netfilter mode off it
# leaves both alone; the tailnet itself still works.
if command -v tailscale >/dev/null && systemctl is-active --quiet tailscaled; then
	tailscale set --netfilter-mode=off
fi
sysctl --system >/dev/null

# Host firewall: only nodes, pods and the tailnet reach host ports.
install -D -m 0644 "${HERE}/factory-firewall.nft" /etc/factory/firewall.nft
install -m 0644 "${HERE}/factory-firewall.service" /etc/systemd/system/factory-firewall.service
systemctl daemon-reload
systemctl enable --quiet factory-firewall
# reload swaps the rules in one transaction; restart would drop them first.
systemctl reload-or-restart factory-firewall
# rpcbind serves NFS, which no node uses.
systemctl disable --now --quiet rpcbind.socket rpcbind.service 2>/dev/null || true

# zram swap replaces the disk swap file (zram-generator.conf).
if ! dpkg -s systemd-zram-generator >/dev/null 2>&1; then
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd-zram-generator
fi
install -m 0644 "${HERE}/zram-generator.conf" /etc/systemd/zram-generator.conf
systemctl daemon-reload
# The setup unit waits for /dev/zram0, which loading the module creates.
modprobe zram
[[ -e /dev/zram0 ]] || cat /sys/class/zram-control/hot_add >/dev/null
# Sets up and enables zram0, or does nothing when it is already swap.
systemctl start dev-zram0.swap
if grep -q '^/swap.img ' /proc/swaps; then
	swapoff /swap.img
fi
sed -i 's|^/swap.img|#/swap.img|' /etc/fstab
rm -f /swap.img

# gVisor, at the release Agent Substrate's SandboxConfig pins.
# runsc execs its sentry and helpers from gvisor-bin/ beside its own binary.
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
curl -fsSL -o "${tmp}/install.sh" "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION}/install.sh"
echo "ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b  ${tmp}/install.sh" | sha256sum -c --quiet
if [[ "$(/usr/local/bin/runsc --version 2>/dev/null | head -1)" != *"release-${GVISOR_RELEASE}"* ||
! -x /usr/local/bin/gvisor-bin/gvisor_sentry ]]; then
	curl -fsSL -o "${tmp}/gvisor.tar.zstd" \
		"https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/x86_64/gvisor.tar.zstd"
	echo "${GVISOR_SHA512}  ${tmp}/gvisor.tar.zstd" | sha512sum -c --quiet
	mkdir "${tmp}/x"
	tar --zstd -xf "${tmp}/gvisor.tar.zstd" -C "${tmp}/x"
	rm -rf /usr/local/bin/gvisor-bin
	cp -a "${tmp}/x/gvisor-bin" /usr/local/bin/gvisor-bin
	install -m 0755 "${tmp}/x/runsc" "${tmp}/x/containerd-shim-runsc-v1" /usr/local/bin/
fi

install -d -m 0755 /etc/rancher/k3s /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d
bash "${HERE}/cap-ate.sh"
install -d -m 0700 /var/log/k3s-audit
install -m 0600 "${HERE}/audit-policy.yaml" /etc/rancher/k3s/audit-policy.yaml
install -m 0600 "${HERE}/k3s-config.yaml" /etc/rancher/k3s/config.yaml
install -m 0644 "${HERE}/registries.yaml" /etc/rancher/k3s/registries.yaml
install -m 0644 "${HERE}/authentication-config.yaml" /etc/rancher/k3s/authentication-config.yaml
# A drop-in, not a config-v3.toml.tmpl, so k3s's generated config stays whole.
rm -f /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl
install -m 0644 "${HERE}/containerd-runsc.toml" \
	/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/runsc.toml
# The kubelet and k3s read these at start; the install below restarts k3s.
install -D -m 0644 "${HERE}/kubelet-swap.conf" \
	/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/50-swap.conf
install -D -m 0644 "${HERE}/k3s-gogc.conf" /etc/systemd/system/k3s.service.d/10-gogc.conf
systemctl daemon-reload

# The install script is taken from the pinned release tag, not get.k3s.io.
# It restarts k3s only when the binary, unit or env file changed; k3s reads
# config.yaml and registries.yaml only at start, so a rerun forces the restart.
if [[ "${MODE}" == init ]]; then
	INSTALL_K3S_VERSION="${K3S_VERSION}" INSTALL_K3S_FORCE_RESTART=true \
		sh "${tmp}/install.sh" server --cluster-init
else
	INSTALL_K3S_VERSION="${K3S_VERSION}" INSTALL_K3S_FORCE_RESTART=true \
		K3S_TOKEN="${K3S_TOKEN}" sh "${tmp}/install.sh" server --server "${SERVER}"
fi
