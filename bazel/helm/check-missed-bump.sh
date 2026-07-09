#!/usr/bin/env bash
# check-missed-bump.sh: content-stable "missed chart bump" guard.
#
# Given a chart whose current version is ALREADY published, compare the image
# digests pinned in the freshly built chart against the ones in the published
# chart of the same version. If they differ, an image was rebuilt without a
# chart bump, so the merge would publish nothing and silently not deploy (or
# fail main's Push images idempotency check). Exit 1 with the exact bump command
# in that case; exit 0 otherwise.
#
# This is the same digest-content check push.sh.tpl runs post-merge on main,
# factored out so it can also run pre-merge on a PR branch and be unit tested
# with stubbed helm/crane. It is deliberately content-stable (compares manifest
# digests, not the build-timestamped tags) and FAILS OPEN: an unresolved digest
# or an unpublished version is treated as "nothing to assert", never a failure,
# so a transient registry error cannot wedge a PR.
#
# Usage:
#   HELM=/path/helm CRANE=/path/crane REPOSITORY=oci://ghcr.io/... \
#     check-missed-bump.sh <chart_name> <chart_version> <fresh_chart_tgz> <project_dir>
#
# Env:
#   HELM         path to the helm binary (required)
#   CRANE        path to the crane binary (required)
#   REPOSITORY   OCI chart repository (required), e.g. oci://ghcr.io/jomcgi/homelab/charts
set -o errexit -o nounset -o pipefail

CHART_NAME="${1:?chart_name required}"
CHART_VERSION="${2:?chart_version required}"
CHART_TGZ="${3:?fresh chart .tgz required}"
PROJECT_DIR="${4:?project_dir required}"

HELM="${HELM:?HELM env required}"
CRANE="${CRANE:?CRANE env required}"
REPOSITORY="${REPOSITORY:?REPOSITORY env required}"

# Resolve one repo:tag to its manifest digest, retrying to absorb ghcr
# propagation lag / transient rate limits. A genuinely gone tag ends UNRESOLVED,
# which the caller treats as "skip", not "fail".
_crane_digest() {
	local ref="$1" i d
	for i in 1 2 3; do
		if d=$("$CRANE" digest "$ref" 2>/dev/null) && [[ -n "$d" ]]; then
			printf '%s' "$d"
			return 0
		fi
		if [[ $i -lt 3 ]]; then sleep 3; fi
	done
	printf 'UNRESOLVED'
}

# Emit sorted "repo=digest" lines for each ghcr.io/jomcgi image pinned in a
# chart. Args pass straight to `helm show values` (a .tgz path, or
# "REPO/NAME --version X"). The pinned tag embeds a build timestamp so it
# changes every build even for identical content; resolving it to the manifest
# digest gives a content-stable identity to compare.
_image_digests() {
	"$HELM" show values "$@" 2>/dev/null | awk '
    /^[[:space:]]*repository:[[:space:]]*/ { repo=$2 }
    /^[[:space:]]*tag:[[:space:]]*/ && repo!="" { print repo"\t"$2; repo="" }' |
		while IFS="$(printf '\t')" read -r repo tag; do
			case "$repo" in
			ghcr.io/jomcgi/*)
				printf '%s=%s\n' "$repo" "$(_crane_digest "${repo}:${tag}")"
				;;
			esac
		done | sort
}

# Only meaningful when this version is already published. If `helm show chart`
# fails, the version is new (this PR bumped it) and there is nothing to assert.
if ! "$HELM" show chart "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}" >/dev/null 2>&1; then
	echo "check-missed-bump: ${CHART_NAME} ${CHART_VERSION} is not published yet; nothing to check."
	exit 0
fi

FRESH_DIGESTS=$(_image_digests "$CHART_TGZ") || FRESH_DIGESTS=""
PUB_DIGESTS=$(_image_digests "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}") || PUB_DIGESTS=""

# Fail open on any unresolved or empty digest set: a registry hiccup must not
# block a PR. The version-scoped detector on main is the backstop for these.
if [[ "$FRESH_DIGESTS" == *UNRESOLVED* ]] || [[ "$PUB_DIGESTS" == *UNRESOLVED* ]]; then
	echo "check-missed-bump: WARNING: could not resolve all image digests for ${CHART_NAME}; skipping (fail open)." >&2
	exit 0
fi
if [[ -z "$FRESH_DIGESTS" ]] || [[ -z "$PUB_DIGESTS" ]]; then
	echo "check-missed-bump: no ghcr.io/jomcgi images pinned in ${CHART_NAME}; nothing to check."
	exit 0
fi

if [[ "$FRESH_DIGESTS" != "$PUB_DIGESTS" ]]; then
	{
		echo "ERROR: ${CHART_NAME} ${CHART_VERSION} is already published, but its pinned image digests differ from the published chart (an image was rebuilt)."
		echo "This PR will NOT deploy until the chart version is bumped."
		echo ""
		echo "Fix: in a fresh worktree run"
		echo "  bazel/tools/git/bump-chart.sh ${PROJECT_DIR}"
		echo "then commit the bump to this PR."
	} >&2
	exit 1
fi

echo "check-missed-bump: ${CHART_NAME} ${CHART_VERSION} image digests match the published chart; OK."
exit 0
