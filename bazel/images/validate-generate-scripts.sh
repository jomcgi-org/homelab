#!/usr/bin/env bash
# CI validation: cross-check grep-based generate scripts against bazel query output.
# This ensures the grep approximations haven't drifted from the actual Bazel build graph.
#
# Modes (the Format check now runs in the bazel-free ci/format image, so the
# bazel-query validations are split out to the Test action which has bazel):
#   --all          (default) run everything; needs bazel. For local use.
#   --queries-only run only the bazel-query drift checks (push-all,
#                  push-all-pages). Runs in the Test action.
#   --no-queries   run only the bazel-free checks (home-cluster, repo-docs
#                  manifest, docs-site manifest). Runs in the Format check.
set -euo pipefail

MODE="all"
case "${1:-}" in
--queries-only) MODE="queries" ;;
--no-queries) MODE="no-queries" ;;
--all | "") MODE="all" ;;
*)
	echo "usage: $0 [--all|--queries-only|--no-queries]" >&2
	exit 2
	;;
esac

# run_queries: the bazel-query drift checks (need bazel).
run_queries() { [ "$MODE" = "all" ] || [ "$MODE" = "queries" ]; }
# run_nonqueries: the bazel-free checks (python3/bash only).
run_nonqueries() { [ "$MODE" = "all" ] || [ "$MODE" = "no-queries" ]; }

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

FAILED=0
TMPDIR_VALIDATE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_VALIDATE"' EXIT

# --- Helper functions ---

extract_targets_from_build() {
	# Extract "//..." target labels from a generated BUILD file
	local build_file="$1"
	grep -o '"//[^"]*"' "$build_file" | tr -d '"' | grep -v '//visibility:\|//:__subpackages__\|//:__pkg__' | LC_ALL=C sort
}

compare_targets() {
	local name="$1"
	local grep_file="$2"
	local query_file="$3"

	if diff -u "$query_file" "$grep_file" >"$TMPDIR_VALIDATE/diff_${name}" 2>&1; then
		echo "  ${name}: PASS"
	else
		echo "  ${name}: FAIL"
		echo "    The grep-based generate script output differs from bazel query."
		echo "    Diff (- = bazel query / expected, + = grep script / actual):"
		sed 's/^/    /' "$TMPDIR_VALIDATE/diff_${name}"
		echo ""
		FAILED=1
	fi
}

# Retry `bazel query` on transient external/infrastructure exit codes.
# Bazel exit codes: 32=REMOTE_ENVIRONMENTAL_ERROR, 34=REMOTE_ERROR,
# 36=LOCAL_ENVIRONMENTAL_ERROR, 37=INTERNAL_ERROR, 38=EXTERNAL_DEPS_ERROR,
# 39=REMOTE_CACHE_EVICTED. CI uses an ephemeral repo cache, so loading-phase
# queries (kind(..., //...)) periodically hit a transient fetch failure that
# clears on retry. All other exit codes are surfaced immediately.
bazel_query_retry() {
	local query="$1"
	local attempt=1
	local max=3
	local delay=4
	local rc=0
	local stderr
	while [ "$attempt" -le "$max" ]; do
		stderr=$(mktemp)
		if bazel query "$query" 2>"$stderr"; then
			rm -f "$stderr"
			return 0
		fi
		rc=$?
		case "$rc" in
		32 | 34 | 36 | 37 | 38 | 39)
			echo "  bazel query (transient rc=$rc, attempt $attempt/$max): $query" >&2
			sed 's/^/    /' "$stderr" >&2
			rm -f "$stderr"
			if [ "$attempt" -lt "$max" ]; then
				sleep "$delay"
				delay=$((delay * 2))
			fi
			attempt=$((attempt + 1))
			;;
		*)
			cat "$stderr" >&2
			rm -f "$stderr"
			return "$rc"
			;;
		esac
	done
	echo "  bazel query failed after $max attempts: $query" >&2
	return "$rc"
}

