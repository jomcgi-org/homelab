"""Hand-curated dark-sky locations in Scotland.

Replaces stargazer's geospatial pipeline (LP atlas + OSM roads + DEM → grid
of ~500 sample points) with a curated seed list. The shape matches the
original ``sample_points_enriched.geojson`` rows (id/lat/lon/altitude_m/lp_zone)
plus a human-readable ``name`` so the UI can label results.

Coverage targets the recognised dark-sky destinations: International Dark Sky
Park designations (Galloway Forest, Tomintoul-Glenlivet), Dark Sky Communities
(Coll), and well-known remote viewing spots across the Highlands, Islands,
and Borders. ``lp_zone`` is the DJ Lorenz Light Pollution Atlas band (1a-3a
covers natural-sky-dominant areas; 1a is the darkest).
"""

from typing import TypedDict


class SeedLocation(TypedDict):
    id: str
    name: str
    lat: float
    lon: float
    altitude_m: int
    lp_zone: str


SCOTLAND_DARK_SKY_LOCATIONS: list[SeedLocation] = [
    # Galloway Forest: International Dark Sky Park (2009)
    {
        "id": "galloway-forest",
        "name": "Galloway Forest Park",
        "lat": 55.083,
        "lon": -4.500,
        "altitude_m": 110,
        "lp_zone": "1a",
    },
    {
        "id": "bruces-stone",
        "name": "Bruce's Stone, Loch Trool",
        "lat": 55.083,
        "lon": -4.483,
        "altitude_m": 130,
        "lp_zone": "1a",
    },
    {
        "id": "loch-doon",
        "name": "Loch Doon",
        "lat": 55.244,
        "lon": -4.392,
        "altitude_m": 240,
        "lp_zone": "1b",
    },
    {
        "id": "clatteringshaws",
        "name": "Clatteringshaws Loch",
        "lat": 55.063,
        "lon": -4.290,
        "altitude_m": 200,
        "lp_zone": "1a",
    },
    # Cairngorms / Tomintoul-Glenlivet: International Dark Sky Park (2018)
    {
        "id": "tomintoul",
        "name": "Tomintoul",
        "lat": 57.249,
        "lon": -3.371,
        "altitude_m": 345,
        "lp_zone": "1a",
    },
    {
        "id": "glenlivet",
        "name": "Glenlivet",
        "lat": 57.349,
        "lon": -3.302,
        "altitude_m": 260,
        "lp_zone": "1a",
    },
    {
        "id": "glen-tanar",
        "name": "Glen Tanar, Cairngorms",
        "lat": 57.040,
        "lon": -2.870,
        "altitude_m": 220,
        "lp_zone": "1b",
    },
    {
        "id": "glenmore-forest",
        "name": "Glenmore Forest, Cairngorms",
        "lat": 57.166,
        "lon": -3.690,
        "altitude_m": 320,
        "lp_zone": "1b",
    },
    {
        "id": "loch-morlich",
        "name": "Loch Morlich",
        "lat": 57.166,
        "lon": -3.706,
        "altitude_m": 305,
        "lp_zone": "1b",
    },
    # West Highlands
    {
        "id": "glen-affric",
        "name": "Glen Affric",
        "lat": 57.282,
        "lon": -4.918,
        "altitude_m": 250,
        "lp_zone": "1a",
    },
    {
        "id": "glen-coe",
        "name": "Glen Coe",
        "lat": 56.673,
        "lon": -4.989,
        "altitude_m": 100,
        "lp_zone": "1b",
    },
    {
        "id": "rannoch-moor",
        "name": "Rannoch Moor",
        "lat": 56.626,
        "lon": -4.687,
        "altitude_m": 320,
        "lp_zone": "1a",
    },
    {
        "id": "knoydart",
        "name": "Knoydart",
        "lat": 57.040,
        "lon": -5.700,
        "altitude_m": 50,
        "lp_zone": "1a",
    },
    # Inner Hebrides
    {
        "id": "isle-of-coll",
        "name": "Isle of Coll (Dark Sky Community)",
        "lat": 56.620,
        "lon": -6.550,
        "altitude_m": 30,
        "lp_zone": "1a",
    },
    {
        "id": "tiree",
        "name": "Tiree",
        "lat": 56.504,
        "lon": -6.880,
        "altitude_m": 15,
        "lp_zone": "1a",
    },
    {
        "id": "mull-dervaig",
        "name": "Mull (Dervaig)",
        "lat": 56.589,
        "lon": -6.182,
        "altitude_m": 30,
        "lp_zone": "1a",
    },
    {
        "id": "iona",
        "name": "Iona",
        "lat": 56.330,
        "lon": -6.396,
        "altitude_m": 20,
        "lp_zone": "1a",
    },
    {
        "id": "skye-trotternish",
        "name": "Skye (Trotternish)",
        "lat": 57.580,
        "lon": -6.180,
        "altitude_m": 100,
        "lp_zone": "1a",
    },
    # Outer Hebrides
    {
        "id": "north-uist",
        "name": "North Uist",
        "lat": 57.595,
        "lon": -7.319,
        "altitude_m": 20,
        "lp_zone": "1a",
    },
    {
        "id": "lewis-callanish",
        "name": "Lewis (Callanish)",
        "lat": 58.195,
        "lon": -6.745,
        "altitude_m": 30,
        "lp_zone": "1a",
    },
    {
        "id": "harris-luskentyre",
        "name": "Harris (Luskentyre)",
        "lat": 57.881,
        "lon": -6.945,
        "altitude_m": 10,
        "lp_zone": "1a",
    },
    {
        "id": "barra",
        "name": "Barra",
        "lat": 56.978,
        "lon": -7.477,
        "altitude_m": 15,
        "lp_zone": "1a",
    },
    # Northern Highlands
    {
        "id": "assynt",
        "name": "Assynt",
        "lat": 58.158,
        "lon": -5.066,
        "altitude_m": 130,
        "lp_zone": "1a",
    },
    {
        "id": "cape-wrath",
        "name": "Cape Wrath",
        "lat": 58.552,
        "lon": -4.957,
        "altitude_m": 50,
        "lp_zone": "1a",
    },
    {
        "id": "dunnet-head",
        "name": "Dunnet Head, Caithness",
        "lat": 58.671,
        "lon": -3.376,
        "altitude_m": 100,
        "lp_zone": "1a",
    },
    # Orkney + Shetland
    {
        "id": "orkney-birsay",
        "name": "Orkney (Birsay)",
        "lat": 59.130,
        "lon": -3.279,
        "altitude_m": 25,
        "lp_zone": "1a",
    },
    {
        "id": "orkney-hoy",
        "name": "Orkney (Hoy)",
        "lat": 58.851,
        "lon": -3.297,
        "altitude_m": 60,
        "lp_zone": "1a",
    },
    {
        "id": "shetland-mainland",
        "name": "Shetland (South Mainland)",
        "lat": 60.299,
        "lon": -1.342,
        "altitude_m": 30,
        "lp_zone": "1a",
    },
    {
        "id": "shetland-unst",
        "name": "Unst, Shetland",
        "lat": 60.756,
        "lon": -0.831,
        "altitude_m": 50,
        "lp_zone": "1a",
    },
    # Borders / South-East
    {
        "id": "moffat",
        "name": "Moffat (Dark Sky Town)",
        "lat": 55.331,
        "lon": -3.443,
        "altitude_m": 110,
        "lp_zone": "2a",
    },
]
