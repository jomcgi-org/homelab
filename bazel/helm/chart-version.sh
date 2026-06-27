#!/usr/bin/env bash
# Compute the next semver version for a Helm chart based on conventional commits
# scoped to the chart's Bazel dependency closure.
#
# Usage: chart-version.sh <chart-dir> [<bazel-package-label>]
# Output: Next semver version to stdout (e.g., "0.9.0")
#         Outputs current version if no bump needed.
#
# Requires: git, bazel (optional — falls back to chart-dir-only scoping)
set -o errexit -o nounset -o pipefail

CHART_DIR="${1:?Usage: chart-version.sh <chart-dir>}"
BAZEL_PACKAGE="${2:-}"

# --- Read current version from Chart.yaml ---
CHART_YAML="${CHART_DIR}/Chart.yaml"
if [[ ! -f "$CHART_YAML" ]]; then
	echo >&2 "ERROR: Chart.yaml not found at $CHART_YAML"
	exit 1
fi

CURRENT_VERSION=$(grep '^version:' "$CHART_YAML" | head -1 | awk '{print $2}' | tr -d '"')
if [[ -z "$CURRENT_VERSION" ]]; then
	echo >&2 "ERROR: Could not parse version from $CHART_YAML"
	exit 1
fi

# --- Find the commit where this version was last set ---
VERSION_COMMIT=$(git log -1 --format=%H -S"version: ${CURRENT_VERSION}" -- "$CHART_YAML" 2>/dev/null || true)
if [[ -z "$VERSION_COMMIT" ]]; then
	# No previous version commit found (first run or initial version)
	echo >&2 "INFO: No previous version commit found for ${CURRENT_VERSION}, returning current version"
	echo "$CURRENT_VERSION"
	exit 0
fi

# --- Determine dependency directories ---
DEP_DIRS=""
if [[ -n "$BAZEL_PACKAGE" ]]; then
	# Query Bazel for transitive source deps
	DEP_DIRS=$(bazel query "deps(${BAZEL_PACKAGE})" --output=package 2>/dev/null |
		grep -v '^@' |
		sed 's|^//||' ||
		true)
fi

if [[ -z "$DEP_DIRS" ]]; then
	# Fallback: use chart directory only
	echo >&2 "INFO: Bazel query unavailable or returned no results, using chart dir only"
	DEP_DIRS="$CHART_DIR"
fi

# Convert package paths to -- path arguments for git log
GIT_PATHS=()
while IFS= read -r dir; do
	[[ -n "$dir" ]] && GIT_PATHS+=("$dir")
done <<<"$DEP_DIRS"

# --- Find conventional commits since last version ---
BUMP="none"

while IFS= read -r subject; do
	[[ -z "$subject" ]] && continue

	# Skip automated commits
	case "$subject" in
	*"ci-format-bot"* | *"chart-version-bot"*) continue ;;
	esac

	# Check for breaking change (! before colon)
	# Pre-1.0: breaking changes bump minor (semver allows breaking changes in 0.x)
	# Post-1.0: breaking changes bump major
	BREAKING_RE='^[a-z]+(\([^)]*\))?!:'
	if [[ "$subject" =~ $BREAKING_RE ]]; then
		IFS='.' read -r CUR_MAJOR _ _ <<<"$CURRENT_VERSION"
		if [[ "$CUR_MAJOR" -ge 1 ]]; then
			BUMP="major"
		else
			BUMP="minor"
		fi
		break # Can't go higher
	fi

	# Check commit type
	TYPE=$(echo "$subject" | sed -E -n 's/^([a-z]+)(\([^)]*\))?:.*/\1/p')
	case "$TYPE" in
	feat)
		[[ "$BUMP" != "major" ]] && BUMP="minor"
		;;
	fix | perf | refactor | style | docs | test | ci | build | chore | revert)
		[[ "$BUMP" == "none" ]] && BUMP="patch"
		;;
	esac
done < <(git log --format='%an|||%s' "${VERSION_COMMIT}..HEAD" -- "${GIT_PATHS[@]}" 2>/dev/null |
	grep -v '^\(ci-format-bot\|chart-version-bot\)|||' |
	sed 's/^[^|]*|||//')

# --- Apply bump ---
if [[ "$BUMP" == "none" ]]; then
	echo >&2 "INFO: No conventional commits found since ${CURRENT_VERSION}, no bump needed"
	echo "$CURRENT_VERSION"
	exit 0
fi

IFS='.' read -r MAJOR MINOR PATCH <<<"$CURRENT_VERSION"
case "$BUMP" in
major)
	MAJOR=$((MAJOR + 1))
	MINOR=0
	PATCH=0
	;;
minor)
	MINOR=$((MINOR + 1))
	PATCH=0
	;;
patch) PATCH=$((PATCH + 1)) ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
echo >&2 "INFO: Bumping ${CURRENT_VERSION} -> ${NEW_VERSION} (${BUMP})"
echo "$NEW_VERSION"
