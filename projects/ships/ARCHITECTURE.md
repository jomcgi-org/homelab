# Ships on the factory cluster

HOME-256 follows [Joe's decision of 2026-11-18](https://factory-plan.jomcgi.dev/factory/issue/HOME-178#comment-2358): Ships moves factory-native, and its data leaves the cluster through the cluster to R2 sink. A live app may get its own subdomain. This plan keeps the Pacific Northwest subscription and the MapLibre page at `jomcgi.dev/app/ships` as a section of the Astryx site. Its only runtime dependency will be reads of precomputed objects. A separate application origin would add routing and break existing shared map links without isolating any server workload.

## Ingest and capacity

Run one Go process on factory. A typed WebSocket decoder and bounded channels let the reader and batched PostgreSQL writer overlap without allocating an unbounded backlog. Go fits the repository's existing build and image pipeline. Back off on disconnect, count dropped or malformed reports, and record the last successful commit time. Use the hub's subscription box and message filters. Guard the subscription with a PostgreSQL advisory lock during rolling updates.

Keep PostgreSQL. Track history and all-time heat need durable state across reconnects, deployments and partition expiry. Port the hub's deduplication, latest-position and vessel metadata upserts. Keep daily position partitions for seven days. Bank each expired day's distinct vessel counts by cell and drop its partition in one transaction. Import historical heat separately from live partitions and reconcile their UTC day boundary before enabling retention. The hub implementation defines the parity target. [Ingest and serving code](../monolith/ships/)

HOME-256 reports a roughly 2.9 MB snapshot every 120 seconds. That measures page payload.

Hub resource use, measured in Honeycomb (`metrics` dataset, hub containers) for the 24 hours to 2026-11-18 22:36 UTC:

| Hub container | CPU p50 / p95 / peak (cores) | Working set p95 / peak |
| --- | --- | --- |
| monolith `backend` (ingest shares it with other modules) | 0.26 / 0.61 / 0.98 | 544 MiB / 626 MiB |
| monolith `postgres` (all monolith databases) | 0.33 / 1.04 / 1.46 | 897 MiB / 956 MiB |

These are shared-container totals, so they bound the ingest cost from above. The hub emits no ingest telemetry, and the message rate and retained rows need hub database access. [HOME-409](https://factory-plan.jomcgi.dev/factory/issue/HOME-409) measures them before it sets production budgets ([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-256#comment-4186)). Start the Go pod's shadow run with requests of 250m CPU and 256 MiB and limits of 1 CPU and 512 MiB. These are trial allocations for the Go process alone.

Collect a 24-hour hub baseline before sizing production. Count received WebSocket frames per minute before parsing and report median and peak messages/s. At the same UTC boundary count rows in ships.positions and record total partition and index bytes. Sample container CPU usage rate and working-set bytes each minute, recording p95 and peak. The hub container shares other modules, so profile the ingest task and compare a replay of the same traffic in an isolated process. Record window, source, shared-container totals and isolated measurements alongside the final requests. Size database storage for the observed seven-day footprint, indexes, WAL and a partition rollover. Verify memory, write lag and CPU headroom while heat and export run concurrently.

## Publication contract

AISStream.io publishes no terms of service or data license (checked 2026-11-18). Its site links only a [privacy policy](https://aisstream.io/privacypolicy), which says nothing about redistribution. Its [documentation](https://aisstream.io/documentation#limits) sets only operational limits, and tells apps to serve clients through their own server with only the data each client needs. So no published grant covers public position files. With no terms to link, this plan links the pages checked ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-256#comment-4186)). An inquiry in AISStream’s issue tracker asks specifically about public redistribution. Another reports that the usual terms URLs returned 404. Both are requests from users of the service. Neither supplies a license. [Redistribution inquiry](https://github.com/aisstream/issues/issues/223) · [Terms lookup report](https://github.com/aisstream/issues/issues/299)

Keep all three products in private Garage storage until the applicable terms or written permission covers the intended redistribution. Serve private data only to authorized readers through an authenticated endpoint with private, no-store caching. Keep the public static page in an unavailable state in that mode. Do not assume aggregation or delay grants redistribution rights. [AISStream](https://aisstream.io/)

If cleared, copy only the approved prefix through the HOME-137 Garage-to-R2 mirror. The following sizes are uncompressed JSON planning targets, except for the reported snapshot size. Measure actual byte distributions before cutover.

| Key under ships/v1/ | Contents and size | Publish interval | Public Cache-Control |
| --- | --- | --- | --- |
| snapshot.json | Latest position and metadata per vessel, roughly 2.9 MB | 120 seconds | public, max-age=0, s-maxage=120 |
| heat.json | Occupied cells, live plus banked vessel-days and legend stops, target 1 MB | Hourly after rollup | public, max-age=300, s-maxage=3600 |
| tracks/{mmsi}.json | Latest 1,000 retained points in map order, target 150 KB per vessel | Changed tracks every 120 seconds | public, max-age=0, s-maxage=120 |

Public rollout requires a change to the HOME-137 mirror. Its [upload template](../shared/helm/factory-library/chart/templates/_mirror.tpl) currently gives every payload except the root manifest a one-year immutable lifetime. Add opt-in per-key header rules for the three mutable Ships keys. Before routing readers to R2, replace a fixture at each kind of key, read it through the edge with an existing browser cache, and verify both the table’s Cache-Control value and that the replacement becomes visible within its TTL.

Replace each key with a complete object and include a generation timestamp. Publish changed tracks before the snapshot, retry failed copies with the same body, and propagate expiry of tracks that age out even when no new reports arrive. Verify headers and ETags at the public edge. Keep bucket listing disabled. The browser fetches the snapshot on load and revalidates every two minutes, fetches heat on entering heat mode and one track on marker selection. Preserve map state and display the last successful timestamp on a failed refresh. A missing track leaves the selected vessel visible. Allow reads from the site origin if the mirror uses a separate hostname.

## Follow-up HOME issues

The slices are filed under [HOME-178](https://factory-plan.jomcgi.dev/factory/issue/HOME-178) in dependency order. Each issue carries the numbered Done when criteria below ([record](https://factory-plan.jomcgi.dev/factory/issue/HOME-256#comment-4186)).

### Factory AIS ingester

Tracked in [HOME-409](https://factory-plan.jomcgi.dev/factory/issue/HOME-409).

Done when

1. Record the measured hub baseline above and choose production pod and database budgets from a comparable replay.
2. Demonstrate snapshot, track and heat parity on shared input including duplicates, out-of-order reports and a UTC partition rollover.
3. Restart during a write and confirm bounded memory, one active socket, durable history and recovery of the latest position.

### Garage sink and R2 mirror

Tracked in [HOME-410](https://factory-plan.jomcgi.dev/factory/issue/HOME-410).

Done when

1. Record the dated terms permalink and redistribution grant or implement the private delivery path. Verify anonymous access matches that decision for every product.
2. Measure bytes and object writes per cycle and verify schema, timestamps, freshness, headers and expiry at the reader endpoint.
3. Interrupt a copy and verify readers retain complete objects and the mirror catches up after recovery.

### Ships page

Tracked in [HOME-411](https://factory-plan.jomcgi.dev/factory/issue/HOME-411).

Done when

1. Serve the existing map URL from the site and preserve shared heat and vessel selections.
2. Verify snapshot polling, lazy heat and track reads, stale timestamps and missing tracks in a browser against the sink.
3. Exercise the selected access mode and confirm the public build contains no provider or storage credentials.

### Cutover

Tracked in [HOME-412](https://factory-plan.jomcgi.dev/factory/issue/HOME-412).

Done when

1. Compare a day of hub and factory output including vessel coverage, track chronology, banked heat and freshness. Resolve discrepancies before switching.
2. Route the Ships paths to the site, exercise old links and verify rollback to the hub before retiring its feed.
3. Observe one retention rollover and mirror recovery on factory and record route ownership for HOME-261.

### Hub code removal

Tracked in [HOME-413](https://factory-plan.jomcgi.dev/factory/issue/HOME-413).

Done when

1. After cutover acceptance, archive required history and verify a restore including historical heat.
2. Remove hub subscription, credentials references, partition jobs, routes and superseded frontend code. Retire hub tables after restore verification and the agreed rollback window.
3. Verify factory freshness with the hub disabled and run the affected monolith and site checks.

## Factory shadow deployment

| Component | Responsibility |
| --- | --- |
| cmd | Holds the database session lock, reads one bounded frame at a time, writes vessel history and refreshes heat. |

Set `database.storage` and `database.resources` from the baseline to provision the three-instance factory database. CNPG initializes its Ships tables as the application owner. The ingester uses the database owner connection from the CNPG `ships-db-app` Secret in the ships namespace. Each accepted report updates history and latest in a single transaction. The chart starts at zero replicas. Enable the shadow pod after the database and feed credential exist. Enable retention separately after importing and reconciling historical heat at a UTC boundary.

The hub logs received frame counts keyed by Unix minute. For 1,440 complete minutes, sum counts across pod restarts by minute, include zero-count minutes, then divide each count by 60 before calculating the median and maximum. Check log coverage before interpreting missing buckets. Run `baseline.sql` on the hub at the closing boundary. Capture the same traffic for isolated replay and record CPU and working set before choosing production allocations.

The synthetic restart fixture in `verify_replay.py` locks the history table, sends a backlog and kills the writer during its insert. A second process acquires the socket, the fixture resends the uncommitted report, and both databases are read through the hub's snapshot, track and heat functions. On 2026-11-19 the isolated Go writer averaged 0.0147 cores from startup through the blocked write and reached 25,820 KiB sampled RSS during the stall (PostgreSQL 17, Go 1.24.13, WebSocket library v1.8.11). The fixture covers two UTC partitions and five input frames before the backlog. Production sizing still needs a replay at the measured hub rate.
