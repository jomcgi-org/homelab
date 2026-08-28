#!/usr/bin/env python3
"""Measure BuildBuddy cache traffic and compare committed snapshots."""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


# 2 added "concentration" and "top_invocations". Readers tolerate their absence,
# so schema 1 snapshots still compare and render.
SCHEMA_VERSION = 2
_HOST = "https://app.buildbuddy.io"
# Fallback only. BuildBuddy keys invocations by the repo URL it was told at
# push time, so a hardcoded one goes stale the moment the repo moves: the
# 2026-08-22 move to the jomcgi-org org made every default-argument query
# return nothing after that date, silently, while still printing a report.
# Ask git instead, and pass --repo "" to measure the whole group across a
# window that spans a move.
_FALLBACK_REPO = "https://github.com/jomcgi-org/homelab"


def _discover_repo():
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return _FALLBACK_REPO
    if url.startswith("git@"):
        url = "https://" + url[len("git@") :].replace(":", "/", 1)
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url or _FALLBACK_REPO


def _int(data, key):
    value = data.get(key)
    if value in (None, ""):
        return 0
    return int(value)


def normalise_pattern(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if value is None:
        value = ""
    return str(value)[:60]


def source_key(inv: dict) -> tuple[str, str, str]:
    return (
        inv.get("role") or "LOCAL",
        inv.get("command") or "",
        normalise_pattern(inv.get("pattern")),
    )


_ZERO_TOTALS = {
    "download_bytes": 0,
    "upload_bytes": 0,
    "download_transferred_bytes": 0,
    "upload_transferred_bytes": 0,
    "invocations": 0,
}


def _new_totals():
    return dict(_ZERO_TOTALS)


def _invocation_totals(inv):
    stats = inv.get("cacheStats") or {}
    return {
        "download_bytes": _int(stats, "totalDownloadSizeBytes"),
        "upload_bytes": _int(stats, "totalUploadSizeBytes"),
        "download_transferred_bytes": _int(stats, "totalDownloadTransferredSizeBytes"),
        "upload_transferred_bytes": _int(stats, "totalUploadTransferredSizeBytes"),
        "invocations": 1,
    }


def concentration(download_values: list[int], fractions=(0.01, 0.05, 0.10)) -> dict:
    values = sorted(download_values)
    count = len(values)
    total = sum(values)
    if not values:
        percentiles = {"p50_bytes": 0.0, "p90_bytes": 0.0, "p99_bytes": 0.0}
        maximum = 0
    else:
        percentiles = {}
        for name, percentile in (
            ("p50_bytes", 0.50),
            ("p90_bytes", 0.90),
            ("p99_bytes", 0.99),
        ):
            index = min(count - 1, int(math.ceil(percentile * count)) - 1)
            percentiles[name] = float(values[index])
        maximum = values[-1]
    top_shares = {}
    for fraction in fractions:
        top_count = min(count, max(1, int(round(count * fraction)))) if count else 0
        top_bytes = sum(values[-top_count:]) if top_count else 0
        top_shares[f"{fraction * 100:g}%"] = {
            "invocations": top_count,
            "bytes": top_bytes,
            "share": top_bytes / total if total else 0.0,
        }
    return {
        "invocations": count,
        "total_bytes": total,
        **percentiles,
        "max_bytes": maximum,
        "top_shares": top_shares,
    }


def top_invocations(invocations: list[dict], limit: int = 20) -> list[dict]:
    rows = []
    for inv in sorted(
        invocations,
        key=lambda item: _invocation_totals(item)["download_bytes"],
        reverse=True,
    )[:limit]:
        values = _invocation_totals(inv)
        identifier = inv.get("id", {})
        if isinstance(identifier, dict):
            invocation_id = identifier.get("invocationId", inv.get("invocationId", ""))
        else:
            invocation_id = inv.get("invocationId", identifier or "")
        invocation_id = str(invocation_id)
        role, command, pattern = source_key(inv)
        rows.append(
            {
                "invocation_id": invocation_id,
                "url": f"{_HOST}/invocation/{invocation_id}",
                "role": role,
                "command": command,
                "pattern": pattern,
                "branch": inv.get("branchName") or "",
                "success": bool(inv.get("success", False)),
                "download_bytes": values["download_bytes"],
                "upload_bytes": values["upload_bytes"],
            }
        )
    return rows


def _add_totals(destination, values):
    for key in destination:
        destination[key] += values[key]


def aggregate(invocations: list[dict], window_days: float) -> dict:
    totals = _new_totals()
    by_role = {}
    by_source = {}
    for inv in invocations:
        values = _invocation_totals(inv)
        _add_totals(totals, values)
        role, command, pattern = source_key(inv)
        role_totals = by_role.setdefault(role, _new_totals())
        _add_totals(role_totals, values)
        source_totals = by_source.setdefault((role, command, pattern), _new_totals())
        _add_totals(source_totals, values)

    divisor = window_days if window_days else 1.0
    per_day = {
        "download_bytes": totals["download_bytes"] / divisor,
        "upload_bytes": totals["upload_bytes"] / divisor,
    }
    sources = []
    for (role, command, pattern), values in by_source.items():
        share = (
            values["download_bytes"] / totals["download_bytes"]
            if totals["download_bytes"]
            else 0.0
        )
        sources.append(
            {
                "role": role,
                "command": command,
                "pattern": pattern,
                "download_bytes": values["download_bytes"],
                "upload_bytes": values["upload_bytes"],
                "invocations": values["invocations"],
                "download_share": share,
            }
        )
    sources.sort(key=lambda item: item["download_bytes"], reverse=True)
    return {
        "totals": totals,
        "per_day": per_day,
        "by_role": by_role,
        "by_source": sources,
        "concentration": concentration(
            [_invocation_totals(inv)["download_bytes"] for inv in invocations]
        ),
    }


def _window_days(snapshot):
    value = snapshot.get("window", {}).get("days", 1)
    return float(value) if value else 1.0


def compare(baseline: dict, current: dict, target_reduction: float = 0.5) -> dict:
    baseline_download = float(baseline.get("per_day", {}).get("download_bytes", 0))
    current_download = float(current.get("per_day", {}).get("download_bytes", 0))
    baseline_upload = float(baseline.get("per_day", {}).get("upload_bytes", 0))
    current_upload = float(current.get("per_day", {}).get("upload_bytes", 0))
    download_change = (
        (current_download - baseline_download) / baseline_download
        if baseline_download
        else (0.0 if current_download == 0 else 1.0)
    )
    upload_change = (
        (current_upload - baseline_upload) / baseline_upload
        if baseline_upload
        else (0.0 if current_upload == 0 else 1.0)
    )
    target = baseline_download * (1.0 - target_reduction)
    required = baseline_download - target
    progress = (
        (baseline_download - current_download) / required if required > 0 else 0.0
    )
    progress = max(0.0, min(1.0, progress))

    def source_map(snapshot):
        divisor = _window_days(snapshot)
        return {
            (
                row.get("role", "LOCAL"),
                row.get("command", ""),
                row.get("pattern", ""),
            ): float(row.get("download_bytes", 0)) / divisor
            for row in snapshot.get("by_source", [])
        }

    old_sources = source_map(baseline)
    new_sources = source_map(current)
    movers = []
    for key in set(old_sources) | set(new_sources):
        old = old_sources.get(key, 0.0)
        new = new_sources.get(key, 0.0)
        delta = new - old
        if delta != 0:
            movers.append(
                {
                    "role": key[0],
                    "command": key[1],
                    "pattern": key[2],
                    "baseline_per_day": old,
                    "current_per_day": new,
                    "delta_per_day": delta,
                }
            )
    movers.sort(key=lambda item: item["delta_per_day"])
    return {
        "baseline_per_day_download": baseline_download,
        "current_per_day_download": current_download,
        "download_change": download_change,
        "upload_change": upload_change,
        "target_per_day_download": target,
        "target_reduction": target_reduction,
        "progress": progress,
        "met": current_download <= target,
        "movers": movers,
    }


def format_bytes(n: float) -> str:
    value = float(n)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while abs(value) >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{value:.0f} B"


def render_report(snapshot: dict, comparison: dict | None) -> str:
    totals = snapshot.get("totals", {})
    window = snapshot.get("window", {})
    lines = [
        f"BuildBuddy cache usage, {window.get('days', '?')} day window",
        f"Total download: {format_bytes(totals.get('download_bytes', 0))}, "
        f"upload: {format_bytes(totals.get('upload_bytes', 0))}, "
        f"invocations: {totals.get('invocations', 0)}",
    ]
    if snapshot.get("truncated"):
        lines.append(
            "WARNING: invocation cap reached, these numbers are a floor, not a total."
        )
    lines += ["", "Top sources", "download  share  upload  invocations  source"]
    for row in snapshot.get("by_source", [])[:15]:
        label = ":".join(
            (row.get("role", "LOCAL"), row.get("command", ""), row.get("pattern", ""))
        )
        lines.append(
            f"{format_bytes(row.get('download_bytes', 0)):>10}  "
            f"{row.get('download_share', 0.0):>5.1%}  "
            f"{format_bytes(row.get('upload_bytes', 0)):>10}  "
            f"{row.get('invocations', 0):>11}  {label}"
        )
    concentration_data = snapshot.get("concentration")
    if concentration_data:
        lines += [
            "",
            "Concentration",
            f"  p50 {format_bytes(concentration_data.get('p50_bytes', 0))}   "
            f"p90 {format_bytes(concentration_data.get('p90_bytes', 0))}   "
            f"p99 {format_bytes(concentration_data.get('p99_bytes', 0))}   "
            f"max {format_bytes(concentration_data.get('max_bytes', 0))}",
        ]
        for fraction, values in concentration_data.get("top_shares", {}).items():
            lines.append(
                f"  top {fraction} of invocations: {format_bytes(values.get('bytes', 0))} "
                f"({values.get('share', 0.0):.1%} of downloads)"
            )
        lines += [
            "",
            "Worst invocations",
            "download  role  command  pattern  branch  URL",
        ]
        for row in snapshot.get("top_invocations", [])[:8]:
            lines.append(
                f"{format_bytes(row.get('download_bytes', 0)):>10}  {row.get('role', 'LOCAL')}  "
                f"{row.get('command', '')}  {row.get('pattern', '')}  "
                f"{row.get('branch', '')}  {row.get('url', '')}"
            )
    lines += ["", "By role"]
    for role, values in sorted(snapshot.get("by_role", {}).items()):
        lines.append(
            f"{role}: {format_bytes(values.get('download_bytes', 0))} download, "
            f"{format_bytes(values.get('upload_bytes', 0))} upload, {values.get('invocations', 0)} invocations"
        )
    if comparison is not None:
        lines += ["", "Progress"]
        lines.append(
            f"Download per day: {format_bytes(comparison['baseline_per_day_download'])} -> "
            f"{format_bytes(comparison['current_per_day_download'])} -> "
            f"{format_bytes(comparison['target_per_day_download'])} target"
        )
        lines.append(
            f"Reduction progress: {comparison['progress']:.1%}, target met: {comparison['met']}"
        )
        lines.append("Top movers")
        for row in comparison["movers"][:10]:
            label = ":".join((row["role"], row["command"], row["pattern"]))
            lines.append(f"{format_bytes(row['delta_per_day'])} per day  {label}")
    return "\n".join(lines) + "\n"


def _post_json(path, api_key, body):
    request = urllib.request.Request(
        _HOST + path, data=json.dumps(body).encode(), method="POST"
    )
    request.add_header("x-buildbuddy-api-key", api_key)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"BuildBuddy returned non-JSON response: {raw[:300]}"
        ) from error


