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
#
# CHART_VERSION_ALL_PATHS ignores the dependency closure and counts commits
# across the WHOLE repo. It exists for the caller that already knows the chart's
# content changed, because it compared image digests, and only needs a version
# number that is deterministic per commit. The closure query cannot be trusted
# in that situation: an incomplete package set is exactly how a real content
# change gets reported as "no bump needed" (see the digest-authority branch in
# push.sh.tpl). Counting repo-wide overcounts, which is harmless, rather than
# undercounting, which silently skips a deploy.
DEP_DIRS=""
if [[ -n "${CHART_VERSION_ALL_PATHS:-}" ]]; then
	echo >&2 "INFO: CHART_VERSION_ALL_PATHS set; counting commits repo-wide instead of over the dependency closure."
	DEP_DIRS="."
elif [[ -n "$BAZEL_PACKAGE" ]]; then
	# Query Bazel for transitive source deps. --keep_going is load-bearing: a
	# dependency whose external closure fails to preload (e.g. an apko image
	# layer pulling wolfi packages) otherwise fails the WHOLE query, and we
	# silently under-scope to the chart dir below, missing image-content changes
	# that must bump the chart. With --keep_going the query still emits the
	# main-repo packages it could load, including the guest package where content
	# like recipes lives. See oci_image_info(image=...), which widens this closure
	# on purpose so a change layered into a pinned image bumps the dependent chart.
	DEP_DIRS=$(bazel query "deps(${BAZEL_PACKAGE})" --output=package --keep_going 2>/dev/null |
		grep -v '^@' |
		sed 's|^//||' ||
		true)
fi

if [[ -z "$DEP_DIRS" ]]; then
	# Fallback: chart directory only. This is a DEGRADED mode: a change to a
	# dependency outside the chart dir (application code, a pinned image's
	# content) will NOT bump the chart. Logged as a warning so a regression to
	# dir-only scoping is visible in CI instead of surfacing later as a missing
	# bump that needs a manual chart version fix.
	echo >&2 "WARNING: Bazel dependency query returned nothing for ${BAZEL_PACKAGE}; falling back to chart-dir-only scoping (${CHART_DIR}). Dependency-scoped version bumping is DISABLED for this run."
	DEP_DIRS="$CHART_DIR"
fi

# Convert package paths to -- path arguments for git log
GIT_PATHS=()
while IFS= read -r dir; do
	[[ -n "$dir" ]] && GIT_PATHS+=("$dir")
done <<<"$DEP_DIRS"

# --- Find conventional commits since last version ---
#
# The serial component is derived from the COMMIT, never from "read the current
# version and add one". Publishing moved post-merge (ADR platform/009 decision
# 1), and BuildBuddy exempts the default branch from superseded-run
# cancellation, so two merges in quick succession run two CONCURRENT publishes.
# Both read the same Chart.yaml, so a +1 scheme makes both compute the SAME next
# version: the first publishes it, and the second then no-ops against the
# idempotent registry check, so its images ship under no version at all. That is
# the silent non-deploy ADR platform/011 exists to prevent.
#
# Counting qualifying commits instead makes the version a function of HEAD. A
# later commit necessarily contains the earlier one, so its count is strictly
# greater and the two cannot collide however the jobs interleave.
#
# Commits are walked OLDEST FIRST (--reverse) because the minor/major boundary
# is positional: after a feat, the patch component counts commits AFTER that
# feat, so the boundary index has to be known as the walk proceeds.
BUMP="none"
COMMIT_COUNT=0
MINOR_BOUNDARY=-1 # index of the first commit to force minor-or-higher
MAJOR_BOUNDARY=-1 # index of the first commit to force major

while IFS= read -r subject; do
	[[ -z "$subject" ]] && continue

	# Skip automated commits
	case "$subject" in
	*"ci-format-bot"* | *"chart-version-bot"*) continue ;;
	esac

	INDEX="$COMMIT_COUNT"
	COMMIT_COUNT=$((COMMIT_COUNT + 1))

	# Check for breaking change (! before colon)
	# Pre-1.0: breaking changes bump minor (semver allows breaking changes in 0.x)
	# Post-1.0: breaking changes bump major
	BREAKING_RE='^[a-z]+(\([^)]*\))?!:'
	if [[ "$subject" =~ $BREAKING_RE ]]; then
		IFS='.' read -r CUR_MAJOR _ _ <<<"$CURRENT_VERSION"
		if [[ "$CUR_MAJOR" -ge 1 ]]; then
			[[ "$MAJOR_BOUNDARY" -lt 0 ]] && MAJOR_BOUNDARY="$INDEX"
			BUMP="major"
		else
			[[ "$MINOR_BOUNDARY" -lt 0 ]] && MINOR_BOUNDARY="$INDEX"
			[[ "$BUMP" != "major" ]] && BUMP="minor"
		fi
		# No `break`: the walk must reach HEAD to count every qualifying commit.
		continue
	fi

	# Check commit type
	TYPE=$(echo "$subject" | sed -E -n 's/^([a-z]+)(\([^)]*\))?:.*/\1/p')
	case "$TYPE" in
	feat)
		[[ "$MINOR_BOUNDARY" -lt 0 ]] && MINOR_BOUNDARY="$INDEX"
		[[ "$BUMP" != "major" ]] && BUMP="minor"
		;;
	fix | perf | refactor | style | docs | test | ci | build | chore | revert)
		[[ "$BUMP" == "none" ]] && BUMP="patch"
		;;
	esac
done < <(git log --reverse --format='%an|||%s' "${VERSION_COMMIT}..HEAD" -- "${GIT_PATHS[@]}" 2>/dev/null |
	grep -v '^\(ci-format-bot\|chart-version-bot\)|||' |
	sed 's/^[^|]*|||//')

# --- Apply bump ---
if [[ "$BUMP" == "none" ]]; then
	echo >&2 "INFO: No conventional commits found since ${CURRENT_VERSION}, no bump needed"
	echo "$CURRENT_VERSION"
	exit 0
fi

# The serial is the number of qualifying commits AFTER the boundary. The last
# commit in the range is the boundary itself when nothing followed it, which is
# why this is (count - 1 - boundary) and not (count - boundary).
IFS='.' read -r MAJOR MINOR PATCH <<<"$CURRENT_VERSION"
case "$BUMP" in
major)
	MAJOR=$((MAJOR + 1))
	MINOR=0
	PATCH=$((COMMIT_COUNT - 1 - MAJOR_BOUNDARY))
	;;
minor)
	MINOR=$((MINOR + 1))
	PATCH=$((COMMIT_COUNT - 1 - MINOR_BOUNDARY))
	;;
patch) PATCH=$((PATCH + COMMIT_COUNT)) ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
echo >&2 "INFO: Bumping ${CURRENT_VERSION} -> ${NEW_VERSION} (${BUMP}, ${COMMIT_COUNT} commit(s))"
echo "$NEW_VERSION"
