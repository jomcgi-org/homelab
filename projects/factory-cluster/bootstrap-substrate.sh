#!/usr/bin/env bash
# Create the Agent Substrate key material that Git must not hold. Run once from
# an operator machine after bootstrap-gitops.sh, and again only to rebuild a
# lost cluster; it keeps every Secret that already exists, so a rerun changes
# nothing.
#
# `ate-setup deploy ate-system` generates these on first install
# (cmd/ate-setup/internal/steps/create.go); the rest of what it installs is
# declared in projects/platform-factory/substrate/. Until this runs, that
# Application's pods wait in ContainerCreating for the Secrets they mount.
#
#   podcertificate-controller-system/{service-dns,pod-identity,postgres}-ca-pool
#       the podcertificate controller's three signing CAs
#   ate-system/actor-id-ca-pool, actor-id-ca-certs (its root alone)
#       the actor identity CA, and the trust bundle the egress gateway reads
#   ate-system/actor-id-jwt-pool    the actor JWT signing keys
#   ate-system/egress-mitm-ca-pool  the egress gateway's interception CA
#   ate-system/snapshot-store-credentials
#       the access key of the in-cluster S3 snapshot store
#
# The CAs are valid for one year, as ate-setup makes them; rotating them is
# `kubectl ate admin` work (docs/guides/actor-jwt-key-rotation.md upstream).
#
# Inputs:
#   KUBECONFIG   the factory cluster (default ~/.kube/factory.yaml)
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/factory.yaml}"

# kubectl-ate from the Substrate release that
# projects/platform-factory/substrate/render-upstream.sh pins.
SUBSTRATE_VERSION="v1.0.0"
KUBECTL_ATE_SHA256="9f2444d8c9327cf3efa13d46468f6d83b856c5080017a29747249e1220605569"

PODCERT_NS="podcertificate-controller-system"
ATE_NS="ate-system"

context="$(kubectl config current-context)"
echo "Bootstrapping Agent Substrate secrets on context ${context}"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
curl -fsSL -o "${tmp}/kubectl-ate" \
	"https://github.com/agent-substrate/substrate/releases/download/${SUBSTRATE_VERSION}/kubectl-ate-linux-amd64"
echo "${KUBECTL_ATE_SHA256}  ${tmp}/kubectl-ate" | sha256sum -c --quiet
chmod +x "${tmp}/kubectl-ate"
ate() { "${tmp}/kubectl-ate" "$@"; }

exists() { kubectl -n "$1" get secret "$2" >/dev/null 2>&1; }

# The substrate Application creates both namespaces too; whichever runs first
# makes them, and ArgoCD adopts them.
for ns in "${PODCERT_NS}" "${ATE_NS}"; do
	kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply --server-side -f - # ratchet-allow: kubectl-mutate (one-time bootstrap of generated keys Git must not hold)
done

ca_pool() { # namespace name key-type
	if exists "$1" "$2"; then
		echo "kept ${1}/${2}"
	else
		ate admin make-ca-pool --secret-namespace "$1" --name "$2" --ca-id 1 --key-type "$3"
	fi
}
ca_pool "${PODCERT_NS}" service-dns-ca-pool ED25519
ca_pool "${PODCERT_NS}" pod-identity-ca-pool ED25519
ca_pool "${PODCERT_NS}" postgres-ca-pool ED25519
# External relying parties trust the actor identity root, so it is ECDSA P-256.
ca_pool "${ATE_NS}" actor-id-ca-pool ECDSAP256
ca_pool "${ATE_NS}" egress-mitm-ca-pool ECDSAP256

if exists "${ATE_NS}" actor-id-jwt-pool; then
	echo "kept ${ATE_NS}/actor-id-jwt-pool"
else
	ate admin make-jwt-pool --secret-namespace "${ATE_NS}" --name actor-id-jwt-pool --alg ES256
fi

# The actor identity root without its key. The pool holds one CA, whose
# certificate chain is that root.
if exists "${ATE_NS}" actor-id-ca-certs; then
	echo "kept ${ATE_NS}/actor-id-ca-certs"
else
	kubectl -n "${ATE_NS}" get secret actor-id-ca-pool -o jsonpath='{.data.tls\.crt}' |
		base64 -d >"${tmp}/ca.crt"
	kubectl -n "${ATE_NS}" create secret generic actor-id-ca-certs --from-file=ca.crt="${tmp}/ca.crt" # ratchet-allow: kubectl-mutate (one-time bootstrap of generated keys Git must not hold)
fi

if exists "${ATE_NS}" snapshot-store-credentials; then
	echo "kept ${ATE_NS}/snapshot-store-credentials"
else
	key_id="substrate-$(openssl rand -hex 8)"
	secret_key="$(openssl rand -hex 32)"
	kubectl -n "${ATE_NS}" create secret generic snapshot-store-credentials --from-literal=AWS_ACCESS_KEY_ID="${key_id}" --from-literal=AWS_SECRET_ACCESS_KEY="${secret_key}" # ratchet-allow: kubectl-mutate (one-time bootstrap of generated keys Git must not hold)
fi

echo "Agent Substrate secrets are in place; ArgoCD's substrate Application can start."
