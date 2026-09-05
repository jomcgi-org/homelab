#!/usr/bin/env bash
# Update Python requirements lock files from pyproject.toml
# Note: requires Bazel — see bazel/ARCHITECTURE.md (Developer tools)
set -euo pipefail

echo "Updating Python requirements..."

# Regenerate runtime.txt from pyproject.toml
bazel run //bazel/requirements:runtime

# Regenerate all.txt (includes test/tools dependencies)
bazel run //bazel/requirements:requirements.all

echo "✅ Python requirements updated"
