# Static trip export

The daily `trips-export` Argo CronWorkflow runs at 03:00 UTC through
`app/jobs_main.py`. It reads the serializers for `/api/trips/trips` and
`/api/trips/trip/{slug}` and exports their JSON responses without changes.
HOME-212 site ingestion, static pages and route activation are separate work.

```text
Postgres ── API serializers ──► private stars/trips/index.json, <slug>.json
                           └─► private stars/trips/v/<name>-<sha256>.json
private monolith-trips originals ── signed hub imgproxy presets
                           └─► private stars/trips/photos/<original-key>/*.webp
                                     └─ large exports: factory-public/trips/photos/…
all referenced objects complete ──► private stars/trips/export.json
```

Each unique photo has five WebP variants: `thumb`, `gallery`, `preview`,
`display` and `full`. The exporter uses the existing hub imgproxy presets,
rejects non-WebP output and EXIF/XMP chunks, and skips variants already in R2.
Originals stay in the private `monolith-trips` bucket. Only processed WebP
objects can be copied to `factory-public`.

`trips/export.json` is the commit point. Consumers read its `files` mapping
to immutable JSON objects, so a failed export leaves the previous manifest
usable even if some aliases have changed. It also records `asOf`, the daily
`refreshSeconds`, `images`, `photoCount`, `photoAssetCount`, `photoBytes`,
`photoMaxBytes`, `photoMode` and `photoBase`.

`photoMode` is `snapshot` when the five variants total at most 10,000 files
and 1 GiB, and each file is at most 25 MiB. This reserves half the Workers
asset limit for pages and other sections. Larger exports use `public` mode:
the exporter copies the derivatives to `factory-public` and sets `photoBase`
to `https://factory-data.jomcgi.dev/trips/photos`. Snapshot mode sets
`photoBase` to `/data/trips/photos` for the future site consumer. The actual
photo count and selected mode come from the completed export manifest.

The job uses the shared R2 writer and its own imgproxy signing Secret in the
workflow namespace, sourced from the existing 1Password item. Its Bazel
package boundary keeps the exporter out of the public backend. The site
reader can use the existing private `stars` bucket access without access to
originals or signing keys.

`export_test.py` covers output paths, exact API data, incremental rendering,
metadata rejection, publication limits and failure before the manifest write.
`../../chart/trips_export_job_test.py` checks the schedule, resource limits
and secret wiring.
