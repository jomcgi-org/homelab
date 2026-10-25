"""Offline GMTED profile preparation. No download, upload or live activation.

Feed the exact bounded site inventory (id/lat/lon) used by aurora_job. The
source tile checksum and each cropped-grid checksum make the profile provenance
reproducible. Input-source vertical datums and coarse-resolution limitations
remain part of the operator's source review, not a claimed sightline guarantee.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path


def prepare(tile: Path, sites: list[dict], expected_sha256: str, binary: str) -> dict:
    import rasterio
    from rasterio.windows import Window, from_bounds

    if not 1 <= len(sites) <= 64 or len({p["id"] for p in sites}) != len(sites):
        raise ValueError("expected 1 to 64 distinct site identities")
    with tile.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError("GMTED tile checksum mismatch")
    profiles = {}
    with rasterio.open(tile) as dataset:
        if (
            dataset.crs.to_epsg() != 4326
            or dataset.nodata is None
            or dataset.transform.b
            or dataset.transform.d
            or not math.isclose(dataset.res[0], 1 / 480)
            or not math.isclose(dataset.res[1], 1 / 480)
        ):
            raise ValueError(
                "expected geographic GMTED 7.5 arc-second grid with nodata"
            )
        for site in sites:
            lat, lon = site["lat"], site["lon"]
            if not (54.5 <= lat <= 61 and -9 <= lon <= -0.5):
                raise ValueError("site outside Scotland")
            # 0.6 latitude and 1.0 longitude degrees cover all 20 km northern
            # rays in Scotland. Boundless reads encode uncovered cells as
            # nodata, so the canonical profile remains unknown there.
            raw_window = from_bounds(
                lon - 0.5, lat - 0.3, lon + 0.5, lat + 0.3, dataset.transform
            )
            window = Window(
                math.floor(raw_window.col_off), math.floor(raw_window.row_off), 482, 290
            )
            data = dataset.read(
                1, window=window, boundless=True, fill_value=dataset.nodata
            )
            transform = dataset.window_transform(window)
            with tempfile.TemporaryDirectory(prefix="aurora-terrain-") as directory:
                folder = Path(directory)
                grid = folder / "crop.asc"
                with grid.open("w") as output:
                    output.write(
                        f"ncols {data.shape[1]}\nnrows {data.shape[0]}\nxllcorner {transform.c:.15f}\nyllcorner {transform.f + data.shape[0] * transform.e:.15f}\ncellsize {transform.a:.15f}\nNODATA_value {dataset.nodata}\n"
                    )
                    for row in data:
                        output.write(" ".join(str(int(value)) for value in row) + "\n")
                inventory = folder / "sites.json"
                inventory.write_text(json.dumps([site]))
                result = subprocess.run(
                    [binary, "--grid", str(grid), "--sites", str(inventory)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                profile = json.loads(result.stdout)[0]
                profiles[site["id"]] = {
                    "lat": lat,
                    "lon": lon,
                    "terrain": profile["terrain"],
                }
    return profiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tile", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sites = json.loads(args.sites.read_text())
    args.output.write_text(
        json.dumps(
            prepare(args.tile, sites, args.sha256, args.binary), separators=(",", ":")
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
