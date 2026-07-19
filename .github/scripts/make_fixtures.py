#!/usr/bin/env python3
"""Generate the synthetic datasets used by the self-test workflow.

Kept separate from the pytest fixtures because the workflow needs files on disk
in the runner's workspace before the Docker action starts, and because the
self-test must exercise the packaged Action rather than the Python API.

Usage::

    python .github/scripts/make_fixtures.py <scenario> <directory>
    python .github/scripts/make_fixtures.py assert-crs <directory> <EPSG:code>

Scenarios:
    clean    Two datasets, both in EPSG:4326.
    mixed    One dataset in EPSG:4326 and one in EPSG:3857.
    missing  One valid dataset and one carrying no CRS at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS as RioCRS
from shapely.geometry import Point


def write_vector(path: Path, crs: str | None, points: list[tuple[float, float]]) -> None:
    """Write a small point dataset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame(
        {"id": list(range(len(points)))},
        geometry=[Point(x, y) for x, y in points],
        crs=crs,
    )
    frame.to_file(path)


def write_raster(path: Path, crs: str | None, transform: Affine) -> None:
    """Write a small single-band GeoTIFF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": 8,
        "height": 8,
        "count": 1,
        "dtype": "uint8",
        "transform": transform,
    }
    if crs is not None:
        profile["crs"] = RioCRS.from_user_input(crs)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.arange(64, dtype="uint8").reshape(8, 8), 1)


def build(scenario: str, directory: Path) -> None:
    """Create the fixtures for ``scenario`` under ``directory``."""
    geographic = Affine(0.01, 0.0, 1.0, 0.0, -0.01, 51.0)
    mercator = Affine(1000.0, 0.0, 111_319.0, 0.0, -1000.0, 6_621_293.0)

    if scenario == "clean":
        write_vector(directory / "points.gpkg", "EPSG:4326", [(1.0, 51.0), (1.5, 51.5)])
        write_raster(directory / "grid.tif", "EPSG:4326", geographic)
    elif scenario == "mixed":
        write_vector(directory / "points.gpkg", "EPSG:4326", [(1.0, 51.0), (1.5, 51.5)])
        write_raster(directory / "grid.tif", "EPSG:3857", mercator)
    elif scenario == "missing":
        write_vector(directory / "points.gpkg", "EPSG:4326", [(1.0, 51.0)])
        write_vector(directory / "unreferenced.gpkg", None, [(1.0, 51.0)])
    else:
        raise SystemExit(f"Unknown scenario {scenario!r}.")


def assert_crs(directory: Path, expected: str) -> None:
    """Fail unless every dataset under ``directory`` is in ``expected``."""
    checked = 0
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() == ".gpkg":
            actual = gpd.read_file(path).crs
        elif path.suffix.lower() in {".tif", ".tiff"}:
            with rasterio.open(path) as src:
                actual = src.crs
        else:
            continue
        checked += 1
        if actual is None or f"EPSG:{actual.to_epsg()}" != expected:
            raise SystemExit(f"{path} is in {actual}, expected {expected}.")

    if checked == 0:
        raise SystemExit(f"No datasets found under {directory}.")
    print(f"All {checked} dataset(s) under {directory} are in {expected}.")


def main(argv: list[str]) -> int:
    """Dispatch the requested sub-command."""
    if len(argv) < 3:
        raise SystemExit(__doc__)

    command, target = argv[1], Path(argv[2])
    if command == "assert-crs":
        if len(argv) < 4:
            raise SystemExit("assert-crs needs a directory and an expected CRS.")
        assert_crs(target, argv[3])
    else:
        build(command, target)
        print(f"Wrote '{command}' fixtures to {target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
