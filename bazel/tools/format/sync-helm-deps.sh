#!/usr/bin/env bash
# Sync homelab-library dependency versions across all consuming charts.
#
# Reads the version from projects/shared/helm/homelab-library/chart/Chart.yaml
# and updates any consuming chart whose dependency version doesn't match.
# Rebuilds Chart.lock and charts/*.tgz when a mismatch is found.
#
# Usage:
#   sync-helm-deps.sh          # Check and fix all charts
#   sync-helm-deps.sh --check  # Check only, exit 1 if mismatched
set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}▶${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1" >&2; }

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
	CHECK_ONLY=true
fi

LIBRARY_CHART="projects/shared/helm/homelab-library/chart/Chart.yaml"
if [[ ! -f "$LIBRARY_CHART" ]]; then
	exit 0
fi

# Extract library version
LIBRARY_VERSION=$(grep '^version:' "$LIBRARY_CHART" | awk '{print $2}')
if [[ -z "$LIBRARY_VERSION" ]]; then
	err "Could not read version from $LIBRARY_CHART"
	exit 1
fi

# Find all Chart.yaml files that depend on homelab-library (excluding the library itself)
MISMATCHED=()
while IFS= read -r chart_file; do
	[[ "$chart_file" == "$LIBRARY_CHART" ]] && continue

	# Extract the version after "name: homelab-library"
	dep_version=$(awk '/name: homelab-library/{getline; if ($1 == "version:") print $2}' "$chart_file" | tr -d '"')
	if [[ -n "$dep_version" && "$dep_version" != "$LIBRARY_VERSION" ]]; then
		MISMATCHED+=("$chart_file")
	fi
done < <(grep -rl 'name: homelab-library' --include='Chart.yaml' projects/ 2>/dev/null || true)

if $CHECK_ONLY; then
	if [[ ${#MISMATCHED[@]} -gt 0 ]]; then
		err "homelab-library is $LIBRARY_VERSION but these charts reference an older version:"
		for f in "${MISMATCHED[@]}"; do
			err "  $f"
		done
		exit 1
	fi
	# Content drift is the other half, and the half a version comparison cannot
	# see. check_helm_deps.py is the authority on both, and is what CI runs.
	exec python3 ./bazel/tools/format/helm_deps/check_helm_deps.py --root .
fi

if [[ ${#MISMATCHED[@]} -gt 0 ]]; then
	log "Syncing homelab-library $LIBRARY_VERSION to ${#MISMATCHED[@]} chart(s)..."

	for chart_file in "${MISMATCHED[@]}"; do
		# Update the version in Chart.yaml (line after "name: homelab-library")
		sed -i '' "/name: homelab-library/{n;s/version: \"[^\"]*\"/version: \"$LIBRARY_VERSION\"/;}" "$chart_file"
		log "  Updated $chart_file"
	done
fi

# CONTENT drift, not just version drift. This used to rebuild only when a
# declared dependency version differed from the library's, which meant editing a
# library template WITHOUT bumping its version left the fix in the source while
# the stale committed tarball kept deploying: `helm_chart` globs `**/*`, so the
# tarball is what Bazel ships. Clean diff, green CI, nothing rolls. That trap
# nearly swallowed PR #4680's fix (issue #4682).
#
# The comparison lives in check_helm_deps.py rather than here so there is ONE
# definition of stale, it is unit tested, and CI can enforce it without helm.
if ! command -v helm &>/dev/null; then
	# Local-only by design: the CI format runner has no helm CLI. CI enforces
	# the same invariant read-only via check_helm_deps.py, so a missing helm
	# here means "cannot fix", never "nothing was wrong".
	exit 0
fi

STALE=()
while IFS= read -r chart_dir; do
	[[ -n "$chart_dir" ]] && STALE+=("$chart_dir")
done < <(python3 ./bazel/tools/format/helm_deps/check_helm_deps.py --root . --list-stale 2>/dev/null || true)

if [[ ${#STALE[@]} -eq 0 ]]; then
	exit 0
fi

log "Rebuilding ${#STALE[@]} chart(s) whose vendored tarballs drifted from source..."
for chart_dir in "${STALE[@]}"; do
	helm dependency update "$chart_dir" >/dev/null 2>&1 || true
	log "  Rebuilt $chart_dir"
done
