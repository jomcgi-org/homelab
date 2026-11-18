# Static trip export

The daily `trips-export` Argo CronWorkflow runs at 03:00 UTC through
`app/jobs_main.py`. It reads the serializers for `/api/trips/trips` and
`/api/trips/trip/{slug}` and exports their JSON responses without changes.
Site ingestion, static pages and route activation are HOME-242.

```text
Postgres ── API serializers ──► private stars/trips/index.json, trip/<slug>.json
                           └─► private stars/trips/v/index-<sha256>.json, v/trip/<slug>-<sha256>.json
private monolith-trips originals ── signed hub imgproxy presets
                           └─► private stars/trips/photos/<original-key>/*.webp
                                     └─ explicit public opt-in: factory-public/trips/photos/…
all referenced objects complete ──► private stars/trips/export.json
```

Each unique photo has five WebP variants: `thumb`, `gallery`, `preview`,
`display` and `full`. The exporter uses the existing hub imgproxy presets,
rejects non-WebP output, EXIF/XMP chunks and files without a `VP8 ` or `VP8L`
frame that Pillow can decode, and skips variants already in R2.
The signer accepts only original keys `img_<name>.jpg`, where `<name>` is 1 to
64 of `A-Z a-z 0-9 _ -`. The hub's ingest names them by the first 12 hex digits
of the SHA-256; the live trip also has one older key, `img_000038.jpg`.
Originals stay in the private `monolith-trips` bucket. Only processed WebP
objects can be copied to `factory-public`.

`trips/export.json` is the commit point. Consumers read its `files` mapping
to immutable JSON objects (`files.index`, and `files.trips` by slug). Trip
aliases live under `trips/trip/`, so no slug can replace the index or the
manifest. The key part is the slug percent-encoded (with `.` and `~` too),
so every slug the API accepts exports, and no two slugs share a key. An
encoding over 200 characters becomes `~` and the slug's SHA-256, which keeps
every key far below R2's 1,024-byte limit. The JSON and `files.trips` keep
the original slug. A failed export leaves the previous manifest usable, even if some
aliases have changed. It also records `asOf`, the daily
`refreshSeconds`, `images`, `photoCount`, `photoAssetCount`, `photoBytes`,
`photoMaxBytes`, `photoMode` and `photoBase`.

`photoMode` says where the site can read the photos. It is `snapshot`, with
`photoBase` `/data/trips/photos`, when the five variants total at most 10,000
files and 1 GiB, and each file is at most 25 MiB. This keeps half the Workers
asset limit for pages and other sections. A larger export still commits its
private JSON and manifest, with `photoMode` `private` and `photoBase` `null`:
the derivatives are only in the private `stars` bucket. The live trip has
4,838 photos (24,190 derivatives), so this is today's mode.

Public copies are off by default, and the scheduled job sets
`TRIPS_EXPORT_PUBLIC_ENABLED` to `false`. Set it to the exact value `true` only
after checking that `factory-public` exists, the job's R2 identity can write to
it, and `https://factory-data.jomcgi.dev` serves a published WebP derivative.
Then a larger export copies the derivatives to `factory-public`, with
`photoMode` `public` and `photoBase`
`https://factory-data.jomcgi.dev/trips/photos`. The photo count and mode come
from the committed manifest.

The job uses the shared R2 writer and its own imgproxy signing Secret in the
workflow namespace, sourced from the existing 1Password item. Its Bazel
package boundary keeps the exporter out of the public backend. The site
reader can use the existing private `stars` bucket access without access to
originals or signing keys.

`export_test.py` covers output paths, exact API data, incremental rendering,
metadata rejection, publication limits and failure before the manifest write.
`../../chart/trips_export_job_test.py` checks the schedule, resource limits
and secret wiring.
