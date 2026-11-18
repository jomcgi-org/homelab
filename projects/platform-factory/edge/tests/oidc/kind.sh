#!/usr/bin/env bash
# Run with a private kubeconfig on the PR runner.
set -euo pipefail
tmp=$(mktemp -d)
export PATH="$tmp:$PATH" KUBECONFIG="$tmp/kubeconfig"
cd "$(dirname "$0")"
curl -fsSL https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64 -o "$tmp/kind"
curl -fsSL https://dl.k8s.io/release/v1.34.0/bin/linux/amd64/kubectl -o "$tmp/kubectl"
curl -fsSL https://get.helm.sh/helm-v3.18.6-linux-amd64.tar.gz | tar -xz -C "$tmp" --strip-components=1 linux-amd64/helm
chmod +x "$tmp/kind" "$tmp/kubectl"
trap 'kind delete cluster --name edge-oidc; rm -rf "$tmp"' EXIT
kind create cluster --name edge-oidc
bash run.sh
