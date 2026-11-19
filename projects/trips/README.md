# Factory Trips

[Architecture](ARCHITECTURE.md)

Bazel builds both Go commands in `//projects/trips/image:image`. The published
chart pins the image digest. Set `workloadsEnabled` to start shadow ingestion
and create the suspended daily export after preparing private storage.

The 03:00 UTC exporter reads approved trip metadata (including highlights and
default photos) and individually approved points in one PostgreSQL statement.
New uploads create no approvals. Only stripped WebP variants and public JSON
enter the Garage claim. The exporter writes latest.json after the payloads.
Allow scratch disk for all missing derivatives. The exporter reuses objects already
in its claim and retains payloads referenced by cached manifests.

## Cutover

1. Size and mount the private `trips-originals` PVC for the source bucket,
   including upload headroom. With hub uploads quiesced, run `migrate.sh` with
   PostgreSQL service names in `HUB_PGSERVICE` and `FACTORY_PGSERVICE`, the
   rclone bucket path in `HUB_ORIGINALS_REMOTE`, and the private mount path in
   `TRIPS_ORIGINALS`. Run as UID 65532 or give that user access after copying.
   The script restores to an empty schema, compares ordered row digests, and
   verifies the private files by downloading source bytes. Keep the source
   available for rollback. Reconcile any later hub writes before cutover.
2. Load the approved slugs and (trip_slug,id) pairs into public_trips and
   public_points. Review highlight/default image references with each slug.
3. Run a one-off export. Verify the manifest's JSON hashes, point counts,
   image references and all five WebP variants against the hub snapshot.
   Provision scoped R2 mirror credentials and enable the trips-public mirror.
   Check root latest.json and payload cache headers at the public endpoint.
4. Replay the same upload corpus against both authenticated routes at equal
   concurrency, including invalid and duplicate uploads. Compare rows, HTTP
   responses, cgroup CPU usage per accepted image and peak memory on fresh pods.
5. After parity, set the edge chart’s `tripsIngestEnabled` and direct only the
   authenticated upload path through the factory tunnel. Check a missing JWT
   is rejected and an authorized replay succeeds. Switch the export reader from
   stars/trips/export.json to trips-public/latest.json, enable the factory
   export schedule (`exportSuspended: false`) and retire the hub handler and schedule.

## Local upload replay

With four clients replaying 64 identical 4 MiB uploads, Go used 33.3 ms of
process CPU per accepted image and reached 22.7 MiB sampled RSS. The hub
FastAPI handler used 57.0 ms/image and reached 112.5 MiB. Both handled the
same geotagged repository JPEG padded after its end marker. CPU is user plus
system time from /proc, and RSS was sampled every 5 ms after warmup. Both
used local PostgreSQL 17 and the same filesystem. The Python S3 write was
replaced with a local write and fsync for this comparison. These figures
exclude the gateway, database process and remote object-store latency.
