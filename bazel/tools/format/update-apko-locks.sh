#!/usr/bin/env bash
# Update all apko lock files in the repository
# Note: requires Bazel — see docs/decisions/tooling/001-oci-tool-distribution.md

set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

echo "Updating apko lock files..."

# Find all apko config files (including architecture-specific ones, excluding lock files)
find . \( -name "apko.yaml" -o -name "apko-*.yaml" \) \
	-not -path "*/node_modules/*" \
	-not -path "*/.git/*" \
	-not -name "*.lock.json" | while read -r config; do

	echo "  Updating lock for: $config"

	# Generate lock file using rules_apko
	if ! bazel run @rules_apko//apko -- lock "$config" 2>&1 | grep -v "^INFO:"; then
		echo "  ⚠️  Warning: Failed to update lock for $config"
		echo "  (config-only apko.yaml edits can use bazel/tools/format/fix-apko-checksum.sh, no bazel needed)"
	fi
done

echo "✅ apko lock files updated"
