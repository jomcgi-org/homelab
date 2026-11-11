#!/usr/bin/env bash
# Update Python requirements lock files from pyproject.toml
# Note: requires Bazel — see bazel/ARCHITECTURE.md (Developer tools)
set -euo pipefail

# Refuse PEP 517 backend execution in automatic writer environments. Packages
# requiring a source build need separately isolated dependency maintenance.
echo "Updating Python requirements..."

# Regenerate runtime.txt from pyproject.toml
bazel run //bazel/requirements:runtime -- --no-build

# Regenerate all.txt (includes test dependencies)
bazel run //bazel/requirements:requirements.all -- --no-build

echo "✅ Python requirements updated"
