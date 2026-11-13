# Nearby place names

Joe selected OS Open Names (OGL) on 2026-11-12 in
[#7265](https://github.com/jomcgi-org/homelab/issues/7265#issuecomment-6838254096).
Naming is staged off. Source provisioning and activation need separate authority.

## Source contract

Primary OS documentation checked on 2026-11-13:

- [Supply formats](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/os-open-names-technical-specification/supply-formats-overview): CSV, GML 3.2.1 and GeoPackage.
- [CSV processing](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/os-open-names-getting-started-guide/csv-file-processing): header in `Doc/OS_Open_Names_Header.csv`, separate headerless tiles in `Data/`. Combine the header once with the required tiles. Each tile starts with a UTF-8 byte-order mark (BOM), which remains mid-stream when concatenated. The parser removes a leading BOM from each selected row's ID before duplicate detection and lexical tie ordering. Include adjacent tiles out to 20 km when preparing a regional extract. Completeness of that extract remains a provisioning responsibility.
- [NamedPlace attributes](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/os-open-names-technical-specification/feature-type/namedplace): persistent `ID`, official spellings `NAME1`/`NAME2`, `TYPE`, `LOCAL_TYPE`, and point geometry `GEOMETRY_X`/`GEOMETRY_Y` in British National Grid, metre resolution.
- [Local types](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/os-open-names-technical-specification/local-type): settlement classes include City, Town, Village, Hamlet, Other Settlement and Suburban Area. Association selects `TYPE=populatedPlace`; roads, postcodes and landforms are excluded.
- [October 2026 release](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/os-open-names/release-notes/october-2026): 3,051,396 features; next release January 2027.
- [OS attribution](https://www.ordnancesurvey.co.uk/business-government/licensing-agreements/copyright-acknowledgements) and [OGL v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/): copying, adaptation and commercial use are permitted with source acknowledgement and a licence link; third-party rights and endorsement are excluded.

The official [download catalogue](https://api.os.uk/downloads/v1/products/OpenNames/downloads)
returned `opname_csv_gb.zip`, 103,312,081 bytes, MD5
`bba5aa44ba375d39de60584099e335cd`. Its licence gives:

> Contains OS data © Crown Copyright and database rights 2026.
>
> Contains Royal Mail data © Royal Mail copyright and database right 2026.
>
> Contains National Statistics data © Crown copyright and database right 2026

The two-record [fixture](tests/open_names_sample.csv) retains the official header
and Glenmore and Aviemore field values from `Data/NH80.csv` (482 records), with
line endings normalized to LF. Glenmore retains the tile's leading BOM after
the header to exercise concatenated input.
Aviemore is `osgb4000000074554515`, `populatedPlace`, `Town`, at
289568 E, 812693 N. Glenmore is `osgb4000000074560085`, `Hamlet`, at
297964 E, 809558 N. The full licence acknowledgements above cover this fixture.
Published associations use settlement names and geometry only, with the OS
acknowledgement and OGL link in response provenance. They publish no postcode
or administrative statistics attributes.

## Producer configuration

`stars-refresh` reaches `materialize._materialize_sync` through the existing
job handler. `STARS_OPEN_NAMES_CSV` is an optional prepared local CSV path;
empty or whitespace disables enrichment. `STARS_OPEN_NAMES_RELEASE` identifies
its quarterly release as `YYYY-MM`. Helm exposes `stars.openNames.csvPath` and
`stars.openNames.release`, both empty in the chart and deployment overlays.
The jobs image includes the pinned `pyproj==3.7.2` dependency. Provisioning the
local file or a read-only mount is separate work; this change downloads nothing
at runtime and supplies no dataset, mount, bucket key or credential.

The materializer streams CSV rows and retains only named populated places in
20 km grid cells. Each site searches nine cells. Distance is planar British
National Grid distance to the feature's representative point, bounded at
20,000 metres before rounding to 0.001 km. Exact ties use the lexical OS ID,
then name. `NAME1` is used as supplied, with `NAME2` only if it is blank. No
language preference or translation is inferred. Remote sites remain unmatched.

WGS84 site coordinates use an explicit offline PROJ pipeline: inverted EPSG
operation 1314 (seven-parameter OSGB36/WGS84 Helmert), then EPSG:27700's Airy
1830 transverse Mercator. It makes no grid downloads and has metre-scale
uncertainty; rounding is serialization precision. This is sufficient for a
nearby-place label, including the documented distance boundary uncertainty.
Original site coordinates and IDs remain unchanged. No name enters ranking or
cache keys; changed snapshot bytes retain the existing ETag isolation.

`nearby_place` carries `Near <name>`, the source release, OS feature ID,
distance/method and attribution. The association describes proximity to a
populated-place point. It promises no parking, access, permission or observing
suitability. Disabled and unmatched associations serialize as null. The
connector accepts older snapshots with the field absent, and rejects malformed
new associations before replacing its last-good dataset. Plan/check and light
return the same association through HTTP and MCP.

Configured missing/unreadable files, invalid release/header, malformed rows,
invalid selected coordinates, duplicate selected IDs or an input with no named
populated places abort before any publication. Existing objects remain available;
there is no silent fallback to an unnamed replacement. Unmatched sites in a
valid input are nullable. Publication keeps the existing object-level storage
failure behavior, including separate connector-publication errors. Disabling
naming deliberately publishes null on the next otherwise successful refresh.
There are no database writes or migrations in enrichment. The grid replacement
job and #7246's uncapped snapshot switch remain unchanged and suspended/off.

## Remaining acceptance

Default-off rollout cannot establish populated names in production. Full named-site
acceptance remains on #7265 after separately authorized source provisioning and
activation. The landing/operations owner retains these checks; this worker runs
no operational commands:

- Verify the scotland-connector deployment for the chart revision containing the delivered commit.
- Verify the monolith deployment for the chart revision containing this producer change.
- Read deployed configuration: naming remains disabled and stars-grid-compute remains suspended.

## Bounded resource check

The existing `stars.tests.snapshot_fixture` benchmark ran on Linux with 14,000
sites, `--no-tracemalloc`, and the uncapped connector switch disabled. A prepared
local extract retained all 43,274 populated-place rows from the verified archive.
The original bulk archive/extract are transient files outside the checkout.

| Naming input | sites.json bytes | Process maximum RSS |
| --- | ---: | ---: |
| Disabled | 29,442,112 | 416.7 MiB |
| Prepared populated places | 33,894,112 | 441.2 MiB |

The map and history-map bytes have identical SHA-256 digests. The named snapshot
also passed the existing `BenchmarkSnapshotRefresh`, one replacement with the
previous dataset retained, `GOMEMLIMIT=220MiB`: 0.692 seconds, 155.4 MiB peak heap
in use and 167.3 MiB peak Go-managed memory. These synthetic measurements omit
real database/storage drivers, HTTP/TLS, walks and populated climatology. They
establish repository-path bounds rather than production capacity acceptance.