def _rfc3339(days_ago=0):
    instant = _datetime.datetime.now(_datetime.timezone.utc) - _datetime.timedelta(
        days=days_ago
    )
    return instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _query(repo, days):
    result = {"updated_after": _rfc3339(days), "updated_before": _rfc3339()}
    if repo:
        result["repo_url"] = repo
    return result


def _context(group_id):
    return {"request_context": {"group_id": group_id}}


def _get_group_id(api_key, explicit):
    if explicit:
        return explicit
    if os.environ.get("BUILDBUDDY_GROUP_ID"):
        return os.environ["BUILDBUDDY_GROUP_ID"]
    try:
        shas = subprocess.check_output(
            ["git", "rev-list", "--max-count=25", "HEAD"], text=True
        ).splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            "Could not discover the BuildBuddy group ID, set BUILDBUDDY_GROUP_ID"
        ) from error
    for sha in shas:
        try:
            public = _post_json(
                "/api/v1/GetInvocation", api_key, {"selector": {"commit_sha": sha}}
            )
            invocations = public.get("invocation", [])
            invocation_id = (
                invocations[0].get("id", {}).get("invocationId")
                if invocations
                else None
            )
            if not invocation_id:
                continue
            internal = _post_json(
                "/rpc/BuildBuddyService/GetInvocation",
                api_key,
                {"lookup": {"invocation_id": invocation_id}},
            )
            found = internal.get("invocation", [])
            group_id = found[0].get("acl", {}).get("groupId") if found else None
            if group_id:
                return group_id
        except (RuntimeError, KeyError, IndexError, TypeError):
            continue
    raise RuntimeError(
        "Could not discover the BuildBuddy group ID, set BUILDBUDDY_GROUP_ID"
    )


