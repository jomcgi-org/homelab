#!/usr/bin/env bash
# admissibility-test.sh - Asks the Kubernetes API schemas whether rendered
# manifests are admissible (#4831).
#
# Usage:
#   admissibility-test.sh <helm> <kubeconform> <fixture-chart-yaml> \
#       <schema-dir-anchor> [rendered-manifests...]
#
# <schema-dir-anchor> is the path of any one vendored schema file (the BUILD
# passes bazel/helm/k8s-schemas/v1.35.8-standalone-strict/configmap-v1.json);
# its directory's parent is the schema root.
#
# Two checks:
#
#  1. Every rendered manifest (helm template against each chart's real value
#     stack) must pass kubeconform against the VENDORED standalone-strict
#     schemas for the cluster's minor version, with at least one resource
#     actually validated. This is offline validation of the same objects the
#     API server would admit; it catches required-field defects like #4830's
#     empty secretKeyRef.key, which helm renders happily.
#
#  2. Negative control: a fixture reproducing #4830 (a secretKeyRef whose key
#     renders as null) MUST be rejected, with the SecretKeySelector error. If
#     it ever passes, or is rejected for some unrelated reason, the gate has
#     gone blind and this test fails even though no real chart regressed.
#
# HERMETIC: schemas come from bazel/helm/k8s-schemas (see its README.md for
# provenance and pinning). kubeconform runs with a local -schema-location, so
# it makes zero network calls; the CI sandbox has no network and this test
# must never need one. There is deliberately NO -ignore-missing-schemas: a
# kind without a vendored schema fails loudly until it is either vendored
# (built-in kinds) or added to SKIP_KINDS below (CRDs).

set -uo pipefail

