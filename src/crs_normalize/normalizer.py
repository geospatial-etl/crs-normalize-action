"""Reprojection of vector and raster datasets onto a single target CRS.

Normalization never proceeds on a dataset that the scanner considers
unresolvable. A missing CRS with no ``assume_crs``, an unreachable target, a
missing datum-shift grid or a rotated raster all stop that dataset from being
rewritten, and the corresponding finding is carried through to the report.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import geopandas as gpd
import rasterio
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

from .codes import Code, Severity
from .crs_utils import assess_transform, crs_identifier
from .models import DatasetInfo, DatasetKind, FileChange, Finding, Report, TransformReport
from .scanner import ScanOptions, dataset_crs, scan

__all__ = ["NormalizeOptions", "RESAMPLING_METHODS", "normalize", "parse_resampling"]

logger = logging.getLogger(__name__)

#: Resampling methods accepted by ``--resampling``. Restricted to those that
#: are meaningful for a warp of continuous or categorical raster data.
RESAMPLING_METHODS: dict[str, Resampling] = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "cubic_spline": Resampling.cubic_spline,
    "lanczos": Resampling.lanczos,
    "average": Resampling.average,
    "mode": Resampling.mode,
    "max": Resampling.max,
    "min": Resampling.min,
    "med": Resampling.med,
    "q1": Resampling.q1,
    "q3": Resampling.q3,
}

#: Drivers used when writing a normalized vector dataset, keyed by suffix.
_VECTOR_DRIVERS: dict[str, str] = {
    ".gpkg": "GPKG",
    ".shp": "ESRI Shapefile",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".fgb": "FlatGeobuf",
    ".gml": "GML",
}


def parse_resampling(name: str) -> Resampling:
    """Resolve a resampling method name.

    Args:
        name: One of the keys of :data:`RESAMPLING_METHODS`.

    Returns:
        The matching :class:`rasterio.enums.Resampling` member.

    Raises:
        ValueError: If the name is not a supported method.
    """
    try:
        return RESAMPLING_METHODS[name.lower()]
    except KeyError:
        supported = ", ".join(sorted(RESAMPLING_METHODS))
        raise ValueError(f"Unknown resampling method {name!r}. Supported methods: {supported}.") from None


class NormalizeOptions(ScanOptions):
    """Configuration for a normalize run.

    Attributes:
        output_dir: Directory to mirror input structure into. ``None`` rewrites
            datasets in place.
        resampling: Raster resampling method.
        dry_run: When true, decide what would change but write nothing.
    """

    def __init__(
        self,
        target_crs: str,
        assume_crs: str | None = None,
        max_transform_error: float | None = None,
        output_dir: Path | None = None,
        resampling: str = "nearest",
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            target_crs=target_crs,
            assume_crs=assume_crs,
            max_transform_error=max_transform_error,
        )
        if self.target is None:
            raise ValueError("A target CRS is required to normalize.")
        self.output_dir = Path(output_dir) if output_dir else None
        self.resampling_name = resampling
        self.resampling = parse_resampling(resampling)
        self.dry_run = dry_run

    @property
    def target_crs_required(self) -> CRS:
        """Return the target CRS, which is guaranteed to be set."""
        assert self.target is not None
        return self.target


def _output_path(path: Path, options: NormalizeOptions, roots: Sequence[Path]) -> Path:
    """Compute where a normalized dataset should be written.

    When an output directory is configured, the input's structure is mirrored
    beneath it relative to the closest matching input root, so that
    ``data/a/b.gpkg`` scanned from ``data`` lands at ``<out>/a/b.gpkg``.

    Args:
        path: Input dataset path.
        options: Normalize configuration.
        roots: Input roots supplied by the caller.

    Returns:
        The destination path (equal to ``path`` for in-place writes).
    """
    if options.output_dir is None:
        return path

    resolved = path.resolve()
    best: Path | None = None
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if not root_resolved.is_dir():
            root_resolved = root_resolved.parent
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if best is None or len(str(root_resolved)) > len(str(best)):
            best = root_resolved

    relative = resolved.relative_to(best) if best is not None else Path(path.name)
    return options.output_dir / relative


def _sidecars(path: Path) -> list[Path]:
    """Return existing shapefile sidecar files beside ``path``."""
    return [
        candidate
        for suffix in (".dbf", ".shx", ".prj", ".cpg", ".qix")
        if (candidate := path.with_suffix(suffix)).is_file()
    ]


def _normalize_vector(
    dataset: DatasetInfo,
    destination: Path,
    options: NormalizeOptions,
) -> None:
    """Reproject one vector dataset onto the target CRS and write it out."""
    frame = gpd.read_file(dataset.path)
    if frame.crs is None:
        frame = frame.set_crs(options.assumed, allow_override=True)
    reprojected = frame.to_crs(options.target_crs_required)

    suffix = destination.suffix.lower()
    driver = _VECTOR_DRIVERS.get(suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary sibling first so an interrupted write cannot leave a
    # half-reprojected dataset in place of the original.
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        staged = Path(staging) / destination.name
        if driver:
            reprojected.to_file(staged, driver=driver)
        else:
            reprojected.to_file(staged)
        for produced in Path(staging).iterdir():
            target = destination.with_name(produced.name)
            os.replace(produced, target)


def _normalize_raster(
    dataset: DatasetInfo,
    destination: Path,
    options: NormalizeOptions,
) -> None:
    """Warp one raster onto the target CRS and write it out."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = options.target_crs_required

    with rasterio.open(dataset.path) as src:
        source_crs = src.crs if src.crs is not None else rasterio.crs.CRS.from_wkt(options.assumed.to_wkt())
        transform, width, height = calculate_default_transform(
            source_crs, target.to_wkt(), src.width, src.height, *src.bounds
        )
        profile = src.profile.copy()
        profile.update(crs=target.to_wkt(), transform=transform, width=width, height=height)

        with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
            staged = Path(staging) / destination.name
            with rasterio.open(staged, "w", **profile) as dst:
                for band in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, band),
                        destination=rasterio.band(dst, band),
                        src_transform=src.transform,
                        src_crs=source_crs,
                        dst_transform=transform,
                        dst_crs=target.to_wkt(),
                        resampling=options.resampling,
                    )
            os.replace(staged, destination)


