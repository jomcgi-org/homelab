# Hikes

The factory writer publishes WalkHighlands route facts and daylight hours from MET Norway. See the [architecture](ARCHITECTURE.md).

## Activation

Provision the dedicated R2 bucket `hikes` and its bucket-scoped mirror credential in `vaults/k8s-homelab/items/hikes-r2-public-mirror`. Grant the site and Scotland connector credentials read access to this bucket. Confirm that Garage has reconciled the writer credential with read and write access to the hikes bucket and the mirror credential with read access.

Before starting the jobs, seed Garage with the current hub schema-v2 `stars/walks.json` so even routes unavailable during the first scrape retain their IDs and last-good forecasts. Copy the exact bytes as `walks-<sha256>.json` with immutable cache control, then create `latest.json` containing `{"walks":"walks-<sha256>.json"}` with a 60-second cache.

Run `kubectl -n hikes create job --from=cronjob/hikes-catalogue hikes-initial-catalogue`. Verify that the catalogue read the seeded snapshot and published a new generation with its route updates. After that check, set `suspendWriters: false`. Set `buckets[0].public.suspend: false` when the R2 bucket and mirror credential are ready. After the first successful refresh, compare walk IDs, metadata, hourly windows and per-walk freshness against the current hub generation. Resolve R2's `latest.json`, verify its 60-second cache policy, SHA-256 payload digest and immutable payload policy. Check for missing routes or fetch failures before proceeding.

Set the site's hikes source in `projects/jomcgi-site/data/sources.json` to bucket `hikes` and manifest `latest.json`. Set the connector chart's `config.walksBucket` and `config.walksKey` to the same values. Exercise the deployed hikes page and connector walk answers. After checking remaining consumers of the hub hikes database, retire the hub scrape, refresh and prune entries. Rollback uses the old reader settings while hub publication is active.
