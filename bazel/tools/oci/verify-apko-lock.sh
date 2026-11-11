#!/usr/bin/env bash
# Verify an apko lock file is in sync with its config.
# Compares the SHA-256 checksum embedded in the lock file against the actual config.
# Usage: verify-apko-lock.sh <config.yaml> <config.lock.json>
set -euo pipefail

CONFIG="$1"
LOCK="$2"

# Compute SHA-256 of config in SRI format
COMPUTED="sha256-$(openssl dgst -sha256 -binary "$CONFIG" | openssl base64 -A)"

# Extract checksum from lock file
EXPECTED=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['config']['checksum'])" "$LOCK")

if [ "$COMPUTED" != "$EXPECTED" ]; then
	echo "ERROR: apko lock file is stale!"
	echo "  Config:   $CONFIG"
	echo "  Lock:     $LOCK"
	echo "  Expected: $EXPECTED"
	echo "  Got:      $COMPUTED"
	echo ""
	echo "Run ./bazel/tools/format/update-apko-locks.sh \"$CONFIG\" to update the lock file."
	exit 1
fi

echo "OK: apko lock file is in sync with config"
