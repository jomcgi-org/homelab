"""Local synthetic archive batch, no network. Run with monolith on PYTHONPATH.

Usage: python stars/tests/archive_benchmark.py --sites 14000 --hours 48
Fixture insertion precedes tracemalloc. By default measure one bounded batch;
--drain measures an invocation until empty or budget failure. Fake storage keeps
only the current batch. Temporary SQLite files are discarded.
"""

import argparse
import io
import json
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import create_engine
from stars.archive import drain_archive, run_archive
from stars.models import HourArchiveCheckpoint, Site, SiteHour


class Storage:
    def __init__(self):
        self.objects = {}
        self.batch = None
        self.batches = 0
        self.serialized_bytes = 0
        self.max_chunk_bytes = 0
        self.max_chunk_rows = 0

    def put_object(self, *, Key, Body, IfNoneMatch, **kwargs):
        assert IfNoneMatch == "*"
        batch = Key.rsplit("/", 1)[0]
        if batch != self.batch:
            self.objects.clear()
            self.batch = batch
        if Key in self.objects:
            raise FileExistsError(Key)
        self.objects[Key] = Body
        self.serialized_bytes += len(Body)
        if Key.endswith("complete.json"):
            self.batches += 1
        else:
            self.max_chunk_bytes = max(self.max_chunk_bytes, len(Body))
            self.max_chunk_rows = max(
                self.max_chunk_rows, len(json.loads(Body)["rows"])
            )

    def get_object(self, *, Key, **kwargs):
        return {"Body": io.BytesIO(self.objects[Key])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", type=int, default=14000)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(
            f"sqlite:///{Path(directory) / 'benchmark.db'}",
            execution_options={"schema_translate_map": {"stars": None}},
        )
        for model in (Site, SiteHour, HourArchiveCheckpoint):
            model.__table__.create(engine)
        start = time.perf_counter()
        for first in range(0, args.sites, 100):
            sites = [
                {
                    "id": f"grid-{i:06d}",
                    "lat": 56.0,
                    "lon": -4.0,
                    "name": "Synthetic site",
                }
                for i in range(first, min(first + 100, args.sites))
            ]
            with engine.begin() as connection:
                connection.execute(Site.__table__.insert(), sites)
                for hour in range(args.hours):
                    connection.execute(
                        SiteHour.__table__.insert(),
                        [
                            {
                                "site_id": site["id"],
                                "hour_time": now - timedelta(hours=hour + 1),
                                "cloud_area_fraction": 9.0,
                                "air_temperature": 5.0,
                                "dew_spread": 3.0,
                                "sun_elevation_deg": -18.0,
                                "fetched_at": now - timedelta(days=3),
                                "symbol": "clearsky_night",
                            }
                            for site in sites
                        ],
                    )
        fixture_seconds = time.perf_counter() - start
        del sites
        storage = Storage()
        tracemalloc.start()
        start = time.perf_counter()
        if args.drain:
            deleted = drain_archive(engine, storage, "fake", "synthetic", now)
            result = {"deleted": deleted, "rows": deleted, "backlog": False}
        else:
            result = run_archive(engine, storage, "fake", "synthetic", now)
        seconds = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(
            json.dumps(
                {
                    "input_sites": args.sites,
                    "input_hours": args.hours,
                    "input_rows": args.sites * args.hours,
                    "fixture_seconds": fixture_seconds,
                    "batch": result,
                    "completed_batches": storage.batches,
                    "drain": args.drain,
                    "archive_seconds": seconds,
                    "rows_per_second": result["rows"] / seconds,
                    "serialized_bytes": storage.serialized_bytes,
                    "max_chunk_bytes": storage.max_chunk_bytes,
                    "max_chunk_rows": storage.max_chunk_rows,
                    "peak_tracemalloc_bytes": peak,
                    "measurement": "Python allocations including current-batch fake storage; excludes fixture and native/SDK allocations",
                },
                sort_keys=True,
            )
        )
        engine.dispose()


if __name__ == "__main__":
    main()
