#!/usr/bin/env bash
# Produces space-separated key-values for stamp variables.
# Those starting with "STABLE_" will cause actions to re-run when they change.
# See https://docs.aspect.build/rulesets/aspect_bazel_lib/docs/stamping/
set -o errexit -o nounset -o pipefail

git_commit=$(git rev-parse HEAD)
git_short_sha=$(git rev-parse --short HEAD)

# Follows https://blog.aspect.build/versioning-releases-from-a-monorepo
auto_version=$(
	git describe --tags --long --match="[0-9][0-9][0-9][0-9].[0-9][0-9]" 2>/dev/null |
		sed -e 's/-/./;s/-g/-/' || echo "0.0.0"
)

# Generate timestamp-based image tag: YYYY.MM.DD.HH.MM.SS-shortsha
#
# The timestamp is the COMMIT date, not the wall clock, so the tag is a function
# of the commit alone. Rebuilding the same commit republishes the same ghcr tag
# instead of minting a new one per push run, which matches how the rest of the
# image pipeline already works (SOURCE_DATE_EPOCH=0 in .bazelrc makes apko
# digests reproducible; a wall-clock tag undid that property one layer up).
#
# Same sortable YYYY.MM.DD.HH.MM.SS-shortsha format, and it still changes on
# every new commit, so each merge to main still publishes a distinct tag.
#
# Note the semantic: the tag now encodes when the commit was authored, not when
# the image was built. Those differ for a rebuild or a rebase.
#
# This is a reproducibility change, not a performance one. Stamp-related remote
# cache churn was a separate problem, fixed by dropping `common:ci --stamp`
# (PR #4038); making this value deterministic moved cache misses only 524 -> 496,
# because volatile-status.txt is the binding constraint there, not this key.
base_image_tag=$(TZ=UTC git show -s --format=%cd --date=format-local:%Y.%m.%d.%H.%M.%S HEAD)-${git_short_sha}

# No branch-derived stamp value is emitted. STABLE_BRANCH_TAG (a sanitized
# branch name published as a second image tag) and the `dev-` prefix applied to
# STABLE_IMAGE_TAG off main both existed for one consumer: ArgoCD Image Updater
# tag filtering. This cluster has no Image Updater, and charts deploy images by
# repository@digest (bazel/helm/images.bzl), so neither value ever reached a
# running workload. CI also no longer pushes images off main (buildbuddy.yaml),
# which left the CI-env branch-detection cascade that used to live here feeding
# nothing but a debug echo.

cat <<EOF
STABLE_GIT_COMMIT ${git_commit}
STABLE_MONOREPO_VERSION ${auto_version}
STABLE_IMAGE_TAG ${base_image_tag}
EOF
