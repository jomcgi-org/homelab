#!/usr/bin/env bash
# Bake real fc-invoke trace data for the public /ember/firecracker explainer.
#
# Restores: triggers real Python-sandbox runs through the private demos API
# and captures each run's span tree via the demo trace endpoint.
#
# Cold: the daemon only cold-boots once per workload at startup (see the
# Task 1 addendum to the firecracker public explainer design),
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
		-d '{"code": "print(\"baked for /ember/firecracker\")"}' | jq -r .trace_id)"
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
# Bake-time normalizer inlined as a heredoc (not a .py file) so gazelle
# does not generate a Bazel target for build-host-only tooling; same
# pattern as the grimoire bake script.
python3 - "$TMP/spans.ndjson" "$TMP/cold_spans.ndjson" "$OUT/trace.js" <<'PY'
"""Normalize captured fc-invoke span trees into phases for data/trace.js.

Bake-time tooling only (invoked by bake-fc-story.sh), not shipped in the app
bundle. Strips everything except phase names and millisecond durations: no
hostnames, IPs, node names, image refs, pod names, or trace ids reach the
output.

Two independent sources, per the Task 1 addendum to the firecracker public
explainer design: the daemon only cold boots once per workload at startup
(rooted in a `base_snapshot_build` span),
so it never appears on the demo-rooted trace path and is fetched straight
from ClickHouse instead. Every subsequent request is a snapshot restore,
captured through the demo API as before.

Usage: bake_trace.py <restores.ndjson> <cold_spans.ndjson> <out/trace.js>

  restores.ndjson    one {"trace_id": ..., "body": <GET /trace/{id} response>}
                      line per demo run (may include unusable/incomplete runs,
                      which are dropped).
  cold_spans.ndjson   flat JSONEachRow span rows (span_id, parent_span_id,
                      name, duration_ms) for the single latest
                      base_snapshot_build trace, or empty if none exists yet.
"""

from __future__ import annotations

import datetime
import json
import sys

# Warm-path phases in display order (Task 1 addendum vocabulary: no
# provision_rootfs, the daemon never cold-boots per request).
WARM_RESTORE_PHASE = "snapshot_restore"
SETUP_PHASES = ("auth_tokenreview", "acquire_slot")
TAIL_PHASES = ("guest_wait_ready", "guest_exec")

# Spans that exist in the real tree but are deliberately excluded: all four
# are post-response cleanup or internal implementation detail, not setup
# latency the visitor waits on / not one of the named story beats.
EXCLUDED_SPANS = {"guest_teardown", "vsock_prime", "vm_release", "bundle_cleanup"}

COLD_ROOT_SPAN = "base_snapshot_build"


def extract_warm(spans: list[dict]) -> list[dict] | None:
    """Return the ordered [{name, ms}, ...] phase list for one demo run.

    None if the run isn't a usable warm (snapshot_restore) run, e.g. spans
    hadn't finished ingesting when fetched.
    """
    by_name: dict[str, float] = {}
    for span in spans:
        name = span.get("name")
        if not name or name in EXCLUDED_SPANS:
            continue
        by_name[name] = span.get("duration_ms", 0.0)

    if WARM_RESTORE_PHASE not in by_name:
        return None

    order = [*SETUP_PHASES, WARM_RESTORE_PHASE, *TAIL_PHASES]
    phases = [
        {"name": name, "ms": round(by_name[name], 1)}
        for name in order
        if name in by_name
    ]
    return phases or None


def extract_cold(spans: list[dict]) -> list[dict] | None:
    """Return the ordered [{name, ms}, ...] phase list for the cold run.

    The real child span names under base_snapshot_build aren't fixed by this
    script (Task 1a defines them); every non-excluded, non-root child is kept
    in the order ClickHouse returns them (timestamp-ordered by the query),
    which is the build's own real sequencing.
    """
    phases = [
        {"name": s["name"], "ms": round(s.get("duration_ms", 0.0), 1)}
        for s in spans
        if s.get("name")
        and s["name"] not in EXCLUDED_SPANS
        and s["name"] != COLD_ROOT_SPAN
    ]
    return phases or None


def run_total(phases: list[dict]) -> float:
    # Total = sum of the included phases, not the root span duration: summing
    # what's actually shown keeps the displayed total exactly reconstructable
    # from the displayed breakdown, honest for both the warm root (fc_invoke
    # minus excluded tail spans) and the cold root (base_snapshot_build).
    return round(sum(p["ms"] for p in phases), 1)


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: bake_trace.py <restores.ndjson> <cold_spans.ndjson> <out/trace.js>",
            file=sys.stderr,
        )
        return 2

    restores_path, cold_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    warm_runs: list[dict] = []
    with open(restores_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            spans = record.get("body", {}).get("spans", [])
            phases = extract_warm(spans)
            if phases is None:
                continue
            warm_runs.append({"total": run_total(phases), "phases": phases})

    cold_spans: list[dict] = []
    with open(cold_path) as f:
        for line in f:
            line = line.strip()
            if line:
                cold_spans.append(json.loads(line))
    cold_phases = extract_cold(cold_spans) if cold_spans else None

    if cold_phases is None:
        print(
            "\n"
            "ERROR: no base_snapshot_build trace found in ClickHouse. The daemon\n"
            "only cold-boots once per workload at startup, rooted in that span\n"
            "(Task 1a); either the fc-invoke PR adding it hasn't rolled out yet,\n"
            "or the pod hasn't restarted since it did. Fabricating a cold run is\n"
            "not acceptable; wait for the rollout and re-run.\n"
            "\n",
            file=sys.stderr,
        )
        return 1

    if not warm_runs:
        print(
            "\n"
            "ERROR: zero warm (snapshot restore) runs captured. The replay finale\n"
            "needs several real restores; a cold-only dataset is not acceptable\n"
            "for this task. Re-run bake-fc-story.sh with more runs or check the\n"
            "demos API / port-forward is reachable.\n"
            "\n",
            file=sys.stderr,
        )
        return 1

    cold = {
        "total": run_total(cold_phases),
        "phases": cold_phases,
        "label": "measured at daemon startup",
    }
    baked_at = datetime.date.today().isoformat()

    lines = [
        "// GENERATED by bake-fc-story.sh - do not hand-edit.",
        "// Regenerate: ./bake-fc-story.sh 12",
        "",
        f"export const cold = {json.dumps(cold, indent=2)};",
        "",
        "export const restores = " + json.dumps(warm_runs, indent=2) + ";",
        "",
        f'export const bakedAt = "{baked_at}";',
        "",
    ]

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"  1 cold run (base_snapshot_build, {cold['total']}ms total)")
    print(f"  {len(warm_runs)} warm restore(s) captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
echo "done: baked $OUT/trace.js"
