#!/usr/bin/env bash
# Update all apko lock files in the repository
# Note: requires Bazel — see docs/decisions/tooling/001-oci-tool-distribution.md

set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

# rules_apko picks its apko binary off the EXECUTION platform, and this
# repository registers an ARM64 execution platform for OCaml. Left to resolve on
# its own that one wins and apko comes out as linux_arm64: the wrong CPU on an
# amd64 Linux box, and on a Mac a binary that cannot run at all, failing with
# "cannot execute binary file: Exec format error". The
# renovate-apko-lock-maintenance CronWorkflow already forces linux_x86_64 for
# exactly this reason; do the same here, matched to the host.
case "$(uname -s)" in
Darwin) exec_platform="//bazel/tools/platforms:darwin_aarch64" ;;
*) exec_platform="//bazel/tools/platforms:linux_x86_64" ;;
esac

echo "Updating apko lock files (execution platform: ${exec_platform})..."

failed_count=0
failed_list=""
out="$(mktemp)"
trap 'rm -f "$out"' EXIT

# Read from process substitution rather than `find | while`: a pipeline runs the
# loop body in a subshell, so any failure it recorded would be discarded at the
# end of the pipe.
while IFS= read -r config; do
	# apko records the config path into the lock's `name` field verbatim, so a
	# leading "./" from `find .` would rewrite that field in every lock, and the
	# renovate-apko-lock-maintenance CronWorkflow (which finds without the dot)
	# would flip them all back on its next run. Strip it so both agree.
	config="${config#./}"

	echo "  Updating lock for: $config"

	# Do NOT pipe bazel straight into a filter. A pipeline's exit status is the
	# LAST command's, so a failing bazel that printed anything still tested as
	# success, and this script reported every lock as updated while regenerating
	# none of them (#4854).
	if bazel run --extra_execution_platforms="$exec_platform" \
		@rules_apko//apko -- lock "$config" >"$out" 2>&1; then
		grep -v "^INFO:" "$out" || true
	else
		cat "$out"
		echo "  Warning: Failed to update lock for $config"
		echo "  (config-only apko.yaml edits can use bazel/tools/format/fix-apko-checksum.sh, no bazel needed)"
		failed_count=$((failed_count + 1))
		failed_list="${failed_list}  ${config}
"
	fi
done < <(
	# Find all apko config files (including architecture-specific ones, excluding lock files)
	find . \( -name "apko.yaml" -o -name "apko-*.yaml" \) \
		-not -path "*/node_modules/*" \
		-not -path "*/.git/*" \
		-not -name "*.lock.json"
)

if [ "$failed_count" -ne 0 ]; then
	echo "apko lock update FAILED for ${failed_count} config(s):" >&2
	printf '%s' "$failed_list" >&2
	exit 1
fi

echo "✅ apko lock files updated"
