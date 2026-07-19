"""Locating spatial datasets from user-supplied paths and globs."""

from __future__ import annotations

import glob as globlib
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import DatasetKind

__all__ = [
    "VECTOR_SUFFIXES",
    "RASTER_SUFFIXES",
    "classify",
    "discover",
]

logger = logging.getLogger(__name__)

#: Vector formats the scanner will attempt to open with pyogrio/GDAL.
VECTOR_SUFFIXES: frozenset[str] = frozenset(
    {".shp", ".geojson", ".json", ".gpkg", ".gml", ".kml", ".fgb", ".gpx", ".tab", ".mif"}
)

#: Raster formats the scanner will attempt to open with rasterio/GDAL.
RASTER_SUFFIXES: frozenset[str] = frozenset(
    {".tif", ".tiff", ".vrt", ".img", ".jp2", ".asc", ".bil", ".dem", ".nc"}
)

#: Sidecar and auxiliary files that must never be treated as datasets in
#: their own right.
_IGNORED_SUFFIXES: frozenset[str] = frozenset(
    {".prj", ".dbf", ".shx", ".cpg", ".qix", ".sbn", ".sbx", ".xml", ".aux", ".ovr", ".tfw", ".wld"}
)


def classify(path: Path) -> DatasetKind:
    """Return which reader should handle ``path``, based on its suffix.

    Args:
        path: Candidate dataset path.

    Returns:
        The dataset kind, or :attr:`DatasetKind.UNKNOWN` when the suffix is not
        recognised.
    """
    suffix = path.suffix.lower()
    if suffix in VECTOR_SUFFIXES:
        return DatasetKind.VECTOR
    if suffix in RASTER_SUFFIXES:
        return DatasetKind.RASTER
    return DatasetKind.UNKNOWN


def _expand(pattern: str) -> Iterable[Path]:
    """Expand one user-supplied path, directory or glob into candidate files."""
    path = Path(pattern)
    if path.is_dir():
        yield from (p for p in path.rglob("*") if p.is_file())
        return
    if path.is_file():
        yield path
        return
    # Treat anything else as a glob; recursive so that ``data/**/*.shp`` works.
    for match in globlib.iglob(pattern, recursive=True):
        candidate = Path(match)
        if candidate.is_dir():
            yield from (p for p in candidate.rglob("*") if p.is_file())
        elif candidate.is_file():
            yield candidate


def discover(patterns: Sequence[str]) -> list[Path]:
    """Resolve paths, directories and globs into a sorted list of datasets.

    Directories are walked recursively. Files whose suffix is not a known
    vector or raster format are skipped, as are shapefile sidecars, so that a
    directory of shapefiles yields one entry per ``.shp`` rather than one per
    component file.

    Args:
        patterns: Paths, directory names or glob patterns.

    Returns:
        Deduplicated, sorted dataset paths.
    """
    found: set[Path] = set()
    for pattern in patterns:
        for candidate in _expand(pattern):
            suffix = candidate.suffix.lower()
            if suffix in _IGNORED_SUFFIXES:
                continue
            if classify(candidate) is DatasetKind.UNKNOWN:
                logger.debug("Skipping %s: unrecognised spatial format", candidate)
                continue
            found.add(candidate)
    return sorted(found)
