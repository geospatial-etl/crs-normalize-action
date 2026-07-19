"""Reading CRS metadata and geometry extents out of individual datasets.

Inspection is deliberately cheap: vector datasets are interrogated through
:func:`pyogrio.read_info`, which reads only the layer header and the driver's
cached extent, and rasters through a :func:`rasterio.open` header read. Neither
path loads pixel or feature data, so scanning a large repository stays fast.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pyogrio
import rasterio
from pyproj import CRS

from .codes import Code, Severity
from .crs_utils import crs_display_name, crs_identifier, is_latitude_first
from .discovery import classify
from .models import DatasetInfo, DatasetKind, Finding

__all__ = ["inspect_dataset", "check_sidecar", "read_raster_transform"]

logger = logging.getLogger(__name__)


def _bounds_or_none(raw) -> tuple[float, float, float, float] | None:
    """Coerce a driver-reported extent into a plain tuple, or ``None``."""
    if raw is None:
        return None
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if len(values) != 4 or any(v != v for v in values):
        return None
    return (values[0], values[1], values[2], values[3])


def _inspect_vector(path: Path) -> tuple[DatasetInfo, list[Finding]]:
    """Read CRS, extent and feature count from a vector dataset."""
    info = pyogrio.read_info(path)
    raw_crs = info.get("crs")
    crs = CRS.from_user_input(raw_crs) if raw_crs else None
    return (
        DatasetInfo(
            path=path,
            kind=DatasetKind.VECTOR,
            crs=crs_identifier(crs),
            crs_name=crs_display_name(crs),
            crs_wkt=crs.to_wkt() if crs else None,
            bounds=_bounds_or_none(info.get("total_bounds")),
            feature_count=info.get("features"),
            driver=info.get("driver"),
            readable=True,
        ),
        [],
    )


def _inspect_raster(path: Path) -> tuple[DatasetInfo, list[Finding]]:
    """Read CRS, extent and geotransform sanity from a raster dataset."""
    findings: list[Finding] = []
    with rasterio.open(path) as src:
        crs = CRS.from_user_input(src.crs.to_wkt()) if src.crs else None
        bounds = (
            (float(src.bounds.left), float(src.bounds.bottom), float(src.bounds.right), float(src.bounds.top))
            if src.transform is not None
            else None
        )
        transform = src.transform
        dataset = DatasetInfo(
            path=path,
            kind=DatasetKind.RASTER,
            crs=crs_identifier(crs),
            crs_name=crs_display_name(crs),
            crs_wkt=crs.to_wkt() if crs else None,
            bounds=bounds,
            feature_count=None,
            driver=src.driver,
            readable=True,
        )

    if transform is not None and (transform.b != 0 or transform.d != 0):
        findings.append(
            Finding(
                code=Code.ROTATED_RASTER,
                severity=Severity.ERROR,
                path=path,
                message=(
                    f"Raster has a rotated geotransform (rotation terms b={transform.b:g}, "
                    f"d={transform.d:g} are non-zero). Reprojection assumes an axis-aligned, "
                    f"north-up grid and would silently drop the rotation."
                ),
                detail={"transform": list(transform)[:6]},
            )
        )
    elif transform is not None and transform.e > 0:
        findings.append(
            Finding(
                code=Code.ROTATED_RASTER,
                severity=Severity.WARNING,
                path=path,
                message=(
                    f"Raster is south-up: the north-south pixel size is positive (e={transform.e:g}), "
                    f"so row 0 is the southern edge rather than the northern one. Most consumers "
                    f"assume north-up and will render this flipped."
                ),
                detail={"transform": list(transform)[:6]},
            )
        )

    return dataset, findings


def read_raster_transform(path: Path):
    """Return the affine geotransform of a raster.

    Args:
        path: Raster path.

    Returns:
        The :class:`affine.Affine` transform of the dataset.
    """
    with rasterio.open(path) as src:
        return src.transform


def check_sidecar(path: Path, embedded: str | None) -> list[Finding]:
    """Compare a ``.prj`` sidecar against the CRS embedded in the dataset.

    Formats such as GeoTIFF and GeoPackage carry their own CRS, but a stale
    ``.prj`` written by an earlier step is often left beside them. Which one
    wins is reader-dependent, so a disagreement is reported rather than
    silently resolved. Shapefiles are exempt: for them the ``.prj`` *is* the
    embedded CRS, so the two can never disagree.

    Axis order is checked separately: when the sidecar and the embedded CRS
    describe the same datum and projection but disagree about which axis comes
    first, coordinates read through the two paths will be transposed.

    Args:
        path: Dataset path.
        embedded: Identifier of the CRS embedded in the dataset, or ``None``.

    Returns:
        Findings describing any disagreement, or an empty list.
    """
    if path.suffix.lower() == ".shp":
        return []

    sidecar = path.with_suffix(".prj")
    if not sidecar.is_file():
        return []

    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        logger.debug("Could not read sidecar %s: %s", sidecar, exc)
        return []
    if not text:
        return []

    try:
        sidecar_crs = CRS.from_user_input(text)
    except Exception as exc:
        return [
            Finding(
                code=Code.SIDECAR_MISMATCH,
                severity=Severity.WARNING,
                path=path,
                message=(
                    f"A sidecar {sidecar.name} sits beside this dataset but its contents are not a "
                    f"readable CRS definition ({exc}). Readers that prefer the sidecar over the "
                    f"embedded CRS will fail to georeference this file at all."
                ),
                detail={"sidecar": str(sidecar)},
            )
        ]

    sidecar_id = crs_identifier(sidecar_crs)

    if embedded is None:
        return [
            Finding(
                code=Code.SIDECAR_MISMATCH,
                severity=Severity.WARNING,
                path=path,
                message=(
                    f"The dataset declares no CRS of its own, but a sidecar {sidecar.name} declares "
                    f"{sidecar_id}. Readers that ignore sidecars for this format will treat the data "
                    f"as unreferenced."
                ),
                detail={"sidecar": str(sidecar), "sidecar_crs": sidecar_id, "embedded_crs": None},
            )
        ]

    try:
        embedded_crs = CRS.from_user_input(embedded)
    except Exception:
        return []

    if not embedded_crs.equals(sidecar_crs):
        return [
            Finding(
                code=Code.SIDECAR_MISMATCH,
                severity=Severity.ERROR,
                path=path,
                message=(
                    f"The CRS embedded in this dataset ({crs_identifier(embedded_crs)}) disagrees with "
                    f"the sidecar {sidecar.name} ({sidecar_id}). The effective CRS then depends on "
                    f"which reader opens the file, so downstream results are not reproducible."
                ),
                detail={
                    "sidecar": str(sidecar),
                    "sidecar_crs": sidecar_id,
                    "embedded_crs": crs_identifier(embedded_crs),
                },
            )
        ]

    if is_latitude_first(embedded_crs) != is_latitude_first(sidecar_crs):
        return [
            Finding(
                code=Code.AXIS_ORDER,
                severity=Severity.WARNING,
                path=path,
                message=(
                    f"The embedded CRS and the sidecar {sidecar.name} both resolve to {sidecar_id} but "
                    f"declare opposite axis orders. Coordinates read through one path will be "
                    f"transposed relative to the other."
                ),
                detail={
                    "sidecar": str(sidecar),
                    "embedded_latitude_first": is_latitude_first(embedded_crs),
                    "sidecar_latitude_first": is_latitude_first(sidecar_crs),
                },
            )
        ]

    return []


def inspect_dataset(path: Path) -> tuple[DatasetInfo, list[Finding]]:
    """Inspect one dataset and return its metadata plus any structural findings.

    Structural findings are those discoverable from the file alone: a rotated
    geotransform, a contradictory sidecar, or the file being unreadable.
    Cross-file concerns such as mixed CRS are decided by the scanner.

    Args:
        path: Dataset to inspect.

    Returns:
        A tuple of the dataset metadata and the findings raised for it. When the
        dataset cannot be opened, the metadata has ``readable=False`` and a
        :attr:`Code.UNREADABLE` finding is returned.
    """
    kind = classify(path)
    try:
        if kind is DatasetKind.VECTOR:
            dataset, findings = _inspect_vector(path)
        elif kind is DatasetKind.RASTER:
            dataset, findings = _inspect_raster(path)
        else:
            return (
                DatasetInfo(path=path, kind=DatasetKind.UNKNOWN, readable=False),
                [],
            )
    except Exception as exc:
        logger.debug("Failed to open %s", path, exc_info=True)
        return (
            DatasetInfo(path=path, kind=kind, readable=False),
            [
                Finding(
                    code=Code.UNREADABLE,
                    severity=Severity.ERROR,
                    path=path,
                    message=(
                        f"Could not open this dataset as a {kind.value} source: "
                        f"{type(exc).__name__}: {exc}."
                    ),
                    detail={"error": str(exc), "kind": kind.value},
                )
            ],
        )

    findings.extend(check_sidecar(path, dataset.crs))
    return dataset, findings
