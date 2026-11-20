#!/usr/bin/env bash
# Exercise the installed policy without creating items or reading Secrets.
set -euo pipefail
check() {
	local path=$1 expected=$2 response
	response=$(
		kubectl create --dry-run=server -f - 2>&1 <<YAML
apiVersion: onepassword.com/v1
kind: OnePasswordItem
metadata:
  generateName: admission-test-
  namespace: ships
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
check vaults/k8s-homelab/items/marine listed
check vaults/k8s-homelab/items/factory-gh-app unlisted
