#!/usr/bin/env bash
# Render Agent Substrate's install manifests at the pinned release into
# resources/upstream/.
#
# The factory runs the kagent-dev fork of Agent Substrate, the build kagent
# v1.0.1 compiles against. The fork publishes four Helm charts. This script
# pulls each at its pinned version, checks the chart's OCI digest, renders it
# with values/, and pins every image the charts name by tag to that tag's
# digest. The charts take one image tag for all components, so a digest pin
# cannot be set through values, and ArgoCD here cannot post-render a Helm
# source; rendering into Git keeps every image pinned and the diff reviewable.
#
# The substrate chart leaves out PostgreSQL. The fork's own development
# install (hack/install-postgres.sh) applies manifests/ate-install/postgres/
# and pkg/postgressetup/setup.sql from the same release, which are copied here.
# Factory-specific settings are values/ and kustomize patches in resources/.
# Generated key material comes from projects/factory-cluster/bootstrap-substrate.sh.
#
# Rerun after changing VERSION, COMMIT or a chart digest; review the diff of
# upstream/. Needs git, helm and crane.
set -euo pipefail

# Keep VERSION in step with SUBSTRATE_VERSION in
# projects/factory-cluster/bootstrap-substrate.sh and the Agent Substrate row of
# projects/factory-cluster/README.md.
VERSION="1.0.1"
COMMIT="7ce6f439239c568285f6d897aaa4afd47752c775"
CHARTS="oci://ghcr.io/kagent-dev/substrate/helm"
IMAGES="ghcr.io/kagent-dev/substrate"
declare -A CHART_DIGEST=(
	["substrate-crds"]="sha256:3ed670bacd5680ecbcafc9d4a6c95ef90a20f96568b1ba621d0c9c7e411eb084"
	["substrate-podcert"]="sha256:366b0431ed99cd4ff74f49cb7746329a722c9f8ec9ce0ab4d0670817b34deb80"
	["substrate-atelet"]="sha256:233e1ed8f74a202abca851c43adee88849482d8ce37760b8be65f4d96fbc8ca3"
	["substrate"]="sha256:336c12f7f265e1b6400529eb1ef69c8d0765872d810f18f97525c66d2ba90f86"
)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/resources/upstream"
VALUES="${HERE}/values"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

render() { # chart namespace values-file...
	local chart="$1" namespace="$2"
	shift 2
	local pulled
	pulled="$(helm pull "${CHARTS}/${chart}" --version "${VERSION}" --untar --untardir "${tmp}/charts" 2>&1)"
	if ! grep -qx "Digest: ${CHART_DIGEST[${chart}]}" <<<"${pulled}"; then
		echo "${chart} ${VERSION} is not ${CHART_DIGEST[${chart}]}:" >&2
		echo "${pulled}" >&2
		exit 1
	fi
	local args=()
	for f in "$@"; do args+=(-f "${f}"); done
	# The release names are the ones the charts expect: "substrate" renders
	# unprefixed object names, and substrate-atelet names its objects after
	# that release.
	helm template "${chart}" "${tmp}/charts/${chart}" --namespace "${namespace}" \
		"${args[@]}" --output-dir "${tmp}/rendered" >/dev/null
	mkdir -p "${OUT}/${chart}"
	for f in "${tmp}/rendered/${chart}/templates/"*.yaml; do
		{
			echo "# Rendered by render-upstream.sh from ${CHARTS}/${chart}:${VERSION}"
			echo "# (${CHART_DIGEST[${chart}]}) templates/$(basename "${f}"). Do not edit."
			grep -v '^# Source: ' "${f}"
		} >"${OUT}/${chart}/$(basename "${f}")"
	done
}

rm -rf "${OUT}"
mkdir -p "${OUT}"
render substrate-crds ate-system
render substrate-podcert podcertificate-controller-system "${VALUES}/podcert.yaml"
render substrate-atelet ate-system "${VALUES}/common.yaml" "${VALUES}/atelet.yaml"
render substrate ate-system "${VALUES}/common.yaml" "${VALUES}/substrate.yaml"

# Pin each component image, which the charts name by the appVersion tag, to
# its digest. Third-party images (agentgateway) arrive digest-pinned.
declare -A pinned
while read -r ref; do
	pinned[${ref}]="${ref}@$(crane digest "${ref}")"
done < <(grep -rhoE "${IMAGES}/[a-z0-9-]+:v${VERSION}\$" "${OUT}" | sort -u)
for ref in "${!pinned[@]}"; do
	grep -rlE "${ref}\$" "${OUT}" | xargs sed -i -E "s#${ref}\$#${pinned[${ref}]}#"
done
if grep -rnE "image: [^@]+\$" "${OUT}"; then
	echo "an image above is not pinned by digest" >&2
	exit 1
fi

# The bundled PostgreSQL and its role and schema setup, from the same release.
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v${VERSION}" \
	https://github.com/kagent-dev/substrate.git "${tmp}/src"
if [[ "$(git -C "${tmp}/src" rev-parse HEAD)" != "${COMMIT}" ]]; then
	echo "tag v${VERSION} is not commit ${COMMIT}" >&2
	exit 1
fi
{
	echo "# Rendered by render-upstream.sh from kagent-dev/substrate v${VERSION}"
	echo "# (${COMMIT}) manifests/ate-install/postgres/postgres.yaml. Do not edit."
	cat "${tmp}/src/manifests/ate-install/postgres/postgres.yaml"
} >"${OUT}/postgres.yaml"
{
	echo "-- Rendered by render-upstream.sh from kagent-dev/substrate v${VERSION}"
	echo "-- (${COMMIT}) pkg/postgressetup/setup.sql. Do not edit."
	cat "${tmp}/src/pkg/postgressetup/setup.sql"
} >"${OUT}/postgres-setup.sql"
echo "rendered $(find "${OUT}" -name '*.yaml' | wc -l) manifests and postgres-setup.sql into ${OUT}"