# Keep only Bazel target labels from a query capture, dropping any bb-sidecar log
# noise that lands on stdout when the BES endpoint is flaky. Those WRN/INF
# "connection reset by peer" lines would otherwise pollute the query output and
# break the diff against the grep-extracted labels, even though the build graph
# (and the real target list) is unchanged. `|| true` so an empty match is not a
# pipefail; genuine drift is still caught by the diff.
filter_labels() {
	grep -E '^(@[^/]*)?//' || true
}

# --- Validation 1: generate-push-all.sh ---

echo "Validating generate-push-all.sh ..."

# Run the grep-based script (it writes bazel/images/BUILD)
bash bazel/images/generate-push-all.sh

# Extract targets from the generated BUILD file
extract_targets_from_build bazel/images/BUILD >"$TMPDIR_VALIDATE/push_all_grep.txt"

# Run equivalent bazel queries
{
	bazel_query_retry 'kind("oci_push", //...)'
	bazel_query_retry 'kind("apko_push", //...)'
	bazel_query_retry 'kind("helm_push", //...)'
} | filter_labels | LC_ALL=C sort >"$TMPDIR_VALIDATE/push_all_query.txt"

compare_targets "push-all" "$TMPDIR_VALIDATE/push_all_grep.txt" "$TMPDIR_VALIDATE/push_all_query.txt"

# --- Validation 2: generate-push-all-pages.sh ---

echo "Validating generate-push-all-pages.sh ..."

# Run the grep-based script (it writes projects/websites/BUILD)
bash bazel/images/generate-push-all-pages.sh

# Extract targets from the generated BUILD file
extract_targets_from_build projects/websites/BUILD >"$TMPDIR_VALIDATE/push_pages_grep.txt"

# Run equivalent bazel query
bazel_query_retry 'kind("wrangler_pages_push", //...)' |
	filter_labels | LC_ALL=C sort >"$TMPDIR_VALIDATE/push_pages_query.txt"

compare_targets "push-all-pages" "$TMPDIR_VALIDATE/push_pages_grep.txt" "$TMPDIR_VALIDATE/push_pages_query.txt"

# --- Validation 3: generate-home-cluster.sh ---

echo "Validating generate-home-cluster.sh ..."

# Run the script and verify it produces non-empty output
bash bazel/images/generate-home-cluster.sh

if [ ! -s projects/home-cluster/kustomization.yaml ]; then
	echo "  generate-home-cluster: FAIL"
	echo "    Script produced empty or missing projects/home-cluster/kustomization.yaml"
	FAILED=1
else
	# Verify the output contains at least one resource path
	if grep -q '^\s*- ../../projects/' projects/home-cluster/kustomization.yaml; then
		echo "  generate-home-cluster: PASS"
	else
		echo "  generate-home-cluster: FAIL"
		echo "    Generated kustomization.yaml contains no resource paths"
		FAILED=1
	fi
fi

# --- Validation 4: repo-docs knowledge-graph manifest ---
#
# projects/monolith/knowledge/repo_docs_manifest.ndjson is a committed snapshot of
# the repo's markdown (docs/, project READMEs, CLAUDE.md files), baked into the
# monolith image and ingested into the knowledge graph for public-chat grounding.
# It is produced by a py_venv_binary (hermetic python, no system python3 needed),
# not the format multirun, so regenerate it here and fail if it drifts from what is
# committed. This stops a doc change from silently going unindexed.

echo "Validating repo-docs manifest ..."

