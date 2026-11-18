#!/usr/bin/env bash
# helm-template-test.sh - Validates that a Helm chart renders successfully with given values
#
# Usage: helm-template-test.sh <helm-binary> <release-name> <chart-path> <namespace> [values-files...]
#
# This script is used by Bazel's helm_template_test rule to validate that
# Helm charts render without errors when combined with their values files.
# Exit code 0 = success, non-zero = rendering failed.

set -euo pipefail

if [[ $# -lt 4 ]]; then
	echo "Usage: $0 <helm-binary> <release-name> <chart-path> <namespace> [values-files...]"
	exit 1
fi

HELM="$1"
RELEASE_NAME="$2"
CHART_PATH="$3"
NAMESPACE="$4"
shift 4

# Build values arguments
VALUES_ARGS=()
for values_file in "$@"; do
	VALUES_ARGS+=("--values" "$values_file")
done

echo "Testing Helm template rendering:"
echo "  Release: $RELEASE_NAME"
echo "  Chart:   $CHART_PATH"
echo "  Namespace: $NAMESPACE"
echo "  Values:  $*"
echo ""

# Render to a file. Errors are printed to stderr.
RENDERED="${TEST_TMPDIR:-$(mktemp -d)}/rendered.yaml"
if ! "$HELM" template "$RELEASE_NAME" "$CHART_PATH" \
	--namespace "$NAMESPACE" \
	"${VALUES_ARGS[@]}" \
	>"$RENDERED"; then
	echo ""
	echo "FAILED: Chart rendering produced errors"
	exit 1
fi
echo "PASSED: Chart renders successfully"

# CONTENT_RULES is set by helm_template_test(content_rules = True).
if [[ -n "${CONTENT_RULES:-}" ]]; then
	# Each exemption is one Kind/namespace/name word.
	read -r -a EXEMPT <<<"${CONTENT_RULES_EXCEPTIONS:-}"
	if ! "$CONTENT_RULES" "$NAMESPACE" ${EXEMPT[@]+"${EXEMPT[@]}"} <"$RENDERED"; then
		echo "FAILED: the render breaks a factory-cluster content rule (bazel/helm/content_rules)"
		exit 1
	fi
	echo "PASSED: the render meets the factory-cluster content rules"
fi
