# Camping boundary provenance

`camping_boundaries.json.gz` is producer-only WGS84 (EPSG:4326) geometry.
It contains four LLTNP camping-management zones and two National Parks.
Materializers use Shapely `covers`: polygon and hole edges count as inside,
hole interiors stay outside, and multipolygon islands are retained.
Shapely is a declared shared runtime dependency for these point checks;
GeoPandas and Rasterio remain isolated to the offline grid generator. The
grid build-config test enumerates the permitted Shapely consumers.
Missing coordinates produce unknown membership. Missing or unreadable data
aborts publication; it never produces a verified-outside result.

## Primary sources and attribution

Retrieved 1 November 2026 without authentication:

- [LLTNPA spatial record 8902e7ef-4a95-4ef0-bf2a-dc6870960e15](https://spatialdata.gov.scot/geonetwork/srv/api/records/8902e7ef-4a95-4ef0-bf2a-dc6870960e15/formatters/xml)
  identifies the FeatureServer, EPSG:27700 and OGL v3. The
  [publisher's ArcGIS item](https://www.arcgis.com/sharing/rest/content/items/ab53bcc94aab4c10ace5985074ed63ee?f=json)
  says it used OS 1:25,000 colour raster current at 2014. Attribution uses
  that OS source year: Data source LLTNPA. Contains OS data © Crown copyright
  and database right 2014.
- [LLTNP National Park record](https://spatialdata.gov.scot/geonetwork/srv/api/records/21e648e4-d8c7-482d-bb42-985f311f8d19/formatters/xml)
  and [Cairngorms record](https://spatialdata.gov.scot/geonetwork/srv/api/records/af0755a1-2841-4d51-8b16-c95fd921908b/formatters/xml)
  identify the maps.gov.scot ATOM shapefiles and Open Government Licence.
  Both archives' `.prj` files identify EPSG:27700. Their embedded `.shp.xml`
  metadata and export process record are dated 4 December 2013. The filenames
  refer to the 2002 designation and 2010 revision, respectively. Attribution
  uses the documented distribution export year, rather than the retrieval
  year: Copyright Scottish Government, contains Ordnance Survey data © Crown
  copyright and database right 2013.

The licence is [OGL v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
These required statements are carried in connector `meta.sources`. The data
does not establish permission to camp or classify an entire route.

## Reproduce preparation

In a disposable Python environment install `pyshp==2.3.1 pyproj==3.7.2
shapely==2.1.2`, then run:

```sh
python projects/monolith/stars/grid_gen/prepare_camping.py
```

The script downloads the exact endpoints recorded in the packaged resource's
`sources` array with SHA-256 digests. The two ATOM archives are
`https://maps.gov.scot/ATOM/shapefiles/SG_LochLomondTrossachsNationalPark_2002.zip`
and `https://maps.gov.scot/ATOM/shapefiles/SG_CairngormsNationalPark_2010.zip`.
It checks the native CRS and feature counts. ArcGIS converts the zones using
`outSR=4326`; pyproj converts the parks using `always_xy=True`, offline PROJ
datum transformation (metre-scale accuracy, no downloaded grid). Coordinates
are never simplified or rounded. GEOS `make_valid` resolves publisher ring
self-intersections and the affected features record that preparation in the
resource. Tests and runtime never fetch boundaries. Review source digests,
geometry and provenance before accepting a regenerated resource.