REPO_DOCS_MANIFEST=projects/monolith/knowledge/repo_docs_manifest.ndjson
# Run the generator directly with python3 (it is pure stdlib + git ls-files), not
# `bazel run`: the py_venv_binary launcher does not resolve its main module in the
# CI runner. git-driven discovery keeps the output identical to a local run.
if python3 projects/monolith/knowledge/tools/gen_repo_docs_manifest.py >/dev/null 2>"$TMPDIR_VALIDATE/gen_repo_docs.err"; then
	if git diff --quiet -- "$REPO_DOCS_MANIFEST"; then
		echo "  repo-docs-manifest: PASS"
	else
		echo "  repo-docs-manifest: FAIL"
		echo "    $REPO_DOCS_MANIFEST is out of date with the repo's markdown."
		# Diagnostic: which doc PATHS the regen added/removed vs what is committed.
		# Each manifest line is one sort_keys JSON object: {"content":..,"path":..,..}
		_extract_paths() { sed 's/.*, "path": "\([^"]*\)", "sha256":.*/\1/'; }
		git show "HEAD:$REPO_DOCS_MANIFEST" | _extract_paths | LC_ALL=C sort >"$TMPDIR_VALIDATE/rd_committed.txt"
		_extract_paths <"$REPO_DOCS_MANIFEST" | LC_ALL=C sort >"$TMPDIR_VALIDATE/rd_regen.txt"
		echo "    committed_lines=$(wc -l <"$TMPDIR_VALIDATE/rd_committed.txt") regen_lines=$(wc -l <"$TMPDIR_VALIDATE/rd_regen.txt")"
		echo "    paths only in regen (+) / only in committed (-):"
		comm -3 "$TMPDIR_VALIDATE/rd_committed.txt" "$TMPDIR_VALIDATE/rd_regen.txt" | sed 's/^\t/      +/; s/^\([^ +]\)/      -\1/' | head -40
		echo "    Regenerate it and commit the result:"
		echo "      bazel run //projects/monolith:gen_repo_docs_manifest"
		echo "      git add $REPO_DOCS_MANIFEST && git commit"
		FAILED=1
		# Restore the committed version so later format steps see a clean tree.
		git checkout -- "$REPO_DOCS_MANIFEST" 2>/dev/null || true
	fi
else
	echo "  repo-docs-manifest: FAIL (generator did not run)"
	sed 's/^/    /' "$TMPDIR_VALIDATE/gen_repo_docs.err"
	FAILED=1
fi

# --- Validation 5: public docs-site manifest ---
#
# projects/monolith/frontend/src/lib/public/docs/docs-manifest.json is a committed
# snapshot of the public-allowlisted repo docs (top-level docs/*.md + the
# docs/decisions/** ADR tree), baked into the monolith-public frontend image and
# rendered server-side by the SvelteKit /docs route. Regenerate it here and fail
# if it drifts from what is committed, so a doc change does not silently go
# unpublished. Same git-driven, stdlib-only generator pattern as the repo-docs
# manifest above.

echo "Validating docs-site manifest ..."

DOCS_MANIFEST=projects/monolith/frontend/src/lib/public/docs/docs-manifest.json
if python3 projects/monolith/knowledge/tools/gen_docs_manifest.py >/dev/null 2>"$TMPDIR_VALIDATE/gen_docs.err"; then
	if git diff --quiet -- "$DOCS_MANIFEST"; then
		echo "  docs-site-manifest: PASS"
	else
		echo "  docs-site-manifest: FAIL"
		echo "    $DOCS_MANIFEST is out of date with the public docs allowlist."
		echo "    Regenerate it and commit the result:"
		echo "      bazel run //projects/monolith:gen_docs_manifest"
		echo "      git add $DOCS_MANIFEST && git commit"
		FAILED=1
		# Restore the committed version so later format steps see a clean tree.
		git checkout -- "$DOCS_MANIFEST" 2>/dev/null || true
	fi
else
	echo "  docs-site-manifest: FAIL (generator did not run)"
	sed 's/^/    /' "$TMPDIR_VALIDATE/gen_docs.err"
	FAILED=1
fi

# --- Summary ---

echo ""
if [ "$FAILED" -ne 0 ]; then
	echo "VALIDATION FAILED"
	echo ""
	echo "One or more generate scripts produced output that differs from bazel query."
	echo "This means the grep-based heuristics have drifted from the actual build graph."
	echo ""
	echo "To fix:"
	echo "  1. Check which BUILD files changed (new targets added/removed/renamed)"
	echo "  2. Update the corresponding generate script in bazel/images/ to match"
	echo "  3. Re-run 'format' to regenerate the BUILD files"
	echo "  4. Verify locally with: bash bazel/images/validate-generate-scripts.sh"
	exit 1
fi

echo "ALL VALIDATIONS PASSED"
