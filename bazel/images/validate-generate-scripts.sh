#!/usr/bin/env bash
# CI validation: cross-check grep-based generate scripts against bazel query output.
# This ensures the grep approximations haven't drifted from the actual Bazel build graph.
#
# Only intended to run in CI where Bazel is available.
set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

FAILED=0
TMPDIR_VALIDATE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_VALIDATE"' EXIT

# --- Helper functions ---

extract_targets_from_build() {
	# Extract "//..." target labels from a generated BUILD file.
	# `sort -u`: the chart targets are deliberately listed twice, once in
	# push_all and once in the push_charts subset PR CI runs, and this list is
	# diffed against a bazel query that yields each label once.
	local build_file="$1"
	grep -o '"//[^"]*"' "$build_file" | tr -d '"' | grep -v '//visibility:\|//:__subpackages__\|//:__pkg__' | LC_ALL=C sort -u
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
	# `kind()` takes a REGEX and these are deliberately left unanchored, because
	# rules_oci wraps `oci_push` in a same-named macro and the underlying rule
	# kind is not verifiable outside CI; anchoring on a guess would silently
	# empty the query and pass this check against nothing.
	#
	# The cost of that is a substring trap: an `oci_push_manifest` rule added
	# anywhere in the repo IS matched here, and then reported as missing from
	# bazel/images/BUILD, which the grep generator has no reason to list it in.
	# That is why //bazel/tools/oci's manifest rule is called
	# `oci_digest_manifest`. Do not name a new rule after one of these three.
	bazel_query_retry 'kind("oci_push", //...)'
	bazel_query_retry 'kind("apko_push", //...)'
	bazel_query_retry 'kind("helm_push", //...)'
} | filter_labels | LC_ALL=C sort >"$TMPDIR_VALIDATE/push_all_query.txt"

compare_targets "push-all" "$TMPDIR_VALIDATE/push_all_grep.txt" "$TMPDIR_VALIDATE/push_all_query.txt"

# --- Validation 2: generate-home-cluster.sh ---

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

	if grep -Eq '^\s*- ../../projects/(home-cluster|gke-cluster|platform-gke)(/|$)' projects/home-cluster/kustomization.yaml; then
		echo "  generate-home-cluster exclusions: FAIL"
		echo "    Generated kustomization.yaml contains a cluster root or cluster-specific platform path"
		FAILED=1
	else
		echo "  generate-home-cluster exclusions: PASS"
	fi
fi

# NOTE: the two doc-index manifests (repo_docs_manifest.ndjson and the public
# docs-site docs-manifest.json) used to be validated here and hard-fail on drift.
# They now regenerate in the "Format check" action (buildbuddy.yaml) BEFORE its
# auto-commit, so a doc edit auto-commits the refreshed manifest like any other
# formatting fix instead of failing CI and forcing a manual regen. Keeping them
# out of this validator avoids a redundant second check. This validator stays for
# the BUILD-graph generate scripts above, whose drift needs human attention (a
# changed build graph), not a mechanical regen.

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
