#!/usr/bin/env bash
# Render Agent Substrate's install manifests at the pinned release into
# resources/upstream/.
#
# The factory runs the kagent-dev fork of Agent Substrate, the release line
# kagent v1.0.1 uses (it builds against fork v1.0.0). The fork publishes four Helm charts. This script
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
# Fork overlay (gVisor sandbox fork slice 1). The ateapi, atecontroller and
# atelet images and the WorkerPool CRD come from the jomcgi-org/substrate fork
# at FORK_COMMIT instead of the release. The fork changes no other chart, so
# every other object and image stays at VERSION. The fork publishes no charts:
# the CRD is rendered from the checkout. The images are the ones the manual
# workflow .github/workflows/substrate-fork-images.yml published, pinned by
# digest. ForkSeed (values/common.yaml) is on since step 7: it changes no actor
# without a template that sets fork_config. The overlay is not optional: every
# run renders the fork images. To return to the release, revert the change or edit this
# script.
#
# Rerun after changing VERSION, COMMIT, FORK_COMMIT or a chart digest; review
# the diff of upstream/. Needs git, helm and crane.
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

FORK_REPO="https://github.com/jomcgi-org/substrate.git"
FORK_COMMIT="7edec5939cbaf9b21811230dafa0fdb8b5127d2d"
FORK_IMAGES="ghcr.io/jomcgi-org/homelab/substrate-fork"
FORK_TAG="fork-${FORK_COMMIT:0:12}"
declare -A FORK_DIGEST=(
	["ateapi"]="sha256:6798bf9af31eb1f3134eb5f1eef6f07edc8120b9784de3bb1488aec98216a931"
	["atecontroller"]="sha256:a6cb2d6b48f26e832b414d8f46ba2974cd0503ecb1c015f4ce089359b1d146c9"
	["atelet"]="sha256:25e89f5ac3a6f42fc6cedd4adbfb90ebd3be2cea54c9c75c847d3d0ef06cfc2e"
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
# Fork overlay: the CRD from the fork's chart, then the three images.
mkdir "${tmp}/fork"
git -C "${tmp}/fork" init --quiet
git -C "${tmp}/fork" fetch --quiet --depth 1 "${FORK_REPO}" "${FORK_COMMIT}"
git -C "${tmp}/fork" -c advice.detachedHead=false checkout --quiet FETCH_HEAD
if [[ "$(git -C "${tmp}/fork" rev-parse HEAD)" != "${FORK_COMMIT}" ]]; then
	echo "fetched commit is not ${FORK_COMMIT}" >&2
	exit 1
fi
overlay_note="# Overlaid by render-upstream.sh from ${FORK_REPO%.git} ${FORK_COMMIT} (fork overlay)."
helm template substrate-crds "${tmp}/fork/charts/substrate-crds" --namespace ate-system --output-dir "${tmp}/fork-rendered" >/dev/null
{
	echo "# Rendered by render-upstream.sh from ${FORK_REPO%.git} charts/substrate-crds"
	echo "# at ${FORK_COMMIT} templates/ate.dev_workerpools.yaml. Do not edit."
	grep -v '^# Source: ' "${tmp}/fork-rendered/substrate-crds/templates/ate.dev_workerpools.yaml"
} >"${OUT}/substrate-crds/ate.dev_workerpools.yaml"
declare -A fork_files=(
	["ateapi"]="substrate/ate-api-server.yaml"
	["atecontroller"]="substrate/ate-controller.yaml"
	["atelet"]="substrate-atelet/atelet.yaml"
)
for component in "${!FORK_DIGEST[@]}"; do
	got="$(crane digest "${FORK_IMAGES}/${component}:${FORK_TAG}")"
	if [[ "${got}" != "${FORK_DIGEST[${component}]}" ]]; then
		echo "${FORK_IMAGES}/${component}:${FORK_TAG} is ${got}, not ${FORK_DIGEST[${component}]}" >&2
		exit 1
	fi
	file="${OUT}/${fork_files[${component}]}"
	release_ref="${IMAGES}/${component}:v${VERSION}"
	old="${pinned[${release_ref}]}"
	new="${FORK_IMAGES}/${component}:${FORK_TAG}@${FORK_DIGEST[${component}]}"
	if [[ "$(grep -cF "image: ${old}" "${file}")" != 1 ]]; then
		echo "${file} does not hold ${old} once" >&2
		exit 1
	fi
	sed -i "s#image: ${old}#image: ${new}#" "${file}"
	sed -i "2a\\${overlay_note}\n# The ${component} image is the fork build." "${file}"
done

echo "rendered $(find "${OUT}" -name '*.yaml' | wc -l) manifests and postgres-setup.sql into ${OUT}"
