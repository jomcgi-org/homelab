# SRWR representative records

`srwr-2026-11-13.csv` contains seven complete, unmodified records from
`CurrentActivities.csv` in the public daily ZIP downloaded on 2026-11-13.
Source: https://downloads.srwr.scot/disruptions-export/daily/
ZIP Last-Modified: `2026-11-13T01:08:05Z`.
ZIP SHA-256: `f4c3e97149a84292dda95ac0e19ecc9f8f0a0b271915a7fdbd905c8b57734c12`.
The export had 22,170 current rows, UTF-8, CRLF and 40 columns.

Contains Scottish Road Works Commissioner information licensed under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
The Commissioner's [licence statement](https://roadworks.scot/opendata) and
[Disruptions Data Extract v1.1 specification, 2026-08-17](https://downloads.srwr.scot/disruptions-export/specification/)
were checked on 2026-11-13. Daily URLs redirect to short-lived SAS downloads;
only the stable public URLs belong in provenance.

To reproduce the subset from that dated export, read the exact member with
`zipfile.ZipFile.open("CurrentActivities.csv")`, decode `utf-8-sig`, raise
`csv.field_size_limit(4 << 20)` and select these `ActivityReference` values
in this order with `csv.DictReader` and `csv.DictWriter` using the original
header order:

- `XQ001-NW/2026/45556-1`: in-progress A82 night closure and amnesties.
- `XQ001-NW/2025/43186`: explicit A82 convoy.
- `XQ001-NW/2023/24801`: proposed A82 night closure.
- `BT067-1463228`: equal start/end timestamps for a single-day window.
- `PK001-S24936`: 09:00 start and 00:00 end on the same civil date.
- First row whose Street starts with `A83` and whose TrafficManagement is
  `Portable Traffic Lights (TTLS)`, excluded from the snapshot.
- First `Road Closure` on Street `Clifton Road`, excluded from the snapshot.

`stars_roadworks_job_test` zips the CSV in memory and compares the
Python output as JSON to `scotland-connector/internal/fixture/testdata/roadworks.json`
using collection time `2026-11-13T03:00:00Z` and the source clock above. Go
parses that same snapshot. Transport tests explicitly adapt coordinates and
clocks to the existing synthetic October walk/weather fixture.

The upstream retains seven daily ZIPs. This attributable subset remains the
reproducible format evidence after the original download expires. No signed
URL or temporary token is retained.
