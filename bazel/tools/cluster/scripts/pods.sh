#!/usr/bin/env bash
set -euo pipefail

# List pods across key namespaces in the homelab cluster.
# Read-only: uses only kubectl get commands.

if ! command -v kubectl &>/dev/null; then
	echo "ERROR: kubectl not found in PATH" >&2
	exit 1
fi

NAMESPACES=(
	argocd
	claude
	linkerd
	longhorn-system
	cert-manager
	kyverno
)

echo "=== Pod Status (key namespaces) ==="
echo ""

for ns in "${NAMESPACES[@]}"; do
	# Check if the namespace exists before querying
	if kubectl get namespace "$ns" &>/dev/null; then
		echo "--- $ns ---"
		kubectl get pods -n "$ns" --no-headers 2>/dev/null | while IFS= read -r line; do
			echo "  $line"
		done
		echo ""
	fi
done
