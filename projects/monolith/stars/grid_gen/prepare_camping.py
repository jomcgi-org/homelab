"""Prepare producer-only boundaries. Run manually, never on a request path.

Requires pyshp 2.3.1, pyproj 3.7.2 and shapely 2.1.2. The input archives
come from the publisher's ATOM distribution; no geometry is simplified.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

import shapefile
from shapely import make_valid
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

ZONE = "https://services5.arcgis.com/bB6hy3TK2FRc3AB0/arcgis/rest/services/LLTNP_Camping_byelaws_management_zones_2017/FeatureServer/0"
PARKS = {
    "Loch Lomond and The Trossachs": "SG_LochLomondTrossachsNationalPark_2002",
    "Cairngorms": "SG_CairngormsNationalPark_2010",
}


def prepare():
    sources = []

    def fetch(url):
        with urlopen(url, timeout=60) as response:
            body = response.read()
        sources.append({"url": url, "sha256": hashlib.sha256(body).hexdigest()})
        return body

    metadata = json.loads(fetch(ZONE + "?f=json"))
    assert metadata["extent"]["spatialReference"]["wkid"] == 27700
    # ArcGIS performs the declared BNG to WGS84 transformation for this layer.
    zones = json.loads(
        fetch(ZONE + "/query?where=1%3D1&outFields=Zone_name&outSR=4326&f=geojson")
    )
    assert len(zones["features"]) == 4 and not zones.get("exceededTransferLimit")
    features = []
    for f in zones["features"]:
        features.append(
            {
                "kind": "zone",
                "name": f["properties"]["Zone_name"],
                "geometry": f["geometry"],
            }
        )
    for name, stem in PARKS.items():
        archive = ZipFile(
            io.BytesIO(fetch(f"https://maps.gov.scot/ATOM/shapefiles/{stem}.zip"))
        )
        crs = CRS.from_wkt(archive.read(stem + ".prj").decode())
        assert crs.to_epsg() == 27700
        convert = Transformer.from_crs(crs, 4326, always_xy=True)
        reader = shapefile.Reader(
            shp=io.BytesIO(archive.read(stem + ".shp")),
            dbf=io.BytesIO(archive.read(stem + ".dbf")),
        )
        for polygon in reader.shapes():
            geometry = transform(convert.transform, shape(polygon.__geo_interface__))
            features.append(
                {"kind": "park", "name": name, "geometry": mapping(geometry)}
            )
    for f in features:
        geometry = shape(f["geometry"])
        if not geometry.is_valid:
            # Publisher rings contain self-intersections. GEOS makes their
            # topology explicit without dropping vertices or simplifying.
            geometry = make_valid(geometry)
            f["geometry"] = mapping(geometry)
            f["preparation"] = "GEOS make_valid"
        assert geometry.is_valid and not geometry.is_empty, f["name"]
    output = Path(__file__).parents[2] / "shared" / "camping_boundaries.json.gz"
    body = json.dumps(
        {"crs": "EPSG:4326", "sources": sources, "features": features},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    output.write_bytes(gzip.compress(body, mtime=0))
    print(output, hashlib.sha256(output.read_bytes()).hexdigest())
    print(json.dumps(sources, indent=2))


if __name__ == "__main__":
    prepare()
