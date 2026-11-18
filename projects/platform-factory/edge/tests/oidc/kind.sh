#!/usr/bin/env bash
# Run with a private kubeconfig on the PR runner.
set -euo pipefail
# kubelet needs the kernel key sysctls. The BuildBuddy runner kernel has
# none, so kind cannot start a node there; HOME-421 moves this to a runner
# that can. Skip loudly instead of failing every edge PR.
if [ ! -e /proc/sys/kernel/keys/root_maxkeys ]; then
	echo "SKIP edge OIDC kind test: this kernel has no /proc/sys/kernel/keys (HOME-421)"
	exit 0
fi
tmp=$(mktemp -d)
export PATH="$tmp:$PATH" KUBECONFIG="$tmp/kubeconfig"
cd "$(dirname "$0")"
curl -fsSL https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64 -o "$tmp/kind"
curl -fsSL https://dl.k8s.io/release/v1.34.0/bin/linux/amd64/kubectl -o "$tmp/kubectl"
curl -fsSL https://get.helm.sh/helm-v3.18.6-linux-amd64.tar.gz | tar -xz -C "$tmp" --strip-components=1 linux-amd64/helm
chmod +x "$tmp/kind" "$tmp/kubectl"
trap 'kind delete cluster --name edge-oidc; rm -rf "$tmp"' EXIT
if ! kind create cluster --name edge-oidc --retain; then
	docker exec edge-oidc-control-plane journalctl -u kubelet --no-pager | tail -n 40 || true
	exit 1
fi
bash run.sh
