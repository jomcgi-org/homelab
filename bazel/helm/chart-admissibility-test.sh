#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 7 ]]; then
	echo "usage: $0 HELM KUBECONFORM CRD_EXTRACTOR FIXTURE_CHART SCHEMA_ANCHOR CRD_SOURCE... -- MANIFEST..."
	exit 2
fi

HELM="$1"
KUBECONFORM="$2"
CRD_EXTRACTOR="$3"
FIXTURE_CHART="$4"
SCHEMA_ANCHOR="$5"
shift 5

CRD_SOURCES=()
while [[ $# -gt 0 && "$1" != "--" ]]; do
	CRD_SOURCES+=("$1")
	shift
done
if [[ $# -eq 0 ]]; then
	echo "missing manifest separator"
	exit 2
fi
shift
MANIFESTS=("$@")

KUBERNETES_VERSION="1.36.3"
BUILTIN_SCHEMA_LOCATION="$(dirname "$SCHEMA_ANCHOR")/{{ .ResourceKind }}{{ .KindSuffix }}.json"
CRD_SCHEMA_ROOT="${TEST_TMPDIR:-$PWD}/crd-schemas"
CRD_SCHEMA_LOCATION="${CRD_SCHEMA_ROOT}/{{ .Group }}/{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json"

if ! "$CRD_EXTRACTOR" "$CRD_SCHEMA_ROOT" "${CRD_SOURCES[@]}" "${MANIFESTS[@]}"; then
	echo "failed to derive offline schemas from the pinned CRDs"
	exit 1
fi

# These are the custom kinds called out by #4831. Other CRDs found in the
# rendered charts are extracted and validated by the same mechanism.
REQUIRED_CRD_SCHEMAS=(
	"argoproj.io/application_v1alpha1.json"
	"cilium.io/ciliumnetworkpolicy_v2.json"
	"db.atlasgo.io/atlasmigration_v1alpha1.json"
	"gateway.envoyproxy.io/backendtrafficpolicy_v1alpha1.json"
	"gateway.networking.k8s.io/httproute_v1.json"
	"kargo.akuity.io/project_v1alpha1.json"
	"kargo.akuity.io/projectconfig_v1alpha1.json"
	"kargo.akuity.io/promotion_v1alpha1.json"
	"kargo.akuity.io/stage_v1alpha1.json"
	"kargo.akuity.io/warehouse_v1alpha1.json"
	"onepassword.com/onepassworditem_v1.json"
	"postgresql.cnpg.io/cluster_v1.json"
	"postgresql.cnpg.io/database_v1.json"
	"postgresql.cnpg.io/scheduledbackup_v1.json"
)
for schema in "${REQUIRED_CRD_SCHEMAS[@]}"; do
	if [[ ! -f "$CRD_SCHEMA_ROOT/$schema" ]]; then
		echo "missing required CRD schema: $schema"
		exit 1
	fi
done

KUBECONFORM_ARGS=(
	-strict
	-summary
	-kubernetes-version "$KUBERNETES_VERSION"
	-schema-location "$BUILTIN_SCHEMA_LOCATION"
	-schema-location "$CRD_SCHEMA_LOCATION"
	# kubernetes-json-schema has schemas for CRD subobjects, but not for the
	# CustomResourceDefinition resource itself. The CRD bodies are consumed
	# above to validate every custom resource they define.
	-skip "apiextensions.k8s.io/v1/CustomResourceDefinition"
)

failures=0
for manifest in "${MANIFESTS[@]}"; do
	if output=$("$KUBECONFORM" "${KUBECONFORM_ARGS[@]}" "$manifest" 2>&1); then
		echo "ADMISSIBLE: $manifest"
		echo "$output"
	else
		echo "NOT ADMISSIBLE: $manifest"
		echo "$output"
		failures=$((failures + 1))
		continue
	fi

	# An exit-zero run that validated nothing proves nothing. Kubeconform's
	# summary is part of this test's fail-closed contract.
	valid=$(sed -n 's/.*Valid: \([0-9][0-9]*\),.*/\1/p' <<<"$output" | tail -n 1)
	errors=$(sed -n 's/.*Errors: \([0-9][0-9]*\).*/\1/p' <<<"$output" | tail -n 1)
	if [[ ! "$valid" =~ ^[0-9]+$ ]] || [[ "$valid" -eq 0 ]]; then
		echo "NOT ADMISSIBLE: $manifest validated no resources"
		failures=$((failures + 1))
	elif [[ ! "$errors" =~ ^[0-9]+$ ]] || [[ "$errors" -ne 0 ]]; then
		echo "NOT ADMISSIBLE: $manifest did not report zero schema errors"
		failures=$((failures + 1))
	fi
done

export HELM_CACHE_HOME="${TEST_TMPDIR:-$PWD}/helm-cache"
export HELM_CONFIG_HOME="${TEST_TMPDIR:-$PWD}/helm-config"
export HELM_DATA_HOME="${TEST_TMPDIR:-$PWD}/helm-data"

if ! fixture_output=$("$HELM" template bad-secretkeyref "$(dirname "$FIXTURE_CHART")" 2>&1); then
	echo "negative control failed to render"
	echo "$fixture_output"
	exit 1
fi

set +e
fixture_validation=$(printf '%s\n' "$fixture_output" | "$KUBECONFORM" "${KUBECONFORM_ARGS[@]}" - 2>&1)
fixture_status=$?
set -e
if [[ $fixture_status -eq 0 ]]; then
	echo "negative control passed validation, so the gate did not catch the defect"
	exit 1
fi
if ! grep -q "secretKeyRef" <<<"$fixture_validation"; then
	echo "negative control failed for an unexpected reason"
	echo "$fixture_validation"
	exit 1
fi
echo "NEGATIVE CONTROL PASSED: empty secretKeyRef.key was rejected"
echo "$fixture_validation"

if [[ $failures -ne 0 ]]; then
	echo "$failures rendered manifest set(s) were not admissible"
	exit 1
fi

echo "Validated ${#MANIFESTS[@]} rendered manifest set(s) against Kubernetes ${KUBERNETES_VERSION}."
