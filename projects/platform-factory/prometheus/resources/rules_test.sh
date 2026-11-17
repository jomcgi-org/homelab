#!/usr/bin/env bash
# Run promtool's rule unit tests: rules_test.sh <promtool> <rules_test.yml>.
set -euo pipefail
"$1" check rules "$(dirname "$2")/rules.yml"
"$1" test rules "$2"
