"""Offline DoBIH standard CSV reader. Never downloads or imports live records.

Publisher licence verified 2026-11-10, #7368 comment 6760868704.
The caller supplies a separately authorized local release and its version.
"""

import csv
import math
import re
from typing import TextIO

SOURCE_URL = "https://www.hill-bagging.co.uk/dobih/"
LICENSE = "https://creativecommons.org/licenses/by/4.0/"
MODIFICATIONS = (
    "Filtered to Scotland and supported hill classes; joined to Walkhighlands walks."
)
CLASSES = {
    "M": "Munro",
    "MT": "Munro Top",
    "C": "Corbett",
    "G": "Graham",
    "D": "Donald",
}


def build_hills(source: TextIO, version: str) -> dict:
    """Validate the whole local input before publishing any replacement."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
        raise ValueError("an explicit DoBIH release version is required")
    reader = csv.DictReader(source, skipinitialspace=True)
    required = {
        "Number",
        "Name",
        "Country",
        "Classification",
        "Metres",
        "Latitude",
        "Longitude",
    }
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("missing standard DoBIH CSV fields")
    hills, seen = [], set()
    for row in reader:
        # Country is authoritative. Do not substitute the connector bbox,
        # which also overlaps northern England and international waters.
        if row["Country"].strip() != "S":
            continue
        codes = set(row["Classification"].split(","))
        classes = [name for code, name in CLASSES.items() if code in codes]
        if not classes:
            continue
        number = int(row["Number"])
        name = row["Name"].strip()
        height, lat, lon = (
            float(row[key]) for key in ("Metres", "Latitude", "Longitude")
        )
        if (
            number <= 0
            or number in seen
            or not name
            or not all(math.isfinite(v) for v in (height, lat, lon))
            or not 0 < height < 2000
            or not 54.5 <= lat <= 61
            or not -9 <= lon <= -0.5
        ):
            raise ValueError("invalid or duplicate Scottish hill")
        seen.add(number)
        hills.append(
            dict(
                number=number,
                name=name,
                classes=classes,
                lat=lat,
                lon=lon,
                height_m=height,
            )
        )
    if not hills:
        raise ValueError("no supported Scottish hills")
    return dict(
        schema_version=1,
        source=dict(
            name="DoBIH",
            version=version,
            attribution=f"The Database of British and Irish Hills v{version}",
            url=SOURCE_URL,
            license=LICENSE,
            modifications=MODIFICATIONS,
        ),
        hills=sorted(hills, key=lambda hill: hill["number"]),
    )
