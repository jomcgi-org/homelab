#!/usr/bin/env bash
set -euo pipefail

[[ -x "$1" ]]
[[ "$("$1")" == goose ]]
tar -xzf "$2" -p -C "$TEST_TMPDIR" ./usr/local/bin/goose
[[ "$(stat -c %a "$TEST_TMPDIR/usr/local/bin/goose")" == 755 ]]
cmp "$1" "$TEST_TMPDIR/usr/local/bin/goose"
