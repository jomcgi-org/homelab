#!/usr/bin/env bash
# Check every layer, including tools/test inputs and direct all.txt edits.
set -euo pipefail

if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]] || [[ "$(uname -s)" != Linux ]]; then
	echo 'Run this script directly on a supported Linux runner, outside bazel run.' >&2
	exit 2
fi

status=0
bazel test //bazel/requirements:runtime_test //bazel/requirements:requirements.all_test || status=$?
case "$status" in
0)
	./bazel/tools/ci/python-manifest.sh update
	;;
3)
	./bazel/tools/format/update-python-requirements.sh
	;;
*) exit "$status" ;;
esac

# Stage only tracked generated outputs.
git add -u -- bazel/requirements/*.txt bazel/tools/python/gazelle_python.yaml
echo "✓ Python requirements updated and staged"
