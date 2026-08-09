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
# with a stubbed helm. It is content-stable (compares manifest digests, not the
# build-timestamped tags) and reads those digests straight from the chart
# values, so it needs no registry access and does not require the fresh images
# to have been pushed. A chart with nothing digest-pinned fails OPEN, but an
# unpublished version is only "nothing to assert" when this PR itself carries
# the bump. When
# origin/main's Chart.yaml claims the SAME unpublished version, main's publish
# is either still in flight or has failed; that is the state a rebase-merge
# version collision leaves behind (two PRs claimed the same version, the
# loser's bump hunks were silently emptied at merge). The guard waits briefly
# for main's publish to land and then digest-compares; if it never lands, it
# fails CLOSED rather than let another unverifiable PR merge on top.
#
# Usage:
#   HELM=/path/helm REPOSITORY=oci://ghcr.io/... \
#     check-missed-bump.sh <chart_name> <chart_version> <fresh_chart_tgz> <project_dir>
#
# Env:
#   HELM                path to the helm binary (required)
#   REPOSITORY          OCI chart repository (required), e.g. oci://ghcr.io/jomcgi/homelab/charts
#   MAIN_CHART_VERSION  the chart version origin/main's Chart.yaml claims
#                       (optional; empty disables the fail-closed collision path)
#   PUBLISH_WAIT_TRIES  polls while waiting for main's publish (default 10)
#   PUBLISH_WAIT_SECS   seconds between polls (default 30)
set -o errexit -o nounset -o pipefail

CHART_NAME="${1:?chart_name required}"
CHART_VERSION="${2:?chart_version required}"
CHART_TGZ="${3:?fresh chart .tgz required}"
PROJECT_DIR="${4:?project_dir required}"

HELM="${HELM:?HELM env required}"
REPOSITORY="${REPOSITORY:?REPOSITORY env required}"

# Emit sorted "repo=digest" lines for each ghcr.io/jomcgi image pinned in a
# chart. Args pass straight to `helm show values` (a .tgz path, or
# "REPO/NAME --version X").
#
# The digest is read straight out of the chart values, where helm_images_values
# (bazel/helm/images.bzl) pins it next to repository and tag, and where the
# templates read it: charts deploy `repository@digest`. It used to be obtained
# by resolving the pinned `tag:` through `crane digest`, which was equivalent
# but required the fresh build's images to ALREADY BE IN THE REGISTRY. That
# coupled the guard to PR image pushes: with those gone (buildbuddy.yaml builds
# images on PRs instead of pushing them), the fresh tag no longer exists in ghcr
# and every lookup would have failed to UNRESOLVED, silently fail-opening the
# guard on every PR. Reading the pinned digest needs no registry at all.
#
# A ghcr image with no `digest:` is pinned by tag only (e.g. a floating
# `tag: main` that helm_images_values does not inject into). Those are SKIPPED
# rather than failing the whole chart open. They were never comparable: the old
# crane-based check resolved the identical `repo:tag` ref on both the fresh and
# the published side, so it always got the same answer and could not detect
# drift. Letting one of them suppress the chart would lose the real check on
# its digest-pinned siblings, which is what oci-model-cache-operator (pinned
# oci-model-cache alongside a tag-only hf2oci) would have done.
_image_digests() {
	"$HELM" show values "$@" 2>/dev/null | awk '
    function flush() {
      if (repo != "") { print repo "\t" digest }
      repo = ""; digest = ""
    }
    /^[[:space:]]*repository:[[:space:]]*/ { flush(); repo=$2 }
    /^[[:space:]]*digest:[[:space:]]*/ && repo!="" { digest=$2 }
    END { flush() }' |
		while IFS="$(printf '\t')" read -r repo digest; do
			case "$repo" in
			ghcr.io/jomcgi/*)
				if [[ -z "$digest" ]]; then
					echo "check-missed-bump: ${repo} is pinned by tag only (no digest in values); not comparable, skipping it." >&2
					continue
				fi
				printf '%s=%s\n' "$repo" "$digest"
				;;
			esac
		done | sort
}

_published() {
	"$HELM" show chart "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}" >/dev/null 2>&1
}

# The digest comparison is only meaningful when this version is already
# published. Unpublished splits two ways on whether origin/main claims it:
#   - main claims a DIFFERENT version: this PR carries the bump; nothing to
#     assert (publish happens post-merge).
#   - main claims the SAME version: the bump came from a main commit whose
#     publish has not landed. Normally that is a minutes-wide window while
#     main's Push images runs, so wait for it and then compare digests. If it
#     never lands, main's publish failed and nothing about this PR can be
#     verified: fail closed with the fix, instead of merging blind.
if ! _published; then
	if [[ -n "${MAIN_CHART_VERSION:-}" && "$MAIN_CHART_VERSION" == "$CHART_VERSION" ]]; then
		TRIES="${PUBLISH_WAIT_TRIES:-10}"
		SECS="${PUBLISH_WAIT_SECS:-30}"
		echo "check-missed-bump: ${CHART_NAME} ${CHART_VERSION} matches origin/main but is not in the registry yet; waiting up to $((TRIES * SECS))s for main's publish."
		PUBLISHED=""
		for _ in $(seq 1 "$TRIES"); do
			sleep "$SECS"
			if _published; then
				PUBLISHED=1
				break
			fi
		done
		if [[ -z "$PUBLISHED" ]]; then
			{
				echo "ERROR: origin/main pins ${CHART_NAME} ${CHART_VERSION} but that version is not in the registry."
				echo "Main's chart publish is still running or has FAILED (check the latest main 'Push images' run)."
				echo "This PR does not bump the chart, so its images cannot be verified against a published baseline."
				echo ""
				echo "If this PR rebuilds any image the chart pins, bump in this PR:"
				echo "  bazel/tools/git/bump-chart.sh ${PROJECT_DIR}"
				echo "Otherwise re-run this check once main's Push images is green."
			} >&2
			exit 1
		fi
	else
		echo "check-missed-bump: ${CHART_NAME} ${CHART_VERSION} is not published yet (this PR carries the bump); nothing to check."
		exit 0
	fi
fi

FRESH_DIGESTS=$(_image_digests "$CHART_TGZ") || FRESH_DIGESTS=""
PUB_DIGESTS=$(_image_digests "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}") || PUB_DIGESTS=""

# Fail open when either side has nothing digest-pinned to compare (a chart with
# only third-party or tag-only images, or one published before digest pinning).
# The version-scoped detector on main is the backstop for these.
if [[ -z "$FRESH_DIGESTS" ]] || [[ -z "$PUB_DIGESTS" ]]; then
	echo "check-missed-bump: no digest-pinned ghcr.io/jomcgi images to compare in ${CHART_NAME}; nothing to check."
	exit 0
fi

if [[ "$FRESH_DIGESTS" != "$PUB_DIGESTS" ]]; then
	{
		echo "ERROR: ${CHART_NAME} ${CHART_VERSION} is already published, but its pinned image digests differ from the published chart (an image was rebuilt)."
		echo ""
		echo "Which images differ (< published, > freshly built):"
		diff <(printf '%s\n' "$PUB_DIGESTS") <(printf '%s\n' "$FRESH_DIGESTS") | sed 's/^/  /' || true
		echo ""
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
