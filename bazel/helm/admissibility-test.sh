#!/usr/bin/env bash
# admissibility-test.sh - Asks the Kubernetes API schemas whether rendered
# manifests are admissible (#4831).
#
# Usage:
#   admissibility-test.sh <helm> <kubeconform> <fixture-chart-yaml> \
#       [rendered-manifests...]
#
# Two checks:
#
#  1. Every rendered manifest (helm template against each chart's real value
#     stack) must pass `kubeconform -strict -ignore-missing-schemas`. This is
#     offline schema validation of the same objects the API server would
#     admit; it catches required-field defects like #4830's empty
#     secretKeyRef.key, which helm renders happily.
#
#  2. Negative control: a fixture reproducing #4830 (a secretKeyRef whose key
#     renders as null) MUST be rejected, with the SecretKeySelector error. If
#     it ever passes, the gate has gone blind and this test fails even though
#     no real chart regressed.
#
# kubeconform downloads JSON schemas on first use and caches them under
# $TEST_TMPDIR, so this test needs network access (tags = ["requires-network"]).

set -uo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <helm> <kubeconform> <fixture-chart-yaml> [rendered-manifests...]"
	exit 1
fi

HELM="$1"
KUBECONFORM="$2"
FIXTURE_CHART_YAML="$3"
shift 3

CACHE_DIR="${TEST_TMPDIR:-$(mktemp -d)}/kubeconform-schema-cache"
mkdir -p "$CACHE_DIR"

failures=0

for manifest in "$@"; do
	if out=$("$KUBECONFORM" -strict -ignore-missing-schemas -summary -cache "$CACHE_DIR" "$manifest" 2>&1); then
		echo "ADMISSIBLE: $manifest"
	else
		echo "NOT ADMISSIBLE: $manifest"
		echo "$out"
		failures=$((failures + 1))
	fi
done

echo ""
echo "Validated $# rendered manifest set(s) against Kubernetes schemas."

# --- Negative control: the gate must still catch #4830 -----------------------

FIXTURE_CHART_DIR="$(dirname "$FIXTURE_CHART_YAML")"

export HELM_CACHE_HOME="${TEST_TMPDIR:-$PWD}/helm-cache"
export HELM_CONFIG_HOME="${TEST_TMPDIR:-$PWD}/helm-config"
export HELM_DATA_HOME="${TEST_TMPDIR:-$PWD}/helm-data"

if ! fixture_manifests="$("$HELM" template bad-secretkeyref "$FIXTURE_CHART_DIR" --namespace default 2>&1)"; then
	echo "NEGATIVE CONTROL BROKEN: fixture failed to render (it must render,"
	echo "that is the point: helm accepted what the API server rejects):"
	echo "$fixture_manifests"
	exit 1
fi

kcf_out="$(printf '%s\n' "$fixture_manifests" | "$KUBECONFORM" -strict -ignore-missing-schemas -summary -cache "$CACHE_DIR" - 2>&1)"
kcf_rc=$?
if [[ $kcf_rc -eq 0 ]]; then
	echo "NEGATIVE CONTROL FAILED: the #4830 fixture (secretKeyRef with an"
	echo "empty key) passed schema validation. The gate would not have caught"
	echo "the outage this test exists to prevent."
	exit 1
fi

if ! printf '%s\n' "$kcf_out" | grep -q "secretKeyRef"; then
	echo "NEGATIVE CONTROL INCONCLUSIVE: the fixture was rejected, but not for"
	echo "the SecretKeySelector defect. Rejecting for the wrong reason means"
	echo "the real defect may slip through:"
	echo "$kcf_out"
	exit 1
fi

echo "NEGATIVE CONTROL PASSED: empty secretKeyRef.key rejected (#4830)."

if [[ $failures -gt 0 ]]; then
	echo ""
	echo "FAILED: $failures rendered manifest set(s) are not admissible."
	exit 1
fi
