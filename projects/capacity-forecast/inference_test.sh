#!/usr/bin/env bash
set -euo pipefail

layout="$1"
jq_bin="$2"
root="$TEST_TMPDIR/rootfs"
mkdir "$root"
manifest=$("$jq_bin" -r '.manifests[0].digest | split(":")[1]' "$layout/index.json")
manifest="$layout/blobs/sha256/$manifest"
while IFS= read -r layer; do
	tar -xf "$layout/blobs/sha256/$layer" -C "$root"
done < <("$jq_bin" -r '.layers[].digest | split(":")[1]' "$manifest")

config=$("$jq_bin" -r '.config.digest | split(":")[1]' "$manifest")
config="$layout/blobs/sha256/$config"
mapfile -t image_env < <("$jq_bin" -r '.config.Env[]' "$config")
mapfile -t entrypoint < <("$jq_bin" -r '.config.Entrypoint[]' "$config")
touch "$root/dev/null" "$root/dev/urandom"

# A full regularly sampled context ending at Unix time 604800.
awk 'BEGIN {
  printf "{\"data\":{\"result\":[{\"values\":["
  for (t = 0; t <= 604800; t += 300) {
    printf "%s[%d,\"%d\"]", (t == 0 ? "" : ","), t, 600000000 + (t % 7200) * 1000
  }
  printf "]}]},\"past_covariates\":[["
  for (t = 0; t <= 604800; t += 300) {
    printf "%s[%d,\"%.2f\"]", (t == 0 ? "" : ","), t, (t % 7200) / 7200
  }
  printf "],["
  for (t = 0; t <= 604800; t += 300) {
    printf "%s[%d,\"%d\"]", (t == 0 ? "" : ","), t, (t % 3600) / 300
  }
  print "]]}"
}' >"$TEST_TMPDIR/history.json"

replay() {
	unshare -Urmpf bash -euc '
	root=$1
	shift
	mount --bind /dev/null "$root/dev/null"
	mount --bind /dev/urandom "$root/dev/urandom"
	mount -t proc proc "$root/proc"
	exec /usr/sbin/chroot "$root" /usr/bin/env -i "$@"
' bash "$root" "${image_env[@]}" "${entrypoint[@]}" --replay "$@"
}

replay <"$TEST_TMPDIR/history.json" >"$TEST_TMPDIR/metrics"

for policy in baseline timestral; do
	for horizon in 300 600 900 1200 1500 1800; do
		labels="namespace=\"factory-r-homelab-review\",workload=\"homelab-reviewer\",policy=\"$policy\",horizon_seconds=\"$horizon\""
		grep -Eq "^capacity_forecast_memory_budget_bytes\\{$labels\\} [1-9][0-9.e+]*$" "$TEST_TMPDIR/metrics"
		grep -Fxq "capacity_forecast_target_timestamp_seconds{$labels} $((604800 + horizon))" "$TEST_TMPDIR/metrics"
	done
done

replay 605100 <"$TEST_TMPDIR/history.json" >"$TEST_TMPDIR/after-first-target"
if grep -q 'horizon_seconds="300"' "$TEST_TMPDIR/after-first-target"; then
	echo "Forecasts at their target time must expire" >&2
	exit 1
fi
grep -q 'horizon_seconds="600"' "$TEST_TMPDIR/after-first-target"

"$jq_bin" '.data.result[0].values |= .[-6:] | .past_covariates[] |= .[-6:] | .min_samples = 6' \
	"$TEST_TMPDIR/history.json" | replay >"$TEST_TMPDIR/cold-start"
test "$(grep -c '^capacity_forecast_memory_budget_bytes{.*policy="timestral"' "$TEST_TMPDIR/cold-start")" -eq 6
