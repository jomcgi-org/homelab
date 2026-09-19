#!/usr/bin/env bash
# The committed data/trace.js remains the source for the public Firecracker
# story. Its original regeneration path depended on the retired private demos
# API and the removed SigNoz ClickHouse store, so there is no runnable capture
# workflow in this repository after #5163.
set -euo pipefail

echo "fc-story regeneration is unavailable: the private capture API and SigNoz span store were retired by #5163." >&2
echo "Keep the committed data/trace.js until a separately approved measurement source replaces that workflow." >&2
exit 1
