#!/usr/bin/env bash
# Emit the image tags to push, space-separated, for the runtime-arg push flow:
#
#   buck2 run //path:image.push -- $(buck2/image_tags.sh)
#
# This is the Buck2 analogue of bazel/tools/workspace_status.sh's
# STABLE_IMAGE_TAG (buck2 has no --stamp): the timestamped+sha tag, and nothing
# else. The second, sanitized-branch tag and the `dev-` prefix off main were
# dropped on both sides once it was clear their only consumer was ArgoCD Image
# Updater tag filtering, which this cluster does not run. Chart values pin by
# digest (deterministic); this tag is a human/registry-facing alias.
set -o errexit -o nounset -o pipefail

git_short_sha="$(git rev-parse --short HEAD)"

echo "$(date -u +"%Y.%m.%d.%H.%M.%S")-${git_short_sha}"
