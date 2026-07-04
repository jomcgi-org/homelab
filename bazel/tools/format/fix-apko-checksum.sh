#!/usr/bin/env bash
# Recompute and patch the config.checksum field in an apko lock file after a
# config-only apko.yaml edit (e.g. adding a mount, tweaking annotations).
#
# This is the fast path and needs no bazel. If the PACKAGE LIST in apko.yaml
# changed, this script is not enough: re-run the full lock resolve instead
# (bazel/tools/format/update-apko-locks.sh, needs bazel).

set -euo pipefail

if [ $# -ne 1 ]; then
	echo "Usage: $0 <path/to/apko.yaml|apko-*.yaml>" >&2
	exit 1
fi

config="$1"

if [ ! -f "$config" ]; then
	echo "ERROR: config file not found: $config" >&2
	exit 1
fi

base="$(basename "$config")"
case "$base" in
apko.yaml | apko-*.yaml) ;;
*)
	echo "ERROR: expected an apko.yaml or apko-*.yaml file, got: $config" >&2
	exit 1
	;;
esac

lock="${config%.yaml}.lock.json"

if [ ! -f "$lock" ]; then
	echo "ERROR: sibling lock file not found: $lock" >&2
	exit 1
fi

python3 - "$config" "$lock" <<'PYEOF'
import base64
import hashlib
import json
import sys

config_path, lock_path = sys.argv[1], sys.argv[2]

digest = hashlib.sha256(open(config_path, "rb").read()).digest()
new_checksum = "sha256-" + base64.standard_b64encode(digest).decode()

with open(lock_path) as f:
    data = json.load(f)

old_checksum = data.get("config", {}).get("checksum")

if old_checksum == new_checksum:
    print(f"Checksum already up to date for {lock_path}")
    print(f"  checksum: {old_checksum}")
    sys.exit(0)

data["config"]["checksum"] = new_checksum
with open(lock_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

print(f"Patched {lock_path}")
print(f"  old checksum: {old_checksum}")
print(f"  new checksum: {new_checksum}")
PYEOF

echo ""
echo "Note: this only recomputes config.checksum. If apko.yaml's PACKAGE LIST"
echo "changed too (not just config like mounts/env/annotations), the lock still"
echo "needs a full re-resolve: bazel/tools/format/update-apko-locks.sh (needs bazel)."
