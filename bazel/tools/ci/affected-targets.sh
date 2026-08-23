#!/usr/bin/env bash
# Computes Bazel targets affected by committed and worktree changes.
#
# Usage: affected-targets.sh <base-ref> [<head-ref>] [-- <bazel-query-args>...]
#   base-ref  : git ref (e.g., origin/main)
#   head-ref  : git ref (default HEAD)
#   bazel-query-args : extra args passed to bazel query (after --)
#
# Outputs to stdout:
#   - A single line "//..." if fallback to full test suite needed
#   - Otherwise: one target label per line that should be tested
#   - Nothing if no files changed or all changed files are outside packages
#
# Outputs to stderr:
#   - One line prefixed "affected-targets:" explaining the decision
#
# Exit: 0 always (query failures fall back to //... with exit 0)
#
# Algorithm:
#   1. Collect all changed files (merge-base, working tree, staged, and untracked)
#   2. Fallback to //... if any file in the graph-mutation set changed:
#      - BUILD files: BUILD, BUILD.bazel
#      - Workspace files: WORKSPACE, WORKSPACE.bazel, MODULE.bazel, MODULE.bazel.lock
#      - Config files: .bazelrc, .bazelversion, buildbuddy.yaml
#      - Deps and locks: *.bzl, *.lock, *requirements.txt, pnpm-lock.yaml,
#        package.json, go.mod, go.sum, mix.lock, *.apko.yaml
#      - Tooling: anything under bazel/ directory
#      - Deleted files: any file removed from the tree
#   3. Otherwise map each changed file to a Bazel label: //package:relative/path
#      (root package uses //:relative/path)
#   4. Probe for label existence with one set() query; fallback if probe fails
#   5. Build a global test-only universe and find tests that transitively depend
#      on the labels. Exclude Semgrep tests from dependency traversal, then add
#      them back conservatively so platform-specific Semgrep engines are not
#      loaded by the query.
#   6. Filter out external repos (@) and output sorted results
#
# The fallback set covers changes that invalidate a reverse dependency walk over
# source files. Graph-shape mutations (new BUILD files, imports, deps changes,
# toolchain changes) make the forward walk unreliable, so the full test suite
# is needed as the gate. Deleted source files cannot be mapped to labels by
# static inspection (the file no longer exists to find a containing package).
#
# Integration with buildbuddy.yaml (pr-checks action):
#   - Queue candidates (gh-readonly-queue/* branches) run //... (full gate)
#   - Main pushes run //... (full snapshot seed for fast PR fallbacks)
#   - PR branches use this script to run only affected targets (fast feedback)
#   - Local ci test runs use this script inside one hosted Linux runner

set -euo pipefail

usage() {
	cat >&2 <<EOF
Usage: affected-targets.sh <base-ref> [<head-ref>] [-- <bazel-query-args>...]
  base-ref  : git ref (e.g., origin/main)
  head-ref  : git ref (default HEAD)
  bazel-query-args : extra args passed to bazel query (after --)
EOF
	exit 2
}

base_ref="${1:-}"
if [[ -z "$base_ref" ]]; then
	usage
fi

# Parse remaining args: head-ref and optional --
head_ref="HEAD"
bazel_args=()
shift

