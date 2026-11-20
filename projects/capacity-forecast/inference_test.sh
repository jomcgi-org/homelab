#!/usr/bin/env bash
set -euo pipefail

worker="$1"
archive="$2"
tar -xf "$archive" -C "$TEST_TMPDIR"
export TIMESTRAL_MODEL_DIR="$TEST_TMPDIR/model"
export HF_HUB_OFFLINE=1

# A full regularly sampled context ending at Unix time 604800.
awk 'BEGIN {
  printf "{\"data\":{\"result\":[{\"values\":["
  for (t = 0; t <= 604800; t += 300) {
    printf "%s[%d,\"%d\"]", (t == 0 ? "" : ","), t, 600000000 + (t % 7200) * 1000
  }
  print "]}]}}"
}' | "$worker" --replay >"$TEST_TMPDIR/metrics"

for policy in baseline timestral; do
	for horizon in 300 600 900 1200 1500 1800; do
		labels="namespace=\"factory-r-homelab-review\",workload=\"homelab-reviewer\",policy=\"$policy\",horizon_seconds=\"$horizon\""
		grep -Eq "^capacity_forecast_memory_budget_bytes\\{$labels\\} [1-9][0-9.e+]*$" "$TEST_TMPDIR/metrics"
		grep -Fxq "capacity_forecast_target_timestamp_seconds{$labels} $((604800 + horizon))" "$TEST_TMPDIR/metrics"
	done
done
