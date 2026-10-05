#!/usr/bin/env bash
# Tier 1: quick quality check for changes that move numerics or drafts (~30 min).
#
#   bench/quality.sh --model <model.oom> --refs <dir> --label <name> [--out <dir>] [--lock <file>]
#                    [--gsm8k N] [--ruler-per-length N] [-- <oominf flags, e.g. --dense fp8>]
#
# 1. `oominf score` at 32k and 95k against the exact references <refs>/ref2-<n>.bin, with
#    the prompts they were made from, <refs>/prompt-<n>.txt (n = 32768, 100000). References
#    bind token ids, so prompts are kept with them rather than regenerated.
#    Judge against the rounding floor (KL ~0.035-0.038, top-1 ~91.5-93%), not zero.
# 2. `oomeval` on a sample served with the same flags: GSM8K (greedy) and RULER essay
#    haystacks at 32k and 95k. Compare labels with `uv run python -m oomeval compare`.
set -uo pipefail

model=${OOMINF_MODEL:-} refs="" label="" out=evals/results lock="" gsm8k=50 ruler=2 extra=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) model=$2; shift 2 ;;
        --refs) refs=$2; shift 2 ;;
        --label) label=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        --lock) lock=$2; shift 2 ;;
        --gsm8k) gsm8k=$2; shift 2 ;;
        --ruler-per-length) ruler=$2; shift 2 ;;
        --) shift; extra=("$@"); break ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
[[ -n $model && -n $refs && -n $label ]] || { echo "need --model, --refs and --label" >&2; exit 2; }
cd "$(dirname "$0")/.."
root=$PWD
[[ $out = /* ]] || out=$root/$out
if [[ -n $lock ]]; then exec 9>"$lock"; flock 9; fi

bin=$root/target/release/oominf
tmp=$(mktemp -d)
trap 'kill $server 2>/dev/null; wait $server 2>/dev/null; rm -rf "$tmp"' EXIT
server=""

echo "=== score ($label: ${extra[*]:-defaults})"
for n in 32768 100000; do
    $bin score --model "$model" --prompt-file "$refs/prompt-$n.txt" --tail 1024 \
        --against "$refs/ref2-$n.bin" "${extra[@]}" 2>&1 | grep -E "^vs reference|rror" | sed "s/^/  $n: /"
done

echo "=== oomeval ($label)"
port=8099
$bin serve --model "$model" --port $port --max-context 100000 "${extra[@]}" >"$tmp/serve.log" 2>&1 &
server=$!
cd evals
uv run python -m oomeval run --url http://127.0.0.1:$port --label "$label" --out "$out" \
    --notes "$(git rev-parse --short HEAD) ${extra[*]}" \
    --tasks gsm8k,ruler --limit "$gsm8k" --temperature 0 --max-tokens 4096 \
    --ruler-tokenizer "$model/tokenizer.json" --ruler-lengths 32768,95000 \
    --ruler-per-length "$ruler" --progress-every 25 2>&1 | grep -v -i "warning"
