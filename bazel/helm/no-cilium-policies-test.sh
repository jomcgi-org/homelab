#!/usr/bin/env bash
# Application policies were removed in #5816, including default-on home arms.
set -uo pipefail

if [[ $# -eq 0 ]]; then
	echo "no rendered application manifests supplied" >&2
	exit 2
fi
for manifest in "$@"; do
	if [[ ! -s "$manifest" ]]; then
		echo "missing or empty manifest: $manifest" >&2
		exit 2
	fi
	grep -nF 'cilium.io/v2' "$manifest"
	status=$?
	if [[ $status -eq 0 ]]; then
		echo "inert Cilium policy in $manifest" >&2
		exit 1
	elif [[ $status -ne 1 ]]; then
		echo "could not inspect $manifest (grep status $status)" >&2
		exit "$status"
	fi
done
