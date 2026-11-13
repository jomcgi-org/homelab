#!/usr/bin/env bash
# Render Agent Substrate's install manifests at the pinned release into
# resources/upstream/, the files `ate-setup deploy ate-system` would apply.
#
# ate-setup has two install profiles, kind and GKE, and no render-only mode.
# This script does the deterministic part of its work for the factory cluster:
# it copies the release's manifests and resolves what ate-setup resolves at
# install time, namely each ko:// and ${ENVOY_DATAPLANE_IMAGE} reference
# pinned to the release image's digest, and the #ATE_EGRESS_INJECT_FLAGS
# marker. Factory-specific settings (S3 snapshot storage, the bundled
# PostgreSQL's roles, ate-api-server's configuration) are kustomize patches and
# files in resources/, reviewed separately. Generated key material is created
# by projects/factory-cluster/bootstrap-substrate.sh.
#
# Rerun after changing VERSION and COMMIT; review the diff of upstream/.
# Needs git and crane.
set -euo pipefail

VERSION="v1.0.0"
COMMIT="b34fbee742906494b5eb4f1f36987176bf8770a4"
REPO="ghcr.io/agent-substrate/substrate"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/resources/upstream"

src="$(mktemp -d)"
trap 'rm -rf "${src}"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "${VERSION}" \
	https://github.com/agent-substrate/substrate.git "${src}"
if [[ "$(git -C "${src}" rev-parse HEAD)" != "${COMMIT}" ]]; then
	echo "tag ${VERSION} is not commit ${COMMIT}" >&2
	exit 1
fi

# The objects deploy ate-system applies with --sandbox-classes=gvisor and
# --credential-provider='{"enabled":false}'. Left out: ate-system-namespace.yaml
# (ate-controller.yaml declares the same Namespace), atenet-router-monitoring.yaml
# (a GKE Managed Prometheus PodMonitoring), ate-otel-config.yaml (it points at
# GKE's collector; resources/otel-config.yaml replaces it), the microVM
# SandboxConfig and the bundled credential provider.
manifests=(
	generated/ate.dev_csidriverconfigs.yaml
	generated/ate.dev_sandboxconfigs.yaml
	generated/ate.dev_workerpools.yaml
	generated/role.yaml
	sandboxconfig-validation.yaml
	sandboxconfig-gvisor.yaml
	pod-certificate-controller.yaml
	postgres/postgres.yaml
	ate-api-server.yaml
	ate-idp-server.yaml
	ate-controller.yaml
	atelet.yaml
	atenet-router.yaml
	atenet-egress.yaml
	ate-system-networkpolicies.yaml
)

# ko://go.ate.dev/substrate/cmd/<path> is published as <last path element>.
images=(ate-idp-server ateapi atecontroller atelet atenet objectstore-plugin podcertcontroller envoy-dataplane)
declare -A pinned
for image in "${images[@]}"; do
	pinned[${image}]="${REPO}/${image}:${VERSION}@$(crane digest "${REPO}/${image}:${VERSION}")"
done

rm -rf "${OUT}"
mkdir -p "${OUT}"
for manifest in "${manifests[@]}"; do
	dest="${OUT}/$(basename "${manifest}")"
	{
		echo "# Rendered by render-upstream.sh from agent-substrate/substrate ${VERSION}"
		echo "# (${COMMIT}) manifests/ate-install/${manifest}. Do not edit."
		cat "${src}/manifests/ate-install/${manifest}"
	} >"${dest}"
	for image in "${images[@]}"; do
		sed -i -E "s#ko://go\.ate\.dev/substrate/cmd/([a-z0-9/-]+/)?${image}\$#${pinned[${image}]}#" "${dest}"
	done
	sed -i "s#\${ENVOY_DATAPLANE_IMAGE}#${pinned["envoy-dataplane"]}#" "${dest}"
	# {"enabled":false}: no credential provider flags.
	sed -i '/#ATE_EGRESS_INJECT_FLAGS/d' "${dest}"
	if grep -nE "ko://|[$]\{ENVOY_DATAPLANE_IMAGE\}|#ATE_EGRESS_INJECT_FLAGS" "${dest}"; then
		echo "unresolved reference in ${dest}" >&2
		exit 1
	fi
done

# The bundled PostgreSQL's role and schema setup, which ate-setup runs with
# psql in the postgres pod (pkg/postgressetup); resources/ runs it from the
# StatefulSet's postStart hook instead.
{
	echo "-- Rendered by render-upstream.sh from agent-substrate/substrate ${VERSION}"
	echo "-- (${COMMIT}) pkg/postgressetup/setup.sql. Do not edit."
	cat "${src}/pkg/postgressetup/setup.sql"
} >"${OUT}/postgres-setup.sql"
echo "rendered ${#manifests[@]} manifests and postgres-setup.sql into ${OUT}"
