#!/usr/bin/env bash
# Only update Python requirements if they're out of sync with pyproject.toml
set -euo pipefail

# Quick check: are requirements in sync?
if bazel test //bazel/requirements:runtime_test 2>/dev/null; then
	echo "✓ Python requirements in sync"
	exit 0
fi

echo "Python requirements out of sync, regenerating..."
"$(dirname "$0")/update-python-requirements.sh"

# Stage the updated lock files
git add bazel/requirements/*.txt bazel/tools/python/gazelle_python.yaml
echo "✓ Python requirements updated and staged"
