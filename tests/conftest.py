"""Shared fixtures building small synthetic vector and raster datasets.

Everything is generated on the fly into ``tmp_path`` so the test suite carries
no binary fixtures and each test gets an isolated working tree.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS as RioCRS
from shapely.geometry import Point, box


def write_vector(
    path: Path,
    crs: str | None,
    geometries=None,
) -> Path:
    """Write a tiny vector dataset at ``path`` in ``crs``.

    Args:
        path: Destination file. The driver is inferred from the suffix.
        crs: CRS to declare, or ``None`` to write an unreferenced dataset.
        geometries: Geometries to write. Defaults to two points near the
            origin, which are valid degrees and valid metres alike.

    Returns:
        The path written.
    """
    if geometries is None:
        geometries = [Point(1.0, 51.0), Point(1.5, 51.5)]
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame(
        {"id": list(range(len(geometries)))},
        geometry=list(geometries),
        crs=crs,
    )
    with warnings.catch_warnings():
        # Writing a deliberately unreferenced dataset is the point of several
        # fixtures; GDAL's warning about it is expected noise.
        warnings.simplefilter("ignore", UserWarning)
        frame.to_file(path)
    return path


def write_raster(
    path: Path,
    crs: str | None,
    transform: Affine | None = None,
    width: int = 8,
    height: int = 8,
) -> Path:
    """Write a tiny single-band GeoTIFF at ``path``.

    Args:
        path: Destination file.
        crs: CRS to declare, or ``None`` for an unreferenced raster.
        transform: Geotransform. Defaults to a north-up grid near longitude 1,
            latitude 51 with 0.01-unit pixels.
        width: Raster width in pixels.
        height: Raster height in pixels.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if transform is None:
        transform = Affine(0.01, 0.0, 1.0, 0.0, -0.01, 51.0)
    data = np.arange(width * height, dtype="uint8").reshape(height, width)
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 1,
        "dtype": "uint8",
        "transform": transform,
    }
    if crs is not None:
        profile["crs"] = RioCRS.from_user_input(crs)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def vector_4326(tmp_path: Path) -> Path:
    """A GeoPackage of points in EPSG:4326."""
    return write_vector(tmp_path / "data" / "points_4326.gpkg", "EPSG:4326")


@pytest.fixture
def vector_27700(tmp_path: Path) -> Path:
    """A GeoPackage of points in EPSG:27700, with British National Grid coordinates."""
    return write_vector(
        tmp_path / "data" / "points_27700.gpkg",
        "EPSG:27700",
        geometries=[Point(530000, 180000), Point(531000, 181000)],
    )


@pytest.fixture
def vector_no_crs(tmp_path: Path) -> Path:
    """A GeoPackage carrying no CRS at all."""
    return write_vector(tmp_path / "data" / "points_nocrs.gpkg", None)


@pytest.fixture
def raster_4326(tmp_path: Path) -> Path:
    """A north-up GeoTIFF in EPSG:4326."""
    return write_raster(tmp_path / "data" / "grid_4326.tif", "EPSG:4326")


@pytest.fixture
def polygon_4326(tmp_path: Path) -> Path:
    """A GeoPackage holding one polygon in EPSG:4326."""
    return write_vector(
        tmp_path / "data" / "poly_4326.gpkg",
        "EPSG:4326",
        geometries=[box(1.0, 51.0, 1.1, 51.1)],
    )
