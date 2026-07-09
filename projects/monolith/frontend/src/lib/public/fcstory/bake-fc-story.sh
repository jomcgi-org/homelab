#!/usr/bin/env bash
# Bake real fc-invoke trace data for the public /app/firecracker explainer.
#
# Restores: triggers real Python-sandbox runs through the private demos API
# and captures each run's span tree via the demo trace endpoint.
#
# Cold: the daemon only cold-boots once per workload at startup (see the
# Task 1 addendum in docs/plans/2026-07-08-firecracker-public-explainer.md),
# rooted in a `base_snapshot_build` span that the demo trace endpoint cannot
# see (it only resolves demo-rooted traces). That one run is fetched straight
# from SigNoz's ClickHouse backing store instead.
#
# Re-run and commit data/ whenever the daemon improves.
#
# Usage: bake-fc-story.sh [runs]   (default 12)
# Requires: kubectl (cluster access, incl. exec into the signoz clickhouse
# pod), python3, curl, jq.
set -euo pipefail

RUNS="${1:-12}"
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/data"
TMP="$(mktemp -d)"
PF_PID=""
cleanup() {
	rm -rf "$TMP"
	if [[ -n "$PF_PID" ]]; then
		kill "$PF_PID" 2>/dev/null || true
		wait "$PF_PID" 2>/dev/null || true
	fi
}
trap cleanup EXIT
mkdir -p "$OUT"

echo "> port-forwarding monolith backend"
kubectl port-forward -n monolith svc/monolith 18000:8000 >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
	curl -fsS -o /dev/null http://127.0.0.1:18000/healthz && break
	sleep 1
done

echo "> triggering $RUNS sandbox runs"
: >"$TMP/trace_ids"
for i in $(seq 1 "$RUNS"); do
	tid="$(curl -fsS -X POST http://127.0.0.1:18000/api/demos/firecracker/python \
		-H 'content-type: application/json' \
		-d '{"code": "print(\"baked for /app/firecracker\")"}' | jq -r .trace_id)"
	echo "  run $i: $tid"
	echo "$tid" >>"$TMP/trace_ids"
	sleep 2
done

echo "> waiting for SigNoz ingest, then fetching span trees"
: >"$TMP/spans.ndjson"
while read -r tid; do
	# Spans lag emission by ~5-10s (same as the demos frontend's own poll).
	# Retry until the span count stops growing between polls, or give up
	# after a timeout and record whatever's there (the normalizer below
	# will just classify it honestly, e.g. drop it if it's empty).
	prev_count=-1
	body=""
	for _ in $(seq 1 20); do
		body="$(curl -fsS "http://127.0.0.1:18000/api/demos/firecracker/trace/$tid")"
		count="$(echo "$body" | jq '.spans | length')"
		if [[ "$count" -gt 0 && "$count" == "$prev_count" ]]; then
			break
		fi
		prev_count="$count"
		sleep 2
	done
	echo "$body" | jq -c --arg tid "$tid" '{trace_id: $tid, body: .}' >>"$TMP/spans.ndjson"
done <"$TMP/trace_ids"

echo "> fetching the base_snapshot_build (cold) trace from ClickHouse"
CH_POD="chi-signoz-clickhouse-cluster-0-0-0"
COLD_TRACE_ID="$(kubectl exec -n signoz "$CH_POD" -c clickhouse -- clickhouse-client -q "
	SELECT traceID FROM signoz_traces.distributed_signoz_index_v3
	WHERE serviceName = 'fc-invoke' AND name = 'base_snapshot_build'
	ORDER BY timestamp DESC LIMIT 1
" 2>/dev/null | tr -d '[:space:]')"

if [[ -z "$COLD_TRACE_ID" ]]; then
	echo ">> no base_snapshot_build trace in ClickHouse yet (Task 1a rollout" >&2
	echo ">> hasn't landed, or hasn't rolled the fc-invoke pod since it did)." >&2
	: >"$TMP/cold_spans.ndjson"
else
	echo "  cold trace: $COLD_TRACE_ID"
	kubectl exec -n signoz "$CH_POD" -c clickhouse -- clickhouse-client -q "
		SELECT spanID as span_id, parentSpanID as parent_span_id, name,
		       durationNano / 1e6 as duration_ms
		FROM signoz_traces.distributed_signoz_index_v3
		WHERE traceID = '$COLD_TRACE_ID' AND serviceName = 'fc-invoke'
		ORDER BY timestamp ASC
		FORMAT JSONEachRow
	" >"$TMP/cold_spans.ndjson"
fi

echo "> writing data/trace.js"
python3 "$DIR/bake_trace.py" "$TMP/spans.ndjson" "$TMP/cold_spans.ndjson" "$OUT/trace.js"
echo "done: baked $OUT/trace.js"
