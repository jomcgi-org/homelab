"""Best-effort SigNoz span collection and hop bucketing."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

HOPS = (
    "cloudflare-tunnel (gateway)",
    "context-forge",
    "monolith-backend",
    "embervm-control",
    "guest / shim",
)


def _bucket_name(service: str) -> str:
    lowered = service.lower()
    if "cloudflare" in lowered or "cloudflared" in lowered:
        return HOPS[0]
    if "forge" in lowered or "mcpgateway" in lowered:
        return HOPS[1]
    if service == "monolith-backend":
        return HOPS[2]
    if service == "embervm-control":
        return HOPS[3]
    return HOPS[4]


def bucket_spans(spans: list[dict]) -> dict:
    """Bucket spans by hop and derive EmberVM phase timings when possible."""
    buckets = {
        hop: {
            "spans": 0,
            "total_ms": 0.0,
            "max_ms": 0.0,
            "service_names": [],
            "by_name": {},
        }
        for hop in HOPS
    }
    all_services: set[str] = set()
    phases: dict[str, float] = {}
    for span in spans:
        service = str(span.get("serviceName", "") or "unknown")
        name = str(span.get("name", ""))
        all_services.add(service)
        try:
            duration_ms = float(span.get("durationNano", 0) or 0) / 1_000_000
        except (TypeError, ValueError):
            duration_ms = 0.0
        bucket = buckets[_bucket_name(service)]
        bucket["spans"] += 1
        bucket["total_ms"] += duration_ms
        bucket["max_ms"] = max(bucket["max_ms"], duration_ms)
        bucket["service_names"].append(service)
        name_key = f"{service}: {name}"
        named = bucket["by_name"].setdefault(
            name_key, {"spans": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        named["spans"] += 1
        named["total_ms"] += duration_ms
        named["max_ms"] = max(named["max_ms"], duration_ms)
        if service == "embervm-control":
            lowered = name.lower()
            if "restore" in lowered:
                phases["session_create_restore_ms"] = max(
                    phases.get("session_create_restore_ms", 0.0), duration_ms
                )
            elif "boot" in lowered:
                phases["session_create_boot_ms"] = max(
                    phases.get("session_create_boot_ms", 0.0), duration_ms
                )
            if "invoke" in lowered:
                phases["invoke_ms"] = max(phases.get("invoke_ms", 0.0), duration_ms)
    for bucket in buckets.values():
        bucket["total_ms"] = round(bucket["total_ms"], 3)
        bucket["max_ms"] = round(bucket["max_ms"], 3)
        bucket["service_names"] = sorted(set(bucket["service_names"]))
        for named in bucket["by_name"].values():
            named["total_ms"] = round(named["total_ms"], 3)
            named["max_ms"] = round(named["max_ms"], 3)
    return {
        "buckets": buckets,
        "service_names_seen": sorted(all_services),
        "embervm_phases": phases,
    }


def _clickhouse(sql: str) -> str:
    command = [
        "kubectl",
        "exec",
        "-n",
        "signoz",
        "chi-signoz-clickhouse-cluster-0-0-0",
        "-c",
        "clickhouse",
        "--",
        "clickhouse-client",
        "-q",
        sql,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"kubectl exited {result.returncode}"
        )
    return result.stdout


def _rows(raw: str) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def collect_spans(started_at: datetime, ended_at: datetime) -> dict:
    """Query candidate traces in the probe window, then load all their spans."""
    start = (started_at - timedelta(seconds=5)).isoformat()
    end = (ended_at + timedelta(seconds=5)).isoformat()
    table = "signoz_traces.distributed_signoz_index_v3"
    find_sql = f"""
SELECT DISTINCT traceID
FROM {table}
WHERE timestamp >= parseDateTime64BestEffort('{start}')
  AND timestamp <= parseDateTime64BestEffort('{end}')
  AND serviceName = 'monolith-backend'
  AND (name LIKE '%/api/agents/sessions%'
       OR attributes_string['http.target'] LIKE '%/api/agents/sessions%'
       OR attributes_string['url.path'] LIKE '%/api/agents/sessions%')
FORMAT JSONEachRow
""".strip()
    trace_ids = {
        str(row.get("traceID"))
        for row in _rows(_clickhouse(find_sql))
        if row.get("traceID")
    }
    safe_ids = sorted(
        trace_id for trace_id in trace_ids if trace_id and trace_id.isalnum()
    )
    if not safe_ids:
        return bucket_spans([])
    quoted = ", ".join(f"'{trace_id}'" for trace_id in safe_ids)
    load_sql = f"""
SELECT traceID, spanID, parentSpanID, name, serviceName, durationNano, hasError,
       timestamp, attributes_string, resources_string
FROM {table}
WHERE traceID IN ({quoted})
FORMAT JSONEachRow
""".strip()
    return bucket_spans(_rows(_clickhouse(load_sql)))
