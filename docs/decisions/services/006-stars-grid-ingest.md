# ADR 006: Stars grid ingest via a dedicated job writing to the monolith DB

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-06-13

---

## Problem

The monolith `stars` domain currently plots a hand-curated list of ~30 Scottish
dark-sky destinations (`projects/monolith/stars/seed.py`). That list is useful
(named, drivable spots) but it is sparse: it cannot answer "is it dark and clear
near me right now" for an arbitrary location, and it under-represents large
swathes of the country.

The original `stargazer` pipeline produced a dense grid of ~500 sample points by
combining the DJ Lorenz light-pollution atlas, OSM road data, and a DEM, then
filtering to genuinely dark, reachable locations. That grid lived only in the
standalone `stargazer` service. When the dynamic half of stargazer (weather
fetch plus astronomy scoring) was absorbed into the monolith and the rest was
slated for retirement, the grid was dropped: porting its geospatial stack
(`rasterio`/GDAL, `geopandas`, `osmium`) into the monolith API image would
balloon the image and bring 2Gi+ memory spikes into the request-serving pod,
which is unacceptable for a latency-sensitive API.

We want the grid's coverage back without paying that cost in the API pod, and
without resurrecting the whole standalone pipeline as a runtime dependency.

---

## Decision

Reintroduce the grid as **data ingested into the monolith Postgres by a
dedicated job that is separate from the API pod**. The monolith remains the
source of truth: a small `stars` sites table holds the grid points, and the
existing in-monolith weather-refresh job scores them into `stars.site_hours`
exactly as it scores the curated seed today (subject to the adaptive-darkness
threshold work).

The heavy geospatial computation (light-pollution atlas plus roads plus DEM into
a filtered point set) is **near-static**: light pollution and road networks
change on the order of months to years, not nightly. So it does not belong on
the hot weather-refresh path. It runs in its own Bazel-built image, on its own
infrequent cadence, and its only output is a few hundred rows written to the
monolith DB. The API pod never imports a geospatial dependency.

| Aspect                                            | Today                                | Decided                                                     |
| ------------------------------------------------- | ------------------------------------ | ----------------------------------------------------------- |
| Site source                                       | ~30 curated points in `seed.py`      | ~500 grid points in `stars.sites` (DB)                      |
| Coverage                                          | Named destinations                   | Dense Scotland-wide grid                                    |
| Geospatial deps (`rasterio`/`osmium`/`geopandas`) | Not present anywhere in the monolith | Only in the separate ingest job image                       |
| Who computes the grid                             | N/A (static list)                    | Dedicated job, infrequent (grid is near-static)             |
| Who scores weather                                | In-monolith refresh job (every 3h)   | Unchanged: in-monolith refresh job                          |
| Source of truth                                   | Monolith Postgres                    | Monolith Postgres (unchanged)                               |
| Map rendering                                     | All ~30 points at once               | Viewport-scoped loading (grid is too dense for one payload) |

---

## Architecture

```mermaid
graph LR
    subgraph offline["Grid ingest job (separate image, infrequent)"]
        LP[LP atlas + OSM roads + DEM] --> GRID[Filter to dark, reachable points]
    end
    GRID -->|write ~500 rows| SITES[(stars.sites)]
    subgraph api["Monolith API pod (light, no geospatial deps)"]
        REFRESH[weather-refresh job every 3h] -->|read sites| SITES
        REFRESH -->|fetch + score met.no| HOURS[(stars.site_hours)]
        PRUNE[hourly prune] --> HOURS
        EP[/api/stars/sites?bbox=/] -->|read future hours| HOURS
    end
    EP --> FE[/app/stars map: viewport-scoped]
```

The ingest job writes site metadata (id, lat, lon, light-pollution zone, and any
reachability attributes); the refresh job joins that against met.no forecasts and
writes scored future hours; the read endpoint serves viewport-scoped slices to
the map.

---

## Alternatives Considered

- **Keep the curated ~30 sites only.** Rejected: the coverage gap is the whole
  reason for this ADR. (It remains a fine fallback if the grid never ships.)
- **Port the geospatial stack into the monolith API image.** Rejected: image
  bloat and 2Gi+ memory spikes do not belong in the request-serving pod. This is
  the exact cost the original absorption avoided.
- **Hybrid: pull ranked output from a still-running standalone stargazer over
  the cluster network.** Rejected (also rejected when the domain was first built):
  it keeps the entire standalone pipeline, its CronJob, PVC, and NGINX alive as a
  permanent cross-namespace runtime dependency, the opposite of retiring it.
- **Expand the curated seed list to ~150 sites by hand.** Not the endgame (still
  manual, still not a true grid), but a low-effort interim that needs no new
  infrastructure if the job slips.

---

## Security

Baseline per `docs/security.md`. The ingest job runs non-root (uid 65532), needs
write credentials to the monolith database (via the 1Password operator, never
hardcoded), and reads only public datasets (the light-pollution atlas and OSM).
It adds no new external exposure: its sole output is rows in the existing
Postgres, reached over the in-cluster network. Cross-namespace DB access, if the
job runs outside the monolith namespace, must follow the Linkerd-meshed policy
rules (no raw Kubernetes NetworkPolicies in meshed namespaces).

---

## Risks

| Risk                                                                                                   | Likelihood | Impact | Mitigation                                                                                                             |
| ------------------------------------------------------------------------------------------------------ | ---------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| ~500 points multiplies met.no fetch volume on the refresh job                                          | High       | Medium | The refresh already rate-limits (~15/s under met.no's 20/s cap); ~500 points is ~33s per 3h run, well within budget    |
| Serving all grid points in one payload bloats the page                                                 | High       | Medium | Viewport-scoped loading on the read endpoint (bbox query), mirroring the ships global-feed roadmap                     |
| Geospatial job image is large and slow to build                                                        | Medium     | Low    | Separate image built infrequently; not on the API build/deploy hot path                                                |
| Static grid goes stale as light pollution shifts                                                       | Low        | Low    | Re-run the job on dataset updates; cadence measured in months                                                          |
| Migration-as-data: a bulk seed in the chart migrations ConfigMap would blow the 256 KiB annotation cap | Medium     | High   | The grid is loaded out-of-band by the job, never embedded in `chart/migrations/*.sql` (see the monolith anti-patterns) |

---

## Open Questions

1. Grid resolution and the light-pollution-zone cutoff (how dark is "dark
   enough" to include a point), and whether to also gate on reachability (road
   proximity) as the original pipeline did.
2. Table shape: a new `stars.sites` table joined to `stars.site_hours`, versus
   folding site metadata into the hours rows. (The current domain keeps metadata
   in `seed.py`; the grid forces it into the DB.)
3. Whether to retain the curated ~30 as a "featured / named" subset layered over
   the grid, or replace them entirely.
4. Job cadence and trigger: a low-frequency CronJob versus a one-shot plus manual
   re-run on dataset refresh.
5. The viewport read API contract (bbox parameters, max points per response,
   clustering/zoom behaviour) and how it interacts with the CDN cache key.

---

## References

| Resource                                                  | Relevance                                                     |
| --------------------------------------------------------- | ------------------------------------------------------------- |
| `docs/decisions/platform/002-cdn-cached-data-fetching.md` | The read-endpoint caching model the viewport API must fit     |
| `projects/monolith/stars/seed.py`                         | The curated list this grid supersedes/augments                |
| `projects/stargazer/backend/`                             | The original geospatial grid computation to lift into the job |
| Stars-into-monolith design                                | The self-contained stars design this extends                  |