def _search(api_key, group_id, query, maximum):
    body = {
        "request_context": {"group_id": group_id},
        "query": query,
        "count": min(200, maximum),
    }
    result = []
    truncated = False
    while len(result) < maximum:
        page = _post_json("/rpc/BuildBuddyService/SearchInvocation", api_key, body)
        entries = page.get("invocation", [])
        remaining = maximum - len(result)
        result.extend(entries[:remaining])
        token = page.get("nextPageToken")
        if not token:
            break
        if len(result) >= maximum:
            truncated = True
            break
        body["page_token"] = token
    return result, truncated


def _trend(api_key, group_id, query):
    body = dict(_context(group_id), query=query)
    response = _post_json("/rpc/BuildBuddyService/GetTrend", api_key, body)
    entries = response.get("trendStat", [])
    entries.sort(key=lambda item: _int(item, "bucketStartTimeMicros"))
    buckets = []
    for entry in entries:
        timestamp = _int(entry, "bucketStartTimeMicros") / 1_000_000
        bucket_start = _datetime.datetime.fromtimestamp(
            timestamp, _datetime.timezone.utc
        ).isoformat()
        buckets.append(
            {
                "bucket_start": bucket_start,
                "download_bytes": _int(entry, "totalDownloadSizeBytes"),
                "upload_bytes": _int(entry, "totalUploadSizeBytes"),
                "builds": _int(entry, "totalNumBuilds"),
                "action_cache_hits": _int(entry, "actionCacheHits"),
                "action_cache_misses": _int(entry, "actionCacheMisses"),
            }
        )
    return buckets, response.get("interval")


