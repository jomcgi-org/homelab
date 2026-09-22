#!/usr/bin/env bash
# Compute the next semver version for a Helm chart based on conventional commits
# scoped to the chart's Bazel dependency closure.
#
# Usage: chart-version.sh <chart-dir> [<bazel-package-label>]
# Output: Next semver version to stdout (e.g., "0.9.0")
#         Outputs current version if no bump needed.
#
# Requires: git, bazel when a Bazel package label is supplied
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

# --- Refuse to compute anything from a truncated history ---
#
# Every number below comes out of a commit walk, so the whole computation
# assumes the history is actually present. In a SHALLOW clone it is not, and it
# fails silently rather than obviously: at the graft boundary every file reads
# as newly ADDED, so the -S search below matches the boundary commit itself. At
# depth 1 that is HEAD, the HEAD..HEAD range is empty by definition, and this
# script cheerfully reports "no bump needed" for a chart whose content changed.
#
# A partial depth is worse than depth 1, because it returns a plausible WRONG
# number instead of an obviously stuck one: this repo computes embervm 0.2.8 at
# depth 10 and 0.2.30 with the full history. That breaks the property the
# serial arithmetic below is built on, that the version is a function of the
# COMMIT. Under a shallow clone it is a function of the commit AND the fetch
# depth, so two publishes of the same commit on differently-deepened runners
# disagree, and the shallower one computes the lower version.
#
# This is not hypothetical: it is what took main's publish down on 2026-08-10.
# BuildBuddy clones shallow, every chart reported "no bump needed", and the
# publish either skipped silently or died in push.sh.tpl's escalation. The
# pr-checks main run now deepens the clone before publishing (buildbuddy.yaml);
# this is the backstop for any caller that does not, and it fails loudly because
# a wrong version is far more expensive than a red build: it wedges ArgoCD.
if [[ "$(git rev-parse --is-shallow-repository 2>/dev/null || echo false)" == "true" ]]; then
	echo >&2 "ERROR: refusing to compute a chart version in a shallow repository."
	echo >&2 "The commit walk would be truncated, making the version a function of the fetch depth rather than of the commit."
	echo >&2 "Fix: git fetch --unshallow"
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

# --- Determine release inputs ---
#
# CHART_VERSION_ALL_PATHS ignores the dependency closure and counts commits
# across the WHOLE repo. It exists for the caller that already knows the chart's
# content changed, because it compared image digests, and only needs a version
# number that is deterministic per commit. The closure query cannot be trusted
# in that situation: an incomplete package set is exactly how a real content
# change gets reported as "no bump needed" (see the digest-authority branch in
# push.sh.tpl). Counting repo-wide overcounts, which is harmless, rather than
# undercounting, which silently skips a deploy.
INPUT_PATHS=()
RENAME_GUARD_DIRS=()

append_unique() {
	local value="$1" existing
	shift
	for existing in "$@"; do
		[[ "$existing" == "$value" ]] && return 1
	done
	return 0
}

add_input_path() {
	local path="$1"
	if append_unique "$path" "${INPUT_PATHS[@]}"; then
		INPUT_PATHS+=("$path")
	fi
}

add_guard_dir() {
	local dir="$1"
	[[ -z "$dir" ]] && dir="."
	if append_unique "$dir" "${RENAME_GUARD_DIRS[@]}"; then
		RENAME_GUARD_DIRS+=("$dir")
	fi
}

use_all_paths() {
	INPUT_PATHS=(".")
	RENAME_GUARD_DIRS=()
}

if [[ -n "${CHART_VERSION_ALL_PATHS:-}" ]]; then
	echo >&2 "INFO: CHART_VERSION_ALL_PATHS set; counting commits repo-wide instead of over the dependency closure."
	use_all_paths
