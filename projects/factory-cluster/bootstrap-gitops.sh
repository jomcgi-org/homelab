#!/usr/bin/env bash
# Hand the factory cluster to ArgoCD. Run once from an operator machine after
# nodes/bootstrap-nodes.sh, and again only to rebuild a lost cluster.
#
# The only imperative steps on the cluster are here: the two 1Password
# bootstrap Secrets that nothing in Git may hold, a first install of Connect
# and ArgoCD from this repository's charts, and the factory-root Application.
# Every later change, including to those two charts, goes through a PR and is
# synced by ArgoCD, which adopts what this script installed.
#
# Inputs:
#   KUBECONFIG            the factory cluster (default ~/.kube/factory.yaml)
#   OP_CREDENTIALS_FILE   the Connect server's 1password-credentials.json
#   OP_CONNECT_TOKEN      an access token for that Connect server
# Both 1Password inputs come from the factory Connect server created in the
# 1Password admin console with read access to the k8s-homelab vault.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/factory.yaml}"
: "${OP_CREDENTIALS_FILE:?set OP_CREDENTIALS_FILE to the Connect 1password-credentials.json}"
: "${OP_CONNECT_TOKEN:?set OP_CONNECT_TOKEN to a Connect access token}"

context="$(kubectl config current-context)"
echo "Bootstrapping GitOps on context ${context} ($(kubectl get nodes --no-headers | wc -l) nodes)"

apply() { kubectl apply --server-side --force-conflicts -f -; }

# 1. 1Password Connect and operator, with the bootstrap Secrets they read.
kubectl create namespace onepassword --dry-run=client -o yaml | apply
# The Connect chart stores the credentials file base64-encoded (OP_SESSION).
kubectl -n onepassword create secret generic op-credentials \
	--from-literal=1password-credentials.json="$(base64 -w0 <"${OP_CREDENTIALS_FILE}")" \
	--dry-run=client -o yaml | apply
kubectl -n onepassword create secret generic onepassword-token \
	--from-literal=token="${OP_CONNECT_TOKEN}" \
	--dry-run=client -o yaml | apply
helm template connect "${ROOT}/projects/platform/onepassword" -n onepassword --include-crds \
	-f "${ROOT}/projects/platform/onepassword/values.yaml" | apply
kubectl -n onepassword rollout status deploy/onepassword-connect --timeout=5m
kubectl wait --for=condition=Established crd/onepassworditems.onepassword.com --timeout=2m

# 2. ArgoCD, rendered from the same chart and values its Application uses.
kubectl create namespace argocd --dry-run=client -o yaml | apply
helm template argocd "${ROOT}/projects/platform/argocd" -n argocd \
	-f "${ROOT}/projects/platform/argocd/values.yaml" \
	-f "${ROOT}/projects/platform/argocd/values-factory.yaml" | apply
kubectl -n argocd rollout status deploy/argocd-server --timeout=5m
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=5m

# 3. The parent Application. From here ArgoCD owns the cluster.
apply <"${ROOT}/projects/factory-cluster/factory-root-application.yaml"
echo "factory-root applied; watch: kubectl -n argocd get applications -w"
