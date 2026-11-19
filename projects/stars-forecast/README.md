# Factory Stars publication

The [architecture](ARCHITECTURE.md) describes the inputs, forecast state and publication order.

## Activation

1. Sync the chart and verify that the six-hour grid loader can read the existing Stars R2 bucket and write the private Garage bucket. Run the grid CronJob template once with its argument changed from `grid` to `climatology`. Repeat that manual import after a CERRA backfill. Verify both input counts against the hub.
2. Provide the mirror's scoped 1Password item and R2 bucket. Coordinate the three-hour weather schedule with the hub so the two jobs together stay within the provider request budget. Enable `refreshEnabled` to start refresh and hourly prune. For a parallel comparison, replay captured weather responses instead of fetching the same grid twice.
3. Unsuspend the mirror after verifying its destination. Resolve all three keys from its R2 root manifest, verify the digests and cache headers, and compare scores, history and timestamps with a hub generation from identical inputs. Exercise the site snapshot using the new bucket and manifest.
4. Set the Stars section in `projects/jomcgi-site/data/sources.json` to bucket `stars-forecast` and add `"manifest": "latest.json"`. Keep the logical file names. Check the published page. Roll back by restoring the previous Stars section.
5. The Scotland connector still reads the old bucket. Account for its sites, history and optional uncapped feed, the hub public Stars endpoints and the shared grid used by aurora before retiring their input, refresh or prune jobs. Keep the separate Hikes writer and its walks object.

## Same-input scoring measurement

On Linux amd64, a synthetic replay scored 2,500 copies of a 72-hour met.no series in batches of 500 at 56.25 N, 4.5 W. The series starts at 2026-02-01 00:00 UTC, with hourly cloud cover cycling from 0 to 19 percent. Python 3.12 with Astral 3.2 ran the hub's `score_location`. Go 1.24 ran `BenchmarkWinterBatch` for four measured batches plus the benchmark harness warmup batch. Each process retained each batch until it finished.

| Scoring process | User + system CPU seconds | Peak RSS (KiB) |
| --- | ---: | ---: |
| Python | 1.47 | 36,420 |
| Go | 0.09 | 22,996 |

These process measurements include startup. Full scheduled-job measurements must also include the provider, persistence and publication paths. The Go parity test checks the SHA-256 of the Python scorer's retained timestamps, rounded solar elevations and cloud readings.

A second replay ran both refresh and materialization paths for 256 sites with 72 forecast hours and 12 climatology rows per site. It used a local HTTP weather fixture, SQLite for the hub, and local object storage fixtures. Python consumed 3.60 CPU-seconds and peaked at 119,040 KiB RSS. Go consumed 1.52 CPU-seconds and peaked at 23,532 KiB RSS. The history payload matched exactly. The comparison exposed a timestamp formatting difference for a fetch at an exact second. The writer now omits the fractional part in that case, matching Python.