if [[ $# -lt 4 ]]; then
	echo "Usage: $0 <helm> <kubeconform> <fixture-chart-yaml> <schema-dir-anchor> [rendered-manifests...]"
	exit 1
fi

HELM="$1"
KUBECONFORM="$2"
FIXTURE_CHART_YAML="$3"
SCHEMA_ANCHOR="$4"
shift 4

# The cluster runs Kubernetes 1.35 (ADR agents/028); the vendored tree mirrors
# upstream's <version>-standalone-strict directory layout under the schema root.
SCHEMA_ROOT="$(dirname "$SCHEMA_ANCHOR")/../"
KUBERNETES_VERSION="1.35.8"
SCHEMA_LOCATION="${SCHEMA_ROOT}{{ .NormalizedKubernetesVersion }}-standalone{{ .StrictSuffix }}/{{ .ResourceKind }}{{ .KindSuffix }}.json"

# CRDs have no vendored schema and are skipped by exact <apiVersion>/<Kind>
# (kubeconform matches this string precisely, so kyverno.io/v1/ClusterPolicy
# and nvidia.com/v1/ClusterPolicy are distinct entries). Every entry here is a
# conscious decision to leave that kind's body unvalidated by this gate;
# built-in kinds must NEVER be listed here. Kinds observed across the 28
# manifest sets as of #4831:
SKIP_KINDS="argoproj.io/v1alpha1/CronWorkflow"                                                                                                                  # argo-workflows chart
SKIP_KINDS+=",cert-manager.io/v1/Certificate,cert-manager.io/v1/Issuer"                                                                                         # cert-manager chart
SKIP_KINDS+=",cilium.io/v2/CiliumNetworkPolicy"                                                                                                                 # cilium chart
SKIP_KINDS+=",clickhouse.altinity.com/v1/ClickHouseInstallation"                                                                                                # signoz chart
SKIP_KINDS+=",db.atlasgo.io/v1alpha1/AtlasMigration"                                                                                                            # atlas-operator chart
SKIP_KINDS+=",embervm.dev/v1alpha1/Workload"                                                                                                                    # first-party EmberVM CRD
SKIP_KINDS+=",gateway.envoyproxy.io/v1alpha1/BackendTrafficPolicy,gateway.envoyproxy.io/v1alpha1/HTTPRouteFilter,gateway.envoyproxy.io/v1alpha1/SecurityPolicy" # cf-ingress (Envoy Gateway)
SKIP_KINDS+=",gateway.networking.k8s.io/v1/HTTPRoute"                                                                                                           # Gateway API CRD
SKIP_KINDS+=",kargo.akuity.io/v1alpha1/Project,kargo.akuity.io/v1alpha1/ProjectConfig,kargo.akuity.io/v1alpha1/Stage,kargo.akuity.io/v1alpha1/Warehouse"        # kargo chart
SKIP_KINDS+=",kyverno.io/v1/ClusterPolicy"                                                                                                                      # kyverno chart
SKIP_KINDS+=",nvidia.com/v1/ClusterPolicy"                                                                                                                      # nvidia-gpu-operator chart
SKIP_KINDS+=",onepassword.com/v1/OnePasswordItem"                                                                                                               # 1Password Operator
SKIP_KINDS+=",opentelemetry.io/v1alpha1/Instrumentation"                                                                                                        # opentelemetry-operator chart
SKIP_KINDS+=",postgresql.cnpg.io/v1/Cluster,postgresql.cnpg.io/v1/Database,postgresql.cnpg.io/v1/ScheduledBackup"                                               # cloudnative-pg chart
# Upstream kubernetes-json-schema ships no CustomResourceDefinition kind
# schema at all (only its sub-objects), so CRD definitions themselves are
# skipped rather than vendored from elsewhere.
SKIP_KINDS+=",apiextensions.k8s.io/v1/CustomResourceDefinition"

failures=0

for manifest in "$@"; do
	if out=$("$KUBECONFORM" -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
		-schema-location "$SCHEMA_LOCATION" -skip "$SKIP_KINDS" "$manifest" 2>&1); then
		echo "ADMISSIBLE: $manifest"
		echo "$out"
	else
		echo "NOT ADMISSIBLE: $manifest"
		echo "$out"
		failures=$((failures + 1))
		continue
	fi

	# A set that validates nothing proves nothing: require Valid > 0 and no
	# Errors even when kubeconform exits 0 (e.g. everything skipped).
	valid=$(printf '%s\n' "$out" | sed -n 's/.*Valid: \([0-9]*\),.*/\1/p' | tail -n 1)
	errors=$(printf '%s\n' "$out" | sed -n 's/.*Errors: \([0-9]*\).*/\1/p' | tail -n 1)
	if [[ ! "$valid" =~ ^[0-9]+$ ]] || [[ "$valid" -eq 0 ]]; then
		echo "NOT ADMISSIBLE: $manifest validated 0 resources; the gate proved nothing."
		failures=$((failures + 1))
	elif [[ "${errors:-0}" != "0" ]]; then
		echo "NOT ADMISSIBLE: $manifest reported $errors error(s)."
		failures=$((failures + 1))
	fi
done

echo ""
echo "Validated $# rendered manifest set(s) against vendored Kubernetes ${KUBERNETES_VERSION} schemas."

# --- Schema integrity: the tree must match its pinned manifest ---------------

if (cd "$(dirname "$SCHEMA_ANCHOR")/.." && shasum -c SHA256SUMS >/dev/null 2>&1) ||
	(cd "$(dirname "$SCHEMA_ANCHOR")/.." && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
	echo "Schema integrity OK ($(grep -c . "$(dirname "$SCHEMA_ANCHOR")/.."/SHA256SUMS) files match SHA256SUMS)."
else
	echo "FAILED: vendored schemas in bazel/helm/k8s-schemas do not match SHA256SUMS."
	echo "The schema tree was modified without re-pinning; see bazel/helm/k8s-schemas/README.md."
	failures=$((failures + 1))
fi

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

kcf_out="$(printf '%s\n' "$fixture_manifests" | "$KUBECONFORM" -strict -summary \
	-kubernetes-version "$KUBERNETES_VERSION" -schema-location "$SCHEMA_LOCATION" \
	-skip "$SKIP_KINDS" - 2>&1)"
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

echo "NEGATIVE CONTROL PASSED: empty secretKeyRef.key rejected (#4830):"
printf '%s\n' "$kcf_out" | grep "secretKeyRef"

if [[ $failures -gt 0 ]]; then
	echo ""
	echo "FAILED: $failures rendered manifest set(s) are not admissible."
	exit 1
fi
