#!/usr/bin/env bash
# Update Python requirements lock files from pyproject.toml
# Run directly on Linux, outside Bazel. See bazel/ARCHITECTURE.md.
set -euo pipefail

if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]] || [[ "$(uname -s)" != Linux ]]; then
	echo 'Run this script directly on a supported Linux runner, outside bazel run.' >&2
	exit 2
fi

echo "Updating Python requirements..."

# Regenerate runtime.txt from pyproject.toml
bazel run //bazel/requirements:runtime

# Regenerate all.txt (includes test/tools dependencies)
bazel run //bazel/requirements:requirements.all

./bazel/tools/ci/python-manifest.sh update

echo "✅ Python requirements updated"