if [[ $# -gt 0 && "$1" != "--" ]]; then
	head_ref="$1"
	shift
fi

if [[ $# -gt 0 && "$1" == "--" ]]; then
	shift
	bazel_args=("$@")
fi

# Get changed files relative to the merge base
echo "affected-targets: diffing $base_ref...$head_ref" >&2
changed_files=()
while IFS= read -r file; do
	[[ -n "$file" ]] && changed_files+=("$file")
done < <(git diff --name-only --diff-filter=ACMRD "$base_ref...$head_ref" 2>/dev/null || true)

# Include uncommitted changes (working tree, staged, and untracked)
while IFS= read -r file; do
	[[ -n "$file" ]] && changed_files+=("$file")
done < <(git diff --name-only 2>/dev/null || true)

while IFS= read -r file; do
	[[ -n "$file" ]] && changed_files+=("$file")
done < <(git diff --cached --name-only 2>/dev/null || true)

while IFS= read -r file; do
	[[ -n "$file" ]] && changed_files+=("$file")
done < <(git ls-files --others --exclude-standard 2>/dev/null || true)

# Deduplicate (but only if non-empty to avoid mapfile adding an empty element)
if [[ ${#changed_files[@]} -gt 0 ]]; then
	mapfile -t changed_files < <(printf '%s\n' "${changed_files[@]}" | sort -u)
fi

# If no changes, nothing to test
if [[ ${#changed_files[@]} -eq 0 ]]; then
	echo "affected-targets: no changes, nothing to test" >&2
	exit 0
fi

# Check for deleted files in the diff (three-dot range, working tree, and cached)
deleted_files=()
while IFS= read -r file; do
	[[ -n "$file" ]] && deleted_files+=("$file")
done < <(git diff --name-only --diff-filter=D "$base_ref...$head_ref" 2>/dev/null || true)

while IFS= read -r file; do
	[[ -n "$file" ]] && deleted_files+=("$file")
done < <(git diff --name-only --diff-filter=D 2>/dev/null || true)

while IFS= read -r file; do
	[[ -n "$file" ]] && deleted_files+=("$file")
done < <(git diff --cached --name-only --diff-filter=D 2>/dev/null || true)

if [[ ${#deleted_files[@]} -gt 0 ]]; then
	echo "affected-targets: fallback to //... because deleted file: ${deleted_files[0]}" >&2
	echo "//..."
	exit 0
fi

# Check each file against fallback patterns (graph-shape mutations)
for file in "${changed_files[@]}"; do
	# Check basename
	basename_only="${file##*/}"
	case "$basename_only" in
	BUILD | BUILD.bazel | WORKSPACE | WORKSPACE.bazel | MODULE.bazel | MODULE.bazel.lock | .bazelrc | .bazelversion | buildbuddy.yaml | package.json | go.mod | go.sum | mix.lock | pnpm-lock.yaml)
		echo "affected-targets: fallback to //... because BUILD-shaped file changed: $file" >&2
		echo "//..."
		exit 0
		;;
	esac

	# Check suffixes and patterns
	case "$file" in
	bazel/*)
		echo "affected-targets: fallback to //... because file under bazel/ tooling: $file" >&2
		echo "//..."
		exit 0
		;;
	*.bzl)
		echo "affected-targets: fallback to //... because Starlark import changed: $file" >&2
		echo "//..."
		exit 0
		;;
	*requirements.txt)
		echo "affected-targets: fallback to //... because Python deps changed: $file" >&2
		echo "//..."
		exit 0
		;;
	*.lock)
		echo "affected-targets: fallback to //... because dependency lock changed: $file" >&2
		echo "//..."
		exit 0
		;;
	*.apko.yaml)
		echo "affected-targets: fallback to //... because apko image config changed: $file" >&2
		echo "//..."
		exit 0
		;;
	esac
done

# Map changed files to source labels: //package:relative/path or //:relative/path for root
echo "affected-targets: ${#changed_files[@]} changed file(s), mapping to labels" >&2
source_labels=()
outside_count=0

for file in "${changed_files[@]}"; do
	# Find the nearest enclosing package (directory with BUILD or BUILD.bazel)
	dir="${file%/*}"
	[[ "$dir" == "$file" ]] && dir="."

	package_root=""
	while [[ "$dir" != "" && "$dir" != "." ]]; do
		if [[ -f "$dir/BUILD" || -f "$dir/BUILD.bazel" ]]; then
			package_root="$dir"
			break
		fi
		if [[ "$dir" == */* ]]; then
			dir="${dir%/*}"
		else
			dir="."
		fi
	done

	# Check the repo root
	if [[ -z "$package_root" ]]; then
		if [[ -f "BUILD" || -f "BUILD.bazel" ]]; then
			package_root="."
		fi
	fi

	# If no package found, the file is outside any Bazel package
	if [[ -z "$package_root" ]]; then
		((outside_count++)) || true
		continue
	fi

	# Compute the label: //:relative/path for root, //package:relative/path otherwise
	if [[ "$package_root" == "." ]]; then
		relative_path="${file#./}"
		label="//:$relative_path"
	else
		relative_path="${file#$package_root/}"
		label="//${package_root}:$relative_path"
	fi

	source_labels+=("$label")
done

if [[ ${#source_labels[@]} -eq 0 ]]; then
	if [[ $outside_count -gt 0 ]]; then
		echo "affected-targets: changed files are outside every Bazel package; nothing to test ($outside_count files)" >&2
	else
		echo "affected-targets: no changes, nothing to test" >&2
	fi
	exit 0
fi

# Probe for label existence with ONE query: if any label doesn't exist in the graph,
# the probe still succeeds with exit 0 or 3, but only existing labels appear in output.
# stdout only: bazel's stderr (WARNING, Loading: ...) flows to OUR stderr
# unmodified, it must never be parsed as a label.
# Both queries run under a hard timeout. A bazel query that wedges (two
# docs-only PR runs on 2026-08-22 sat silent until BuildBuddy's 1h limit,
# with the server unresponsive afterwards) must cost minutes, not the hour,
# and must end in the full run rather than a green PR with no tests.
# After a timed-out query the server is usually the thing that is wedged, and
# the //... fallback would block on it for the rest of the hour. Ask it to
# stop, briefly; if that hangs too, kill the server JVM so the next command
# starts a fresh one.
recover_bazel() {
	timeout 60 "${BAZEL:-bazel}" shutdown >/dev/null 2>&1 || pkill -9 -f 'A-server.jar' >/dev/null 2>&1 || true
}

QUERY_TIMEOUT="${AFFECTED_TARGETS_QUERY_TIMEOUT:-600}"
echo "affected-targets: probing ${#source_labels[@]} label(s) (timeout ${QUERY_TIMEOUT}s)" >&2
probe_output=$(timeout "$QUERY_TIMEOUT" "${BAZEL:-bazel}" query "set($(printf '%s ' "${source_labels[@]}"))" --keep_going --output=label "${bazel_args[@]}") || probe_rc=$?
probe_rc=${probe_rc:-0}
if [[ $probe_rc -eq 124 ]]; then
	echo "affected-targets: fallback to //... because the label probe timed out after ${QUERY_TIMEOUT}s" >&2
	recover_bazel
	echo "//..."
	exit 0
fi

# If probe exits with non-0 and non-3, treat as query failure (fallback)
if [[ $probe_rc -ne 0 && $probe_rc -ne 3 ]]; then
	echo "affected-targets: fallback to //... because label existence probe failed (exit $probe_rc)" >&2
	echo "//..."
	exit 0
fi

# Extract which labels exist (grep only the labels we queried)
valid_labels=()
while IFS= read -r label; do
	[[ "$label" == //* ]] && valid_labels+=("$label")
done < <(printf '%s\n' "$probe_output")

# Check which labels were unreferenced (in input but not in output)
unreferenced=0
for label in "${source_labels[@]}"; do
	found=0
	for valid in "${valid_labels[@]}"; do
		if [[ "$valid" == "$label" ]]; then
			found=1
			break
		fi
	done
	if [[ $found -eq 0 ]]; then
		((unreferenced++)) || true
	fi
done

if [[ ${#valid_labels[@]} -eq 0 ]]; then
	echo "affected-targets: $((outside_count + unreferenced)) files not referenced by any Bazel rule; nothing to test" >&2
	exit 0
fi

# Query within the dependency closure of all tests instead of using //... as the
# rdeps universe. This avoids loading packages and targets that cannot affect a
# test. Semgrep tests are excluded from the traversal because their configured
# dependency closures load every platform engine, including repositories that
# cannot be resolved on the Linux runner. Add them back conservatively because
# semgrep_target_test can scan a transitive source closure that query cannot
# recover after those tests are excluded.
test_query="let all_tests = tests(//...) in let semgrep_tests = attr(name, \".*semgrep.*\", \$all_tests) union attr(generator_function, \"semgrep_.*\", \$all_tests) in \$semgrep_tests union tests(rdeps(deps(\$all_tests except \$semgrep_tests), set($(printf '%s ' "${valid_labels[@]}" | xargs))))"
echo "affected-targets: test-universe query over ${#valid_labels[@]} label(s) (timeout ${QUERY_TIMEOUT}s)" >&2
query_output=$(timeout "$QUERY_TIMEOUT" "${BAZEL:-bazel}" query "$test_query" --keep_going --output=label "${bazel_args[@]}") || query_rc=$?
query_rc=${query_rc:-0}
if [[ $query_rc -eq 124 ]]; then
	echo "affected-targets: fallback to //... because the test-universe query timed out after ${QUERY_TIMEOUT}s" >&2
	recover_bazel
	echo "//..."
	exit 0
fi

# Fail CLOSED. Anything but a clean exit, or a --keep_going partial result
# (exit 3) that still produced labels, falls back to //...: an empty list
# here would make the PR run skip every test and report green.
if [[ $query_rc -ne 0 ]] && { [[ $query_rc -ne 3 ]] || [[ -z "$query_output" ]]; }; then
	echo "affected-targets: fallback to //... because test-universe query failed (exit $query_rc)" >&2
	echo "//..."
	exit 0
fi

if [[ -n "$query_output" ]]; then
	# Keep main-repo labels only (drops @external repos and any stray line)
	filtered=$(printf '%s\n' "$query_output" | grep "^//" | grep -v '\.venv$' | sort -u || true)
	target_count=$(printf '%s\n' "$filtered" | grep -c . || true)

	if [[ $unreferenced -gt 0 ]]; then
		echo "affected-targets: $target_count affected tests ($unreferenced files unreferenced)" >&2
	else
		echo "affected-targets: $target_count affected tests" >&2
	fi

	printf '%s\n' "$filtered"
else
	echo "affected-targets: test-universe query returned no tests" >&2
fi

exit 0
