#!/usr/bin/env bash
# Outer Linux orchestration only. Never call from a Bazel action/formatter.
set -euo pipefail

if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
	echo 'python-manifest: invoke directly, outside bazel run' >&2
	exit 2
fi
if [[ "$(uname -s)" != Linux ]]; then
	echo 'python-manifest: use PR Linux CI or a bb remote Linux script' >&2
	exit 2
fi

manifest=//bazel/tools/python:gazelle_python_manifest
case "${1:-}" in
update)
	status=0
	bazel test "$manifest.test" --config=ci || status=$?
	case "$status" in
	0) echo 'python-manifest: integrity is current' ;;
	3)
		# Bazel exit 3 means a test failed. Analysis/infrastructure failures
		# must stop this driver rather than triggering a repair.
		bazel run "$manifest.update" --config=ci
		bazel test "$manifest.test" //bazel/tools/python:manifest_generated_test --config=ci --nocache_test_results
		;;
	*) exit "$status" ;;
	esac
	;;
check)
	# Explicit labels, no deleted_packages or tag filters, even on bot heads
	# and empty affected sets. Force execution for current-head evidence.
	bazel test "$manifest.test" //bazel/tools/python:manifest_drift_test --config=ci --nocache_test_results --test_output=all
	# Full wheel mapping is intentionally scoped to changes in its inputs.
	# Compare the working tree too, so local ci test covers uncommitted edits.
	base_ref="${2:-origin/main}"
	git rev-parse --verify "$base_ref" >/dev/null
	if ! git diff --quiet "$base_ref" -- MODULE.bazel MODULE.bazel.lock bazel/requirements/all.txt bazel/tools/python; then
		bazel test //bazel/tools/python:manifest_generated_test --config=ci --nocache_test_results --test_output=all
	fi
	;;
*)
	echo 'Usage: python-manifest.sh update | check [base-ref]' >&2
	exit 2
	;;
esac