elif [[ -n "$BAZEL_PACKAGE" ]]; then
	# The package closure is deliberately converted to concrete source files.
	# Whole Bazel package directories also contain design notes, tests and STPA
	# fragments that do not affect a chart or a pinned image. Source files in the
	# chart.package closure do: this includes chart files, image inputs, shared
	# sources, apko configuration and the BUILD and .bzl files that define them.
	QUERY_ROOT="deps(${BAZEL_PACKAGE})"

	# Deployment values are inputs to the rendered release but are not inputs to
	# chart.package. Add the repository's explicit render targets for the nearest
	# project deploy directories. Application and kustomization files are also
	# release inputs even though the render action does not read them.
	PROJECT_ROOT="$CHART_DIR"
	while [[ "$PROJECT_ROOT" != "." ]] && [[ "$PROJECT_ROOT" != "/" ]] &&
		[[ ! -d "$PROJECT_ROOT/deploy" ]] && [[ ! -d "$PROJECT_ROOT/dev/deploy" ]]; do
		PROJECT_ROOT=$(dirname "$PROJECT_ROOT")
	done
	for deploy_dir in "$PROJECT_ROOT/deploy" "$PROJECT_ROOT/dev/deploy"; do
		[[ -d "$deploy_dir" ]] || continue
		deploy_build=""
		for candidate in "$deploy_dir/BUILD" "$deploy_dir/BUILD.bazel"; do
			[[ -f "$candidate" ]] && deploy_build="$candidate" && break
		done
		if [[ -n "$deploy_build" ]]; then
			deploy_package="//${deploy_dir}:"
			for render_target in render_manifests render_manifests_gke; do
				if grep -q "name = \"${render_target}\"" "$deploy_build"; then
					QUERY_ROOT+=" union deps(${deploy_package}${render_target})"
				fi
			done
		fi
		for deploy_input in "$deploy_dir/application.yaml" "$deploy_dir/kustomization.yaml"; do
			if git ls-files --error-unmatch -- "$deploy_input" >/dev/null 2>&1; then
				add_input_path "$deploy_input"
			fi
		done
	done

	QUERY_EXPR="kind(\"source file\", ${QUERY_ROOT}) union buildfiles(${QUERY_ROOT})"
	QUERY_ERROR=$(mktemp)
	set +e
	QUERY_OUTPUT=$(bazel query "$QUERY_EXPR" --output=label --keep_going 2>"$QUERY_ERROR")
	QUERY_STATUS=$?
	set -e
	if [[ $QUERY_STATUS -ne 0 ]]; then
		echo >&2 "WARNING: Bazel release-input query failed for ${BAZEL_PACKAGE}; counting commits repo-wide so partial output cannot hide a release input."
		head -5 "$QUERY_ERROR" >&2 || true
		use_all_paths
	else
		UNSUPPORTED_QUERY_OUTPUT="false"
		QUERY_PATH_COUNT=0
		while IFS= read -r label; do
			[[ -z "$label" ]] && continue
			# External repository sources are immutable inputs selected by main-repo
			# MODULE and BUILD files. Only main-repo paths can appear in this Git walk.
			[[ "$label" == @* ]] && continue
			if [[ "$label" != //*:* ]]; then
				UNSUPPORTED_QUERY_OUTPUT="true"
				break
			fi
			label_body="${label#//}"
			package="${label_body%%:*}"
			name="${label_body#*:}"
			if [[ -z "$name" ]]; then
				UNSUPPORTED_QUERY_OUTPUT="true"
				break
			fi
			if [[ -n "$package" ]]; then
				path="${package}/${name}"
			else
				path="$name"
			fi
			if ! git ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
				UNSUPPORTED_QUERY_OUTPUT="true"
				break
			fi
			add_input_path "$path"
			[[ -n "$package" ]] && add_guard_dir "$package"
			QUERY_PATH_COUNT=$((QUERY_PATH_COUNT + 1))
		done <<<"$QUERY_OUTPUT"

		if [[ "$UNSUPPORTED_QUERY_OUTPUT" == "true" ]] || [[ $QUERY_PATH_COUNT -eq 0 ]]; then
			echo >&2 "WARNING: Bazel release-input query returned an empty or unsupported closure for ${BAZEL_PACKAGE}; counting commits repo-wide."
			use_all_paths
		else
			# Bazel does not model every repository-level configuration file as a
			# source dependency. These files can change how every selected target is
			# analysed or built, so keep them as explicit release inputs.
			for build_config in MODULE.bazel MODULE.bazel.lock .bazelrc bazel/remote.bazelrc; do
				if git ls-files --error-unmatch -- "$build_config" >/dev/null 2>&1; then
					add_input_path "$build_config"
				fi
			done
		fi
	fi
	rm -f "$QUERY_ERROR"
else
	# Package-less callers retain the original chart-only behaviour. Production
	# always supplies chart.package, while this mode is useful for standalone use.
	add_input_path "$CHART_DIR"
fi

if [[ ${#INPUT_PATHS[@]} -eq 0 ]]; then
	echo >&2 "ERROR: no release input paths were selected"
	exit 1
fi

# Resolve commits in two stages. --full-history prevents Git's path-history
# simplification from hiding a relevant merge. Exact current inputs handle the
# normal case. A conservative D/R guard over their Bazel package directories
# catches a globbed input that was deleted or renamed out of the current
# closure. It may over-publish for a deleted non-input, but cannot silently skip
# content that disappeared from the built artifact.
if ! INPUT_COMMITS=$(git log --full-history --format=%H "${VERSION_COMMIT}..HEAD" -- "${INPUT_PATHS[@]}"); then
	echo >&2 "ERROR: failed to walk release-input history"
	exit 1
fi

DR_COMMITS=""
if [[ ${#RENAME_GUARD_DIRS[@]} -gt 0 ]]; then
	if ! DR_HISTORY=$(git log --full-history --format='commit %H' --name-status --find-renames \
		"${VERSION_COMMIT}..HEAD" -- "${RENAME_GUARD_DIRS[@]}"); then
		echo >&2 "ERROR: failed to inspect release-input deletions and renames"
		exit 1
	fi
	DR_COMMITS=$(awk '
		$1 == "commit" { commit = $2; next }
		$1 ~ /^D/ || $1 ~ /^R/ { print commit }
	' <<<"$DR_HISTORY" | sort -u)
fi

if ! ORDERED_COMMITS=$(git rev-list --reverse "${VERSION_COMMIT}..HEAD"); then
	echo >&2 "ERROR: failed to order release-input commits"
	exit 1
fi

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

while IFS= read -r commit; do
	[[ -z "$commit" ]] && continue
	if ! grep -Fqx "$commit" <<<"$INPUT_COMMITS" && ! grep -Fqx "$commit" <<<"$DR_COMMITS"; then
		continue
	fi
	AUTHOR=$(git show -s --format=%an "$commit")
	SUBJECT=$(git show -s --format=%s "$commit")

	# Skip automated commits
	case "$AUTHOR|||$SUBJECT" in
	*"ci-format-bot"* | *"chart-version-bot"*) continue ;;
	esac

	INDEX="$COMMIT_COUNT"
	COMMIT_COUNT=$((COMMIT_COUNT + 1))

	# Check for breaking change (! before colon)
	# Pre-1.0: breaking changes bump minor (semver allows breaking changes in 0.x)
	# Post-1.0: breaking changes bump major
	BREAKING_RE='^[a-z]+(\([^)]*\))?!:'
	if [[ "$SUBJECT" =~ $BREAKING_RE ]]; then
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
	TYPE=$(echo "$SUBJECT" | sed -E -n 's/^([a-z]+)(\([^)]*\))?:.*/\1/p')
	case "$TYPE" in
	feat)
		[[ "$MINOR_BOUNDARY" -lt 0 ]] && MINOR_BOUNDARY="$INDEX"
		[[ "$BUMP" != "major" ]] && BUMP="minor"
		;;
	fix | perf | refactor | style | docs | test | ci | build | chore | revert)
		[[ "$BUMP" == "none" ]] && BUMP="patch"
		;;
	esac
done <<<"$ORDERED_COMMITS"

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
