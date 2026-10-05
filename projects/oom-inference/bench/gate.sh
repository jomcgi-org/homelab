#!/usr/bin/env bash
# Tier 0: the correctness gates every change runs (docs/TESTING.md). Exact changes stop here.
#
#   bench/gate.sh --model <model.oom> --fixtures <fixtures dir> [--lock <file>] [-- <extra oominf flags>]
#
# Extra flags (e.g. --dense fp8) go to check-layer/check-model. Exits non-zero if any gate fails.
set -uo pipefail

model=${OOMINF_MODEL:-} fixtures=${OOMINF_FIXTURES:-} lock="" extra=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) model=$2; shift 2 ;;
        --fixtures) fixtures=$2; shift 2 ;;
        --lock) lock=$2; shift 2 ;;
        --) shift; extra=("$@"); break ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
[[ -n $model && -n $fixtures ]] || { echo "need --model and --fixtures" >&2; exit 2; }
cd "$(dirname "$0")/.."
if [[ -n $lock ]]; then exec 9>"$lock"; flock 9; fi

bin=./target/release/oominf
failed=()
run() {
    local name=$1; shift
    echo "=== $name"
    "$@" >"$log" 2>&1
    local rc=$?
    tail -4 "$log"
    [[ $rc -eq 0 ]] || failed+=("$name")
}
log=$(mktemp)
trap 'rm -f "$log"' EXIT

cargo build --release || exit 1
run "cpu tests" env CUDA_VISIBLE_DEVICES= cargo test --release --workspace
run "moe_tiled" cargo test --release -p oominf-cuda --test moe_tiled -- --ignored
run "gemv" cargo test --release -p oominf-cuda --test gemv -- --ignored
run "kv_cache" cargo test --release -p oominf-cuda --test kv_cache -- --ignored
for l in 000:0 001:1 003:3 003-long:3; do
    run "check-layer ${l%%:*}" $bin check-layer --model "$model" --fixtures "$fixtures/layer-${l%%:*}" --layer "${l##*:}" "${extra[@]}"
done
run "check-model" $bin check-model --model "$model" --fixtures "$fixtures/model" "${extra[@]}"
run "check-model fp32 kv" $bin check-model --model "$model" --fixtures "$fixtures/model" --kv-cache fp32 "${extra[@]}"
run "speculative" env OOMINF_MODEL="$model" cargo test --release -p oominf-models-qwen --test speculative -- --ignored
run "snapshot" env OOMINF_MODEL="$model" cargo test --release -p oominf-models-qwen --test snapshot -- --ignored
run "checkpoint" env OOMINF_MODEL="$model" cargo test --release -p oominf-models-qwen --test checkpoint -- --ignored

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "GATES FAILED: ${failed[*]}"
    exit 1
fi
echo "ALL GATES PASSED"
