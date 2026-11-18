# Publishing factory data for static pages

A factory-cluster writer declares a Garage bucket claim in its chart with
`access: write` and a `public.r2Bucket` mirror configured with `public.manifestName: latest.json`. The writer uses only the
claim's scoped Garage S3 credential. The platform provisions the bucket and
runs the mirror with its own destination credential. Writers create no buckets
and hold no R2 admin credentials. Only place public data in a mirrored bucket.
See the [factory library bucket example](../projects/shared/helm/factory-library/README.md).

Hash the exact bytes of each payload and include the digest in its object key,
for example `stars/sites-<sha256>.json`. Upload every payload with
`Cache-Control: public, max-age=31536000, immutable`. After every upload
succeeds, write the bucket's root `latest.json` last with the keys for that
complete generation and `Cache-Control: public, max-age=60`. Give each
independent writer its own bucket so publishers cannot overwrite each other's index.

The [mirror template](../projects/shared/helm/factory-library/chart/templates/_mirror.tpl)
excludes exactly `/latest.json` from the immutable payload transfer and
copies it after that transfer succeeds. Readers fetch this root index from R2
and resolve its keys against the same bucket root. Retain payloads referenced
by cached manifests. If a referenced object is temporarily unavailable, retry
and keep serving the last complete snapshot. Verify the bytes and both cache
policies at the public endpoint before switching readers.

## Hub inventory and port issues (HOME-258)

The [deploy source list](../projects/jomcgi-site/data/sources.json) names the
current Stars and Hikes keys. The [hub chart](../projects/monolith/chart/values.yaml)
defines the schedules below (UTC). Each port must compare CPU-seconds and
peak resident memory against the Python job on the same inputs. Each port is
filed under [HOME-178](https://factory-plan.jomcgi.dev/factory/issue/HOME-178) with the Done when criteria below and its Go rationale ([record](https://factory-plan.jomcgi.dev/factory/issue/HOME-258#comment-4197)).

### Port Stars forecast publication to the factory cluster

Tracked in [HOME-399](https://factory-plan.jomcgi.dev/factory/issue/HOME-399).

The Python `stars-refresh` job runs at `0 */3 * * *`. It scores met.no hours
for the light-pollution site grid, persists site hours and reads the database
to publish `sites.json`, `map.json` and `history-map.json` in bucket `stars`.
Its Python dependencies load grid.json every six hours, load climatology.json
manually and prune elapsed hours at minute 50. Go's typed slices and bounded
batch encoder should reduce scoring CPU and peak heap across the roughly
14,000 sites. The met.no request limit still governs fetch throughput.

Done when

1. Factory jobs reproduce the grid, history and forecast inputs, cadence, provider throttling and last-good results after partial fetch failures.
2. The mirrored manifest resolves all three datasets. Compare scores, history and timestamps for the same inputs and exercise the site snapshot against them.
3. Record CPU and memory measurements, switch the reader, then retire the corresponding hub jobs after checking their other consumers.

### Port Hikes walk and forecast publication to the factory cluster

Tracked in [HOME-400](https://factory-plan.jomcgi.dev/factory/issue/HOME-400).

The Python `hikes-scrape-walks` job is manual (suspended schedule). It upserts
WalkHighlands routes and trailheads. At `20 */2 * * *`, `hikes-refresh-forecasts`
combines those walks with met.no forecasts, stores hourly windows and publishes
`stars/walks.json`. The Python prune job removes elapsed windows at minute 40.
Go can decode forecast fields into typed records and encode walks incrementally,
reducing object allocation, serialization CPU and peak memory.

Done when

1. Reproduce the manual catalogue update, two-hour forecast refresh and hourly pruning on the factory cluster. Preserve walk IDs and per-walk timestamps when a fetch fails.
2. Publish the walk snapshot under the sink contract. Verify walk metadata and forecast windows through the site and Scotland connector.
3. Compare CPU and memory on the same walk corpus, switch readers and retire the hub schedules.

### Port Trips ingestion and public data publication to the factory cluster

Tracked in [HOME-401](https://factory-plan.jomcgi.dev/factory/issue/HOME-401).

The Python FastAPI service handles each authenticated `POST /api/trips/ingest`
as it arrives. It reads a multipart image, trip slug, source and tags, validates
the raster and GPS EXIF, then stores the original under a content-derived key
and upserts a TripPoint. The public export must consume that private state.
Go can stream uploads through a hash into bounded temporary storage and parse
metadata without keeping a Python bytes object per upload. Expect lower peak
memory under concurrency and less CPU spent copying upload buffers.

The daily `trips-export` Argo CronWorkflow runs at 03:00 UTC. It reads the
private trip state through the API serializers, writes WebP photo derivatives
through the hub imgproxy presets, and commits `stars/trips/export.json` last
([export README](../projects/monolith/trips/export/README.md)). HOME-401 also
moves this job: it switches the export's readers to the factory output and
retires the hub schedule.

Done when

1. Preserve gateway authentication, request and response fields, validation and retry-safe upserts. Replay duplicate and invalid uploads against the factory endpoint.
2. Keep originals private. Publish only approved public points and photo derivatives through the mirrored claim and verify the Trips reader resolves a complete generation.
3. Measure CPU per image and peak memory at equal upload concurrency. Migrate existing points and images, verify parity, then switch the ingest route and retire the hub handler, the export's readers and the hub `trips-export` schedule.
