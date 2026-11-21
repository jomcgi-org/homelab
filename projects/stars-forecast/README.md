# Factory Stars publication

The [architecture](ARCHITECTURE.md) describes the inputs, forecast state and publication order.

## Jobs

| Job | Runs | Does |
| --- | --- | --- |
| `inputs` | Argo CD Sync hook, wave 0, on every sync | Imports `grid.json` and `climatology.json` from R2 bucket `stars` into Garage `stars-state`. |
| `grid` | `0 */6 * * *` | Imports `grid.json` between syncs. |
| `refresh` | `50 */3 * * *`, wave 1 | Fetches met.no, prunes elapsed hours in each shard and publishes a generation. |

The refresh schedule follows the [met.no budget](../../docs/static-page-writers.md#metno-budget): it starts 50 minutes after the hub's `stars-refresh`, so the two do not overlap at expected run times. A CERRA climatology backfill reaches Garage at the next sync of the Application.

## Activation (HOME-478)

`refreshEnabled: true` turned on the refresh and the `inputs` hook. In wave 0, the hook imports both inputs with the read-only token `r2-s3-public-read`. If an import fails, the sync fails and the refresh CronJob does not sync. In wave 1, Argo CD syncs the refresh CronJob, so its first run finds both inputs.

The `factory-public` release mirrors the Garage bucket `stars-forecast` to prefix `stars-forecast/` of R2 bucket `factory-public` (HOME-492). Its `stars-forecast` source runs since HOME-478. This chart holds no R2 write credential.

## Live check

After the first refresh:

1. Argo CD shows `stars-forecast` Synced and Healthy. No `inputs` Job stays, because Argo CD deletes a successful hook. The refresh log shows `fetched forecasts for N/M sites` with N close to M.
2. Check the public generation. Each key `k` in the manifest is at `stars-forecast/k`:

   ```bash
   base=https://factory-data.jomcgi.dev/stars-forecast
   curl -sSI "$base/latest.json" | grep -i '^cache-control'  # public, max-age=60
   for k in $(curl -sS "$base/latest.json" | jq -r '.files[]'); do
     curl -sSI "$base/$k" | grep -i '^HTTP\|^cache-control'   # 200, public, max-age=31536000, immutable
     echo "$k $(curl -sS "$base/$k" | sha256sum)"            # digest equals the one in $k
   done
   ```

   Only the factory writer writes Garage `stars-forecast`, and the hub writes no `stars/sites-<sha256>.json` key. A key under `stars-forecast/` is thus a factory generation. The sites dataset has a `fetched_at` later than the rollout.
3. Compare scores, history and timestamps with a hub generation from the same inputs.

## Reader switch and hub retirement

[HOME-522](https://factory-plan.jomcgi.dev/factory/issue/HOME-522) switches the
site snapshot to `factory-public/stars-forecast/latest.json`. The site resolves
each manifest key under `stars-forecast/` and checks its SHA-256. The site R2
credential needs read access to both `factory-public` and the legacy `stars`
bucket while Hikes still uses the latter.

On 2026-11-21, the hub and factory published sites, map and history datasets
with 14,234 sites each. All three factory payloads matched the SHA-256 in
their object keys. The forecast samples used different fetch times
(15:05 UTC on the hub, 16:06 UTC on the factory). The replay below compares the
same inputs.

Keep the hub jobs until the Scotland connector, legacy public endpoints and
aurora grid have replacement sources. HOME-522 tracks this retirement after
the site reader switch. Hikes uses a separate writer and [HOME-521](https://factory-plan.jomcgi.dev/factory/issue/HOME-521).

## Rollback

1. To stop the refresh, set `refreshEnabled: false`. Argo CD removes the refresh CronJob and the `inputs` hook.
2. To stop the mirror, set the `stars-forecast` source in `projects/factory-public/chart/values.yaml` to `suspend: true`. R2 keeps what it has.

Before stopping the factory writer or mirror, restore the site's legacy Stars source and verify the site snapshot reads it.

## Same-input scoring measurement

On Linux amd64, a synthetic replay scored 2,500 copies of a 72-hour met.no series in batches of 500 at 56.25 N, 4.5 W. The series starts at 2026-02-01 00:00 UTC, with hourly cloud cover cycling from 0 to 19 percent. Python 3.12 with Astral 3.2 ran the hub's `score_location`. Go 1.24 ran `BenchmarkWinterBatch` for four measured batches plus the benchmark harness warmup batch. Each process retained each batch until it finished.

| Scoring process | User + system CPU seconds | Peak RSS (KiB) |
| --- | ---: | ---: |
| Python | 1.47 | 36,420 |
| Go | 0.09 | 22,996 |

These process measurements include startup. Full scheduled-job measurements must also include the provider, persistence and publication paths. The Go parity test checks the SHA-256 of the Python scorer's retained timestamps, rounded solar elevations and cloud readings.

A second replay ran both refresh and materialization paths for 256 sites with 72 forecast hours and 12 climatology rows per site. It used a local HTTP weather fixture, SQLite for the hub, and local object storage fixtures. Python consumed 3.60 CPU-seconds and peaked at 119,040 KiB RSS. Go consumed 1.52 CPU-seconds and peaked at 23,532 KiB RSS. The history payload matched exactly. The comparison exposed a timestamp formatting difference for a fetch at an exact second. The writer now omits the fractional part in that case, matching Python.
