# Ships on the factory cluster

HOME-256 follows the HOME-178 decision of 2026-11-18. Keep the Pacific Northwest subscription and the MapLibre page at `jomcgi.dev/app/ships` as a section of the Astryx site. Its only runtime dependency will be reads of precomputed objects. A separate application origin would add routing and break existing shared map links without isolating any server workload.

## Ingest and capacity

Run one Go process on factory. A typed WebSocket decoder and bounded channels let the reader and batched PostgreSQL writer overlap without allocating an unbounded backlog. Go fits the repository's existing build and image pipeline. Back off on disconnect, count dropped or malformed reports, and record the last successful commit time. Use the hub's subscription box and message filters. Guard the subscription with a PostgreSQL advisory lock during rolling updates.

Keep PostgreSQL. Track history and all-time heat need durable state across reconnects, deployments and partition expiry. Port the hub's deduplication, latest-position and vessel metadata upserts. Keep daily position partitions for seven days. Bank each expired day's distinct vessel counts by cell and drop its partition in one transaction. Import historical heat separately from live partitions and reconcile their UTC day boundary before enabling retention. The hub implementation defines the parity target. [Ingest and serving code](../monolith/ships/)

HOME-256 reports a roughly 2.9 MB snapshot every 120 seconds. That measures page payload. Hub input messages/s, retained rows, ingest CPU and memory remain unmeasured here. Start the Go pod's shadow run with requests of 250m CPU and 256 MiB and limits of 1 CPU and 512 MiB. These are trial allocations for the Go process alone.

Collect a 24-hour hub baseline before sizing production. Count received WebSocket frames per minute before parsing and report median and peak messages/s. At the same UTC boundary count rows in ships.positions and record total partition and index bytes. Sample container CPU usage rate and working-set bytes each minute, recording p95 and peak. The hub container shares other modules, so profile the ingest task and compare a replay of the same traffic in an isolated process. Record window, source, shared-container totals and isolated measurements alongside the final requests. Size database storage for the observed seven-day footprint, indexes, WAL and a partition rollover. Verify memory, write lag and CPU headroom while heat and export run concurrently.

## Publication contract

Permission to redistribute raw AISStream positions as downloadable public files is unresolved. The provider site could not be retrieved to verify a terms permalink or applicable grant. Keep all three products in private Garage storage until the applicable terms or written permission covers the intended redistribution. Serve private data only to authorized readers through an authenticated endpoint with private, no-store caching. Keep the public static page in an unavailable state in that mode. Do not assume aggregation or delay grants redistribution rights. [AISStream](https://aisstream.io/)

If cleared, copy only the approved prefix through the HOME-137 Garage-to-R2 mirror. The following sizes are uncompressed JSON planning targets, except for the reported snapshot size. Measure actual byte distributions before cutover.

| Key under ships/v1/ | Contents and size | Publish interval | Public Cache-Control |
| --- | --- | --- | --- |
| snapshot.json | Latest position and metadata per vessel, roughly 2.9 MB | 120 seconds | public, max-age=0, s-maxage=120 |
| heat.json | Occupied cells, live plus banked vessel-days and legend stops, target 1 MB | Hourly after rollup | public, max-age=300, s-maxage=3600 |
| tracks/{mmsi}.json | Latest 1,000 retained points in map order, target 150 KB per vessel | Changed tracks every 120 seconds | public, max-age=0, s-maxage=120 |

Public rollout requires a change to the HOME-137 mirror. Its [upload template](../shared/helm/factory-library/chart/templates/_mirror.tpl) currently gives every payload except the root manifest a one-year immutable lifetime. Add opt-in per-key header rules for the three mutable Ships keys. Before routing readers to R2, replace a fixture at each kind of key, read it through the edge with an existing browser cache, and verify both the table’s Cache-Control value and that the replacement becomes visible within its TTL.

Replace each key with a complete object and include a generation timestamp. Publish changed tracks before the snapshot, retry failed copies with the same body, and propagate expiry of tracks that age out even when no new reports arrive. Verify headers and ETags at the public edge. Keep bucket listing disabled. The browser fetches the snapshot on load and revalidates every two minutes, fetches heat on entering heat mode and one track on marker selection. Preserve map state and display the last successful timestamp on a failed refresh. A missing track leaves the selected vessel visible. Allow reads from the site origin if the mirror uses a separate hostname.

## Follow-up HOME issue drafts

### Factory AIS ingester

Done when

1. Record the measured hub baseline above and choose production pod and database budgets from a comparable replay.
2. Demonstrate snapshot, track and heat parity on shared input including duplicates, out-of-order reports and a UTC partition rollover.
3. Restart during a write and confirm bounded memory, one active socket, durable history and recovery of the latest position.

### Garage sink and R2 mirror

Done when

1. Record the dated terms permalink and redistribution grant or implement the private delivery path. Verify anonymous access matches that decision for every product.
2. Measure bytes and object writes per cycle and verify schema, timestamps, freshness, headers and expiry at the reader endpoint.
3. Interrupt a copy and verify readers retain complete objects and the mirror catches up after recovery.

### Ships page

Done when

1. Serve the existing map URL from the site and preserve shared heat and vessel selections.
2. Verify snapshot polling, lazy heat and track reads, stale timestamps and missing tracks in a browser against the sink.
3. Exercise the selected access mode and confirm the public build contains no provider or storage credentials.

### Cutover

Done when

1. Compare a day of hub and factory output including vessel coverage, track chronology, banked heat and freshness. Resolve discrepancies before switching.
2. Route the Ships paths to the site, exercise old links and verify rollback to the hub before retiring its feed.
3. Observe one retention rollover and mirror recovery on factory and record route ownership for HOME-261.

### Hub code removal

Done when

1. After cutover acceptance, archive required history and verify a restore including historical heat.
2. Remove hub subscription, credentials references, partition jobs, routes and superseded frontend code. Retire hub tables after restore verification and the agreed rollback window.
3. Verify factory freshness with the hub disabled and run the affected monolith and site checks.