def _blocking_codes(findings: Sequence[Finding], path: Path) -> list[Finding]:
    """Return the findings that make ``path`` unsafe to reproject."""
    blocking = {
        Code.MISSING_CRS,
        Code.NO_TRANSFORM,
        Code.GRID_UNAVAILABLE,
        Code.ROTATED_RASTER,
        Code.UNREADABLE,
        Code.IMPLAUSIBLE_COORDINATES,
        Code.SIDECAR_MISMATCH,
        Code.TRANSFORM_ACCURACY,
    }
    return [
        f
        for f in findings
        if f.path == path and f.severity is Severity.ERROR and f.code in blocking
    ]


def normalize(paths: Sequence[str], options: NormalizeOptions) -> Report:
    """Reproject every resolvable dataset under ``paths`` onto the target CRS.

    The working set is scanned first. Datasets carrying a blocking error are
    left untouched and their findings are preserved in the returned report;
    everything else is reprojected. Datasets already in the target CRS are
    skipped rather than rewritten, so a clean repository produces no diff.

    Args:
        paths: Paths, directories or globs to normalize.
        options: Normalize configuration, including the required target CRS.

    Returns:
        A report whose ``changes`` list records every dataset rewritten (or,
        under ``dry_run``, every dataset that would have been).
    """
    report = scan(paths, options)
    report.mode = "check" if options.dry_run else "fix"
    report.target_crs = options.target_id

    roots = [Path(p) for p in paths]
    target_id = options.target_id or ""

    for dataset in report.datasets:
        if not dataset.readable:
            continue

        if dataset.crs == target_id:
            logger.debug("%s is already in %s", dataset.path, target_id)
            # When mirroring into an output directory the result must be a
            # complete copy of the input, so pass already-correct datasets
            # through untouched rather than omitting them.
            if options.output_dir is not None and not options.dry_run:
                _passthrough(dataset.path, _output_path(dataset.path, options, roots))
            continue

        blocking = _blocking_codes(report.findings, dataset.path)
        if blocking:
            logger.info(
                "Skipping %s: %s", dataset.path, ", ".join(f.code.value for f in blocking)
            )
            continue

        source = dataset_crs(dataset) or options.assumed
        if source is None:
            continue
        source_crs = dataset.crs or options.target_id
        transform_report: TransformReport = assess_transform(source, options.target_crs_required)
        destination = _output_path(dataset.path, options, roots)

        change = FileChange(
            path=dataset.path,
            output_path=destination,
            source_crs=crs_identifier(source),
            target_crs=target_id,
            transform=transform_report,
            written=False,
        )

        if options.dry_run:
            report.changes.append(change)
            continue

        try:
            if dataset.kind is DatasetKind.VECTOR:
                _normalize_vector(dataset, destination, options)
            elif dataset.kind is DatasetKind.RASTER:
                _normalize_raster(dataset, destination, options)
            else:
                continue
        except Exception as exc:
            logger.debug("Failed to write %s", destination, exc_info=True)
            report.findings.append(
                Finding(
                    code=Code.WRITE_FAILED,
                    severity=Severity.ERROR,
                    path=dataset.path,
                    message=(
                        f"Reprojection to {target_id} succeeded in memory but writing "
                        f"{destination} failed: {type(exc).__name__}: {exc}. The original dataset "
                        f"was left unmodified."
                    ),
                    detail={"destination": str(destination), "error": str(exc)},
                )
            )
            continue

        change.written = True
        report.changes.append(change)
        logger.info("Reprojected %s from %s to %s", dataset.path, source_crs, target_id)

    return report


def _passthrough(source: Path, destination: Path) -> None:
    """Copy a dataset and its sidecars to ``destination`` unchanged."""
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    for sidecar in _sidecars(source):
        shutil.copy2(sidecar, destination.with_name(sidecar.name))
