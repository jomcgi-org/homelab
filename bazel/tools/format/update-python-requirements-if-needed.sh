#!/usr/bin/env bash
# Check runtime and aggregate locks. Manifest regeneration belongs to PR CI.
set -euo pipefail

if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]]; then
	echo 'Run this script directly, outside bazel run.' >&2
	exit 2
fi
if [[ "$(uname -s)" != Linux ]]; then
	echo 'Python lock and manifest regeneration is deferred to PR Linux CI.'
	exit 0
fi

status=0
bazel test //bazel/requirements:runtime_test //bazel/requirements:requirements.all_test || status=$?
case "$status" in
0)
	echo 'Python requirement locks are current'
	exit 0
	;;
3)
	./bazel/tools/format/update-python-requirements.sh
	;;
*) exit "$status" ;;
esac

# Stage only tracked generated outputs.
git add -u -- bazel/requirements/*.txt
echo "✓ Python requirements updated and staged"