def roll_up_daily(buckets: list[dict]) -> list[dict]:
    daily = {}
    metric_keys = (
        "download_bytes",
        "upload_bytes",
        "builds",
        "action_cache_hits",
        "action_cache_misses",
    )
    for bucket in buckets:
        try:
            date = (
                _datetime.datetime.fromisoformat(bucket["bucket_start"])
                .date()
                .isoformat()
            )
        except (KeyError, TypeError, ValueError):
            continue
        row = daily.setdefault(date, {"date": date, **{key: 0 for key in metric_keys}})
        for key in metric_keys:
            value = bucket.get(key, 0)
            row[key] += int(value) if value not in (None, "") else 0
    return [daily[date] for date in sorted(daily)]


def _snapshot_files(out_dir):
    return sorted(Path(out_dir).glob("*.json"))


def _load(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _build_snapshot(api_key, group_id, args):
    query = _query(args.repo, args.days)
    invocations, truncated = _search(api_key, group_id, query, args.max_invocations)
    result = aggregate(invocations, args.days)
    result["top_invocations"] = top_invocations(invocations, 20)
    buckets, interval = _trend(api_key, group_id, query)
    result.update(
        {
            "schema": SCHEMA_VERSION,
            "captured_at": _rfc3339(),
            "repo": args.repo,
            "window": {
                "days": args.days,
                "after": query["updated_after"],
                "before": query["updated_before"],
            },
            "truncated": truncated,
            "buckets": buckets,
            "bucket_interval": interval,
        }
    )
    return result


def _default_out_dir():
    return Path(__file__).resolve().parent / "snapshots"


def _parser():
    default_repo = _discover_repo()
    parser = argparse.ArgumentParser(description="Measure BuildBuddy cache traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--days", type=float, default=7)
    snap.add_argument("--repo", default=default_repo)
    snap.add_argument("--group-id")
    snap.add_argument("--max-invocations", type=int, default=20000)
    snap.add_argument("--out-dir", type=Path, default=_default_out_dir())
    snap.add_argument("--no-write", action="store_true")
    snap.add_argument("--baseline", type=Path)
    snap.add_argument("--target-reduction", type=float, default=0.5)
    report = sub.add_parser("report")
    report.add_argument("--snapshot", type=Path)
    report.add_argument("--baseline", type=Path)
    report.add_argument("--out-dir", type=Path, default=_default_out_dir())
    report.add_argument("--target-reduction", type=float, default=0.5)
    trend = sub.add_parser("trend")
    trend.add_argument("--days", type=float, default=30)
    trend.add_argument("--repo", default=default_repo)
    trend.add_argument("--group-id")
    outliers = sub.add_parser("outliers")
    outliers.add_argument("--days", type=float, default=7)
    outliers.add_argument("--repo", default=default_repo)
    outliers.add_argument("--group-id")
    outliers.add_argument("--max-invocations", type=int, default=20000)
    outliers.add_argument("--limit", type=int, default=25)
    outliers.add_argument("--min-gb", type=float, default=0.0)
    return parser


def _render_concentration(invocations, limit):
    data = concentration(
        [_invocation_totals(inv)["download_bytes"] for inv in invocations]
    )
    lines = [
        "Concentration",
        f"  p50 {format_bytes(data['p50_bytes'])}   p90 {format_bytes(data['p90_bytes'])}   "
        f"p99 {format_bytes(data['p99_bytes'])}   max {format_bytes(data['max_bytes'])}",
    ]
    for fraction, values in data["top_shares"].items():
        lines.append(
            f"  top {fraction} of invocations: {format_bytes(values['bytes'])} "
            f"({values['share']:.1%} of downloads)"
        )
    lines += ["", "Worst invocations", "download  role  command  pattern  branch  URL"]
    for row in top_invocations(invocations, limit):
        lines.append(
            f"{format_bytes(row['download_bytes']):>10}  {row['role']}  {row['command']}  "
            f"{row['pattern']}  {row['branch']}  {row['url']}"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "report":
            files = _snapshot_files(args.out_dir)
            current = _load(args.snapshot or files[-1])
            baseline_path = args.baseline or (files[0] if files else None)
            baseline = (
                _load(baseline_path)
                if baseline_path and baseline_path != (args.snapshot or files[-1])
                else None
            )
            print(
                render_report(
                    current,
                    compare(baseline, current, args.target_reduction)
                    if baseline
                    else None,
                ),
                end="",
            )
            return 0
        api_key = os.environ.get("BUILDBUDDY_API_KEY")
        if not api_key:
            print("BUILDBUDDY_API_KEY is required", file=sys.stderr)
            return 2
        group_id = _get_group_id(api_key, args.group_id)
        if args.command == "trend":
            buckets, interval = _trend(api_key, group_id, _query(args.repo, args.days))
            print("date builds download upload AC hit rate")
            if interval is not None:
                print(f"bucket interval: {interval} (rolled up to days)")
            for row in roll_up_daily(buckets):
                total = row["action_cache_hits"] + row["action_cache_misses"]
                rate = row["action_cache_hits"] / total if total else 0.0
                print(
                    f"{row['date']} {row['builds']} {format_bytes(row['download_bytes'])} "
                    f"{format_bytes(row['upload_bytes'])} {rate:.1%}"
                )
            return 0
        if args.command == "outliers":
            query = _query(args.repo, args.days)
            invocations, _ = _search(api_key, group_id, query, args.max_invocations)
            minimum = args.min_gb * 1_000_000_000
            invocations = [
                inv
                for inv in invocations
                if _invocation_totals(inv)["download_bytes"] >= minimum
            ]
            print(_render_concentration(invocations, args.limit), end="")
            return 0
        snapshot = _build_snapshot(api_key, group_id, args)
        files = _snapshot_files(args.out_dir)
        baseline_path = args.baseline or (files[0] if files else None)
        baseline = _load(baseline_path) if baseline_path else None
        print(
            render_report(
                snapshot,
                compare(baseline, snapshot, args.target_reduction)
                if baseline
                else None,
            ),
            end="",
        )
        if not args.no_write:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            date_stem = (
                _datetime.datetime.now(_datetime.timezone.utc).date().isoformat()
            )
            path = args.out_dir / f"{date_stem}.json"
            suffix = 2
            while path.exists():
                path = args.out_dir / f"{date_stem}-{suffix}.json"
                suffix += 1
            path.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"Wrote snapshot to {path}", file=sys.stderr)
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2 if "group ID" in str(error) else 1
    except (OSError, ValueError, IndexError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
