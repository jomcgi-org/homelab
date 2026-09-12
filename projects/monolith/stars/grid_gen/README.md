# Stars grid computation job

`stars-grid-compute` is the infrequent, dedicated pipeline that computes the
site grid and writes it to `stars.sites`. Its image contains rasterio,
geopandas, Shapely, and their native libraries. The API and routine jobs images
continue to exclude this directory and those dependencies.

Each run downloads four operator-selected objects from one S3-compatible store:

- a Natural Earth admin-1 GeoJSON containing Scotland boundaries
- a Scotland roads GeoJSON
- a georeferenced RGB light-pollution raster
- a georeferenced DEM raster with valid elevation coverage at every retained
  point

The prior stargazer archive is documented as containing the road and
light-pollution products, but the retired service never implemented its DEM.
For that reason no chart value guesses an admin boundary or DEM object. Set the
exact bucket and keys under `stars.gridGenerator.source` in an environment
values file. `credentialsSecretName` defaults to the existing
1Password-backed `monolith-r2-s3` Secret and can be changed only to another
already-managed Secret when the selected store differs. No credentials belong
in values.

The job creates a unique directory under the `emptyDir` mounted at `/work`,
downloads each object there, and deletes the directory after the run. Configure
`temporaryStorage.sizeLimit` and the ephemeral-storage requests and limits for
the selected dataset sizes. `spacingKm` controls mesh density and
`maxRoadDistanceM` controls road accessibility.

The production CronWorkflow is intentionally suspended. After setting and
reviewing all input values, submit a one-off Workflow from the template:

```sh
argo submit --from cronwf/stars-grid-compute -n monolith-workflows
```

Do not unsuspend the quarterly guardrail merely to run once. A source download,
raster sample, computation, or database error exits nonzero and Argo retains
the failed pod. A successful computation replaces `stars.sites` in one database
transaction and removes forecast rows for sites no longer present. Repeating
the same input is safe, and a failed commit leaves the previous grid intact.
The existing `stars-load-grid` command and API behavior are unchanged.
