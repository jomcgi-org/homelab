#!/usr/bin/env bash
# The binary from the private image runs and has the pinned version.
set -euo pipefail
out="$("$1" --version)"
echo "$out"
grep -q '^mandoline 0\.25\.0 ' <<<"$out"
