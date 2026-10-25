"""Deterministic scalar-load fixture and offline materialization benchmark.

Run as stars.tests.snapshot_fixture with PYTHONPATH=projects/monolith. No storage or
weather calls are made. The sink retains lengths/digests, never response bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from stars import materialize, router

NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def metadata(site_id="covered", fetched=NOW):
    return SimpleNamespace(
        id=site_id,
        name=None,
        lat=57.3,
        lon=-3.7,
        altitude_m=420,
        lp_zone="pristine",
        forecast_fetched_at=fetched,
    )


def hours(site_id):
    # 72 hourly steps, plus a past row and a sparse 6-hourly tail to day 7.
    # Three nights have 16 qualifying hours each, including twilight-only
    # boundaries. Daylight and cloudy hours must never enter best_hours.
    for offset in [-1, *range(72), *range(78, 169, 6)]:
        hour = NOW + timedelta(hours=offset)
        sun = -11.0 if hour.hour in (16, 7) else -18.0
        cloud = 5.0
        if 8 <= hour.hour < 16:
            sun = 5.0
        if offset == 10:
            cloud, sun = 80.0, -18.0
        yield (site_id, hour, cloud, sun, NOW)


class ScalarSession:
    """Mimic only the two upcoming scalar selects, with fresh result lists."""

    def __init__(self, sites=1, edge_cases=False):
        self.sites = sites
        self.edge_cases = edge_cases
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def exec(self, query):
        self.queries.append(str(query))
        if len(self.queries) % 2 == 1:
            rows = [metadata(f"site-{i:05d}") for i in range(self.sites)]
            if self.edge_cases:
                rows += [
                    metadata("empty"),
                    metadata("stale", NOW - timedelta(days=3)),
                    metadata("missing", None),
                ]
        else:
            rows = [
                r
                for i in range(self.sites)
                for r in reversed(list(hours(f"site-{i:05d}")))
                if r[1] >= NOW
            ]
        return SimpleNamespace(all=lambda: rows)


def run(producer=materialize, *, sites=1, enabled=False, edge_cases=False, sink=None):
    """Exercise the real builder, materializer and serializers with mocked I/O."""
    session = ScalarSession(sites, edge_cases)
    writes = {}

    def put_object(**kw):
        body = kw["Body"]
        writes[kw["Key"]] = {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        if sink:
            sink(kw)

    env = {
        "STARS_GRID_S3_BUCKET": "stars",
        "STARS_SITES_S3_KEY": "sites.json",
        "STARS_MAP_S3_KEY": "map.json",
        "STARS_HISTORY_MAP_S3_KEY": "history-map.json",
        "STARS_CONNECTOR_SITES_S3_KEY": "connector-sites.json" if enabled else "",
    }
    with (
        patch.dict(os.environ, env),
        patch.object(router, "datetime", Clock),
        patch.object(producer, "Session", return_value=session),
        patch.object(producer, "get_engine", return_value=None),
        patch.object(producer, "get_history", return_value={"sites": []}),
        patch.object(
            producer, "_s3_client", return_value=SimpleNamespace(put_object=put_object)
        ),
    ):
        returned = producer._materialize_sync()
    assert len(session.queries) == 2
    assert returned == writes["sites.json"]["bytes"]
    return writes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", type=int, default=14000)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument(
        "--no-tracemalloc",
        action="store_true",
        help="Measure Linux RSS without tracing overhead",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Also save local bodies for the offline Store.Refresh benchmark",
    )
    parser.add_argument(
        "--baseline-revision",
        help="Git revision of materialize.py; uses the current router.py",
    )
    parser.add_argument(
        "--fixture", action="store_true", help="Print the small cross-language fixture"
    )
    args = parser.parse_args()
    if args.fixture:
        run(
            enabled=True,
            edge_cases=True,
            sink=lambda kw: (
                print(kw["Body"].decode())
                if kw["Key"] == "connector-sites.json"
                else None
            ),
        )
        return
    producer = materialize
    if args.baseline_revision:
        source = subprocess.check_output(
            [
                "git",
                "show",
                f"{args.baseline_revision}:projects/monolith/stars/materialize.py",
            ],
            text=True,
        )
        producer = ModuleType("baseline")
        # Explicit local Git revision, used only by this offline comparison.
        exec(compile(source, "baseline-materialize.py", "exec"), producer.__dict__)  # noqa: S102
    sink = None
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        sink = lambda kw: (args.output_dir / kw["Key"]).write_bytes(kw["Body"])
    if not args.no_tracemalloc:
        tracemalloc.start()
    writes = run(producer, sites=args.sites, enabled=args.enabled, sink=sink)
    peak = None
    if not args.no_tracemalloc:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    print(
        json.dumps(
            {
                "sites": args.sites,
                "input_rows_per_site": len(list(hours("x"))),
                "upcoming_rows_per_site": sum(r[1] >= NOW for r in hours("x")),
                "qualifying_per_site": sum(
                    r[1] >= NOW and r[2] < 10 and r[3] < -10 for r in hours("x")
                ),
                "enabled": args.enabled,
                "materializer_revision": args.baseline_revision or "working-tree",
                "router_revision": "working-tree",
                "objects": writes,
                "total_bytes": sum(w["bytes"] for w in writes.values()),
                "python_allocation_peak_bytes": peak,
                "linux_process_max_rss_bytes": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss
                * 1024,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
