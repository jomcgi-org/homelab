#!/usr/bin/env bash
# Exercise the installed policy without creating items or reading Secrets.
set -euo pipefail
check() {
	local namespace=$1 path=$2 expected=$3 response
	response=$(
		kubectl create --dry-run=server -f - 2>&1 <<YAML
apiVersion: onepassword.com/v1
kind: OnePasswordItem
metadata:
  generateName: admission-test-
  namespace: $namespace
spec:
  itemPath: $path
YAML
	) || {
		echo "$response" >&2
		return 1
	}
	local observed=listed
	if [[ $response == *"Warning:"*"OnePasswordItem itemPath is not assigned to this namespace"* ]]; then
		observed=unlisted
	fi
	[[ $observed == "$expected" ]]
}
check ships vaults/k8s-homelab/items/marine listed
check ships vaults/k8s-homelab/items/factory-gh-app unlisted
check ate-system vaults/k8s-homelab/items/ghcr-read-permissions listed
