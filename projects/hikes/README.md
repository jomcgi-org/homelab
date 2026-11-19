# Hikes

The factory writer publishes WalkHighlands route facts and daylight hours from MET Norway. See the [architecture](ARCHITECTURE.md).

## Activation

The mirror writes under prefix `hikes/` of R2 bucket `factory-public` with the shared item `vaults/k8s-homelab/items/factory-r2-public-mirror` (HOME-492). Grant the site and Scotland connector credentials read access to `factory-public`. Confirm that Garage has reconciled the writer credential with read and write access to the hikes bucket and the mirror credential with read access.

Before starting the jobs, seed Garage with the current hub schema-v2 `stars/walks.json` so even routes unavailable during the first scrape retain their IDs and last-good forecasts. Copy the exact bytes as `walks-<sha256>.json` with immutable cache control, then create `latest.json` containing `{"walks":"walks-<sha256>.json"}` with a 60-second cache.

Run `kubectl -n hikes create job --from=cronjob/hikes-catalogue hikes-initial-catalogue`. Verify that the catalogue read the seeded snapshot and published a new generation with its route updates. After that check, set `suspendWriters: false`. Set `buckets[0].public.suspend: false` when the mirror credential is ready. After the first successful refresh, compare walk IDs, metadata, hourly windows and per-walk freshness against the current hub generation. Resolve `https://factory-data.jomcgi.dev/hikes/latest.json`, verify its 60-second cache policy, SHA-256 payload digest and immutable payload policy. Check for missing routes or fetch failures before proceeding.

The site snapshot and the connector resolve the manifest's walk key from the bucket root. Change both to resolve it under `hikes/`. Then set the site's hikes source in `projects/jomcgi-site/data/sources.json` to bucket `factory-public` and manifest `hikes/latest.json`. Set the connector chart's `config.walksBucket` and `config.walksKey` to the same values. Exercise the deployed hikes page and connector walk answers. After checking remaining consumers of the hub hikes database, retire the hub scrape, refresh and prune entries. Rollback uses the old reader settings while hub publication is active.
