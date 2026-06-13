#!/usr/bin/env bash
# Emit the image tags to push, space-separated, for the runtime-arg push flow:
#
#   buck2 run //path:image.push -- $(tools/buck2/image_tags.sh)
#
# This is the Buck2 analogue of bazel/tools/workspace_status.sh's STABLE_IMAGE_TAG
# / STABLE_BRANCH_TAG (buck2 has no --stamp): the timestamped+sha tag plus the
# sanitized branch. On non-main branches the timestamp tag is dev-prefixed so
# ArgoCD Image Updater ignores it (matching the bazel side). Chart values pin by
# digest (deterministic); these tags are human/registry-facing aliases.
set -o errexit -o nounset -o pipefail

git_short_sha="$(git rev-parse --short HEAD)"
base_tag="$(date -u +"%Y.%m.%d.%H.%M.%S")-${git_short_sha}"

# Branch from CI env (GitHub/BuildBuddy/etc.) or git, sanitized for tags.
branch="${GITHUB_REF_NAME:-${GIT_BRANCH:-${BUILDKITE_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}}}"
branch_tag="$(echo "${branch}" | tr '/' '-' | tr '[:upper:]' '[:lower:]')"

if [ "${branch}" = "main" ]; then
	image_tag="${base_tag}"
else
	image_tag="dev-${base_tag}"
fi

echo "${image_tag} ${branch_tag}"
