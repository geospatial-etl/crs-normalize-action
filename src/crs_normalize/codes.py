"""Stable, machine-readable diagnostic codes emitted by :mod:`crs_normalize`.

Every finding produced by a scan or a normalize run carries one of the codes
defined here. The string values are part of the public contract of this package
and of the GitHub Action built on top of it: they appear in JSON reports, in
GitHub Actions annotations (as the annotation ``title``) and in the generated
job summary. They are therefore treated as stable API and must not be renamed.

Each code is paired with a short human-readable title and a remediation hint
describing what a reader should actually *do* about the problem.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "Code",
    "Severity",
    "CODE_TITLES",
    "CODE_REMEDIES",
    "DOC_LINKS",
]


class Severity(StrEnum):
    """How serious a finding is.

    ``ERROR`` findings can fail the run depending on the configured
    ``fail-on`` policy; ``WARNING`` and ``NOTICE`` findings never do on their
    own, but are still reported and annotated.
    """

    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"


class Code(StrEnum):
    """Stable diagnostic codes.

    The numeric prefix is permanent. New codes append; existing codes are never
    reused for a different meaning.
    """

    MISSING_CRS = "CRS001_MISSING_CRS"
    MIXED_CRS = "CRS002_MIXED_CRS"
    IMPLAUSIBLE_COORDINATES = "CRS003_IMPLAUSIBLE_COORDINATES"
    SIDECAR_MISMATCH = "CRS004_SIDECAR_MISMATCH"
    NO_TRANSFORM = "CRS005_NO_TRANSFORM"
    GRID_UNAVAILABLE = "CRS006_GRID_UNAVAILABLE"
    ROTATED_RASTER = "CRS007_ROTATED_RASTER"
    TRANSFORM_ACCURACY = "CRS008_TRANSFORM_ACCURACY"
    UNREADABLE = "CRS009_UNREADABLE"
    AXIS_ORDER = "CRS010_AXIS_ORDER"
    WRITE_FAILED = "CRS011_WRITE_FAILED"


#: Canonical documentation links. Only used where a link genuinely helps the
#: reader understand the class of problem they have hit.
DOC_LINKS: Final[dict[Code, str]] = {
    Code.MISSING_CRS: (
        "https://www.geospatial-etl.com/automated-vector-raster-cleaning-workflows/"
        "crs-normalization-across-mixed-datasets/"
    ),
    Code.MIXED_CRS: (
        "https://www.geospatial-etl.com/automated-vector-raster-cleaning-workflows/"
        "crs-normalization-across-mixed-datasets/"
    ),
}


CODE_TITLES: Final[dict[Code, str]] = {
    Code.MISSING_CRS: "Dataset has no coordinate reference system",
    Code.MIXED_CRS: "Datasets do not share a single coordinate reference system",
    Code.IMPLAUSIBLE_COORDINATES: "Coordinates are implausible for the declared CRS",
    Code.SIDECAR_MISMATCH: "Sidecar .prj disagrees with the embedded CRS",
    Code.NO_TRANSFORM: "No coordinate transformation is available to the target CRS",
    Code.GRID_UNAVAILABLE: "The best transformation needs a datum-shift grid that is not installed",
    Code.ROTATED_RASTER: "Raster has a rotated or south-up geotransform",
    Code.TRANSFORM_ACCURACY: "Transformation accuracy is worse than the configured limit",
    Code.UNREADABLE: "Dataset could not be opened",
    Code.AXIS_ORDER: "Declared axis order disagrees with the stored coordinate order",
}


CODE_REMEDIES: Final[dict[Code, str]] = {
    Code.MISSING_CRS: (
        "Find out which CRS the data was authored in and declare it explicitly. "
        "Pass --assume-crs (Action input 'assume-crs') only when you know the answer; "
        "guessing silently corrupts every downstream coordinate."
    ),
    Code.MIXED_CRS: (
        "Reproject the outliers onto one CRS before they enter the pipeline. "
        "Run this tool in fix mode, or set --target to the CRS the rest of the data already uses."
    ),
    Code.IMPLAUSIBLE_COORDINATES: (
        "The declared CRS is almost certainly wrong for these numbers. Compare the "
        "coordinate magnitudes against the CRS's area of use, then re-declare the "
        "correct CRS rather than reprojecting from the wrong one."
    ),
    Code.SIDECAR_MISMATCH: (
        "Delete the stale sidecar .prj, or update it to match the CRS embedded in the "
        "dataset. GDAL's precedence between the two is format-dependent, so leaving both "
        "in place makes the effective CRS depend on which reader you use."
    ),
    Code.NO_TRANSFORM: (
        "PROJ knows both CRSs but has no operation connecting them, usually because one "
        "is an engineering or unreferenced CRS. Pick a target CRS that shares a datum "
        "path, or georeference the source properly first."
    ),
    Code.GRID_UNAVAILABLE: (
        "Install the missing datum-shift grid (pip install pyproj[network] and set "
        "PROJ_NETWORK=ON, or fetch the grid into PROJ_DATA). Without it PROJ silently "
        "falls back to a lower-accuracy transformation."
    ),
    Code.ROTATED_RASTER: (
        "Rectify the raster to a north-up grid first (for example gdalwarp the source "
        "onto its own CRS). Reprojecting a rotated transform would discard the rotation "
        "terms and shift every pixel."
    ),
    Code.TRANSFORM_ACCURACY: (
        "Either accept the error by raising --max-transform-error, or install the "
        "high-accuracy grid for this datum pair so PROJ can select a better operation."
    ),
    Code.UNREADABLE: (
        "Check the file is complete and that its driver is available in this "
        "environment. Shapefiles additionally need their .shx and .dbf siblings present."
    ),
    Code.AXIS_ORDER: (
        "The CRS declares latitude first but the stored coordinates look like "
        "longitude first (or vice versa). Rewrite the dataset with an explicit "
        "authority-compliant CRS so readers stop disagreeing about the axis order."
    ),
}
