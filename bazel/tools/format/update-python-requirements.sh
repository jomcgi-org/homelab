#!/usr/bin/env bash
# Update Python requirements lock files from pyproject.toml
# Run directly on Linux, outside Bazel. See bazel/ARCHITECTURE.md.
set -euo pipefail

# Refuse PEP 517 backend execution in automatic writer environments. Packages
# requiring a source build need separately isolated dependency maintenance.
if [[ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]] || [[ "$(uname -s)" != Linux ]]; then
	echo 'Run this script directly on a supported Linux runner, outside bazel run.' >&2
	exit 2
fi

echo "Updating Python requirements..."

# Regenerate runtime.txt from pyproject.toml
bazel run //bazel/requirements:runtime -- --no-build

# Regenerate all.txt (includes test dependencies)
bazel run //bazel/requirements:requirements.all -- --no-build

# Renovate runs this lock-only script without BuildBuddy credentials. The outer
# PR Linux format stage refreshes and validates the manifest before Gazelle.
echo 'Python manifest regeneration is deferred to PR Linux CI.'

echo "✅ Python requirements updated"
