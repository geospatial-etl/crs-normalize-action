"""Scanning: turn a set of paths into a :class:`~crs_normalize.models.Report`.

The scanner owns every judgement that needs to see more than one dataset (mixed
CRS across the working set) or that needs to know the intended target CRS
(transform availability, datum-shift grids, transformation accuracy).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pyproj import CRS

from .codes import Code, Severity
from .crs_utils import (
    assess_transform,
    check_coordinate_plausibility,
    crs_identifier,
    parse_crs,
)
from .discovery import discover
from .inspector import inspect_dataset
from .models import DatasetInfo, Finding, Report, TransformReport

__all__ = ["ScanOptions", "scan", "resolve_transforms", "dataset_crs"]

logger = logging.getLogger(__name__)


class ScanOptions:
    """Configuration for a scan.

    Attributes:
        target_crs: CRS everything should end up in, or ``None`` to only
            report the CRSs that are present.
        assume_crs: CRS to attribute to datasets that declare none. Without
            this, a missing CRS is unresolvable by design.
        max_transform_error: Accuracy limit in metres. Transformations whose
            declared accuracy exceeds this are flagged.
    """

    def __init__(
        self,
        target_crs: str | None = None,
        assume_crs: str | None = None,
        max_transform_error: float | None = None,
    ) -> None:
        self.target: CRS | None = parse_crs(target_crs) if target_crs else None
        self.assumed: CRS | None = parse_crs(assume_crs) if assume_crs else None
        self.max_transform_error = max_transform_error

    @property
    def target_id(self) -> str | None:
        """Return the compact identifier of the target CRS, if set."""
        return crs_identifier(self.target)


def dataset_crs(dataset: DatasetInfo) -> CRS | None:
    """Return the CRS a dataset declares, reconstructed from its stored WKT.

    The WKT is preferred over :attr:`DatasetInfo.crs` because the latter is a
    display label: a CRS with no authority code degrades to its name there and
    cannot be parsed back.

    Args:
        dataset: Dataset metadata.

    Returns:
        The declared CRS, or ``None`` when the dataset declares none or its
        definition cannot be parsed.
    """
    source = dataset.crs_wkt or dataset.crs
    if not source:
        return None
    try:
        return CRS.from_user_input(source)
    except Exception:
        logger.debug("Unparseable CRS on %s", dataset.path)
        return None


def _effective_crs(dataset: DatasetInfo, options: ScanOptions) -> CRS | None:
    """Return the CRS a dataset should be treated as having.

    Falls back to ``assume_crs`` when the dataset declares nothing.
    """
    return dataset_crs(dataset) or options.assumed


def _missing_crs_finding(dataset: DatasetInfo, options: ScanOptions) -> Finding:
    """Build the finding for a dataset that declares no CRS."""
    if options.assumed is not None:
        return Finding(
            code=Code.MISSING_CRS,
            severity=Severity.WARNING,
            path=dataset.path,
            message=(
                f"This dataset declares no CRS. Proceeding with the assumed CRS "
                f"{crs_identifier(options.assumed)} because it was supplied explicitly. Verify that "
                f"this really is the CRS the data was authored in before trusting the output."
            ),
            detail={"assumed_crs": crs_identifier(options.assumed)},
        )
    return Finding(
        code=Code.MISSING_CRS,
        severity=Severity.ERROR,
        path=dataset.path,
        message=(
            "This dataset declares no coordinate reference system, so its coordinates cannot be "
            "placed on the earth and no transformation can be computed. This tool will not guess: "
            "an incorrect guess produces data that looks valid and is silently in the wrong place."
        ),
        detail={"assumed_crs": None},
    )


def _mixed_crs_findings(datasets: Sequence[DatasetInfo], target_id: str | None) -> list[Finding]:
    """Report datasets that disagree with the dominant CRS of the working set.

    When no target CRS is configured, the most common CRS in the set is treated
    as the intended one and every other dataset is flagged. This is what makes
    ``fail-on: mixed`` useful as a repository-hygiene gate.
    """
    present = [d for d in datasets if d.readable and d.crs]
    distinct = {d.crs for d in present if d.crs}
    if len(distinct) <= 1:
        return []

    counts: dict[str, int] = {}
    for dataset in present:
        assert dataset.crs is not None
        counts[dataset.crs] = counts.get(dataset.crs, 0) + 1
    dominant = target_id if target_id in counts else max(counts, key=lambda k: (counts[k], k))

    summary = ", ".join(f"{crs} ({counts[crs]})" for crs in sorted(counts, key=lambda k: (-counts[k], k)))
    return [
        Finding(
            code=Code.MIXED_CRS,
            severity=Severity.ERROR,
            path=dataset.path,
            message=(
                f"This dataset is in {dataset.crs}, but the scanned set is not in a single CRS: "
                f"{summary}. Overlaying or joining these datasets without reprojecting first "
                f"produces silently misaligned geometry."
            ),
            detail={"dataset_crs": dataset.crs, "dominant_crs": dominant, "histogram": counts},
        )
        for dataset in present
        if dataset.crs != dominant
    ]


def resolve_transforms(
    datasets: Sequence[DatasetInfo],
    options: ScanOptions,
) -> tuple[dict[str, TransformReport], list[Finding]]:
    """Determine, per source CRS, whether the target CRS is reachable.

    Args:
        datasets: Datasets under consideration.
        options: Scan configuration; a target CRS must be set.

    Returns:
        A mapping of source CRS identifier to its :class:`TransformReport`, and
        the findings raised for unreachable, grid-starved or low-accuracy
        transformations.
    """
    if options.target is None:
        return {}, []

    transforms: dict[str, TransformReport] = {}
    findings: list[Finding] = []

    for dataset in datasets:
        if not dataset.readable:
            continue
        source = _effective_crs(dataset, options)
        if source is None:
            continue
        source_id = crs_identifier(source) or "unknown"
        report = transforms.get(source_id)
        if report is None:
            report = assess_transform(source, options.target)
            transforms[source_id] = report

        if not report.available:
            findings.append(
                Finding(
                    code=Code.NO_TRANSFORM,
                    severity=Severity.ERROR,
                    path=dataset.path,
                    message=(
                        f"PROJ offers no coordinate operation from {source_id} to {options.target_id}, "
                        f"so this dataset cannot be reprojected. This usually means one of the two is "
                        f"an engineering, image or otherwise unreferenced CRS with no path to a datum."
                    ),
                    detail={"source_crs": source_id, "target_crs": options.target_id},
                )
            )
            continue

        if not report.best_available:
            grids = ", ".join(report.missing_grids) or "an unnamed grid"
            findings.append(
                Finding(
                    code=Code.GRID_UNAVAILABLE,
                    severity=Severity.ERROR,
                    path=dataset.path,
                    message=(
                        f"The most accurate transformation from {source_id} to {options.target_id} "
                        f"requires the datum-shift grid {grids}, which is not installed. PROJ would "
                        f"fall back to '{report.name}' with an accuracy of {report.accuracy_label}, "
                        f"introducing an error you did not ask for."
                    ),
                    detail={
                        "source_crs": source_id,
                        "target_crs": options.target_id,
                        "missing_grids": report.missing_grids,
                        "fallback": report.name,
                        "fallback_accuracy_m": report.accuracy_m,
                    },
                )
            )

        if (
            options.max_transform_error is not None
            and report.accuracy_m is not None
            and report.accuracy_m > options.max_transform_error
        ):
            findings.append(
                Finding(
                    code=Code.TRANSFORM_ACCURACY,
                    severity=Severity.ERROR,
                    path=dataset.path,
                    message=(
                        f"Transforming {source_id} to {options.target_id} uses '{report.name}', whose "
                        f"declared accuracy is {report.accuracy_label}. That exceeds the configured "
                        f"limit of {options.max_transform_error:g} m."
                    ),
                    detail={
                        "source_crs": source_id,
                        "target_crs": options.target_id,
                        "transform": report.name,
                        "accuracy_m": report.accuracy_m,
                        "max_transform_error": options.max_transform_error,
                    },
                )
            )

    return transforms, findings


def scan(paths: Sequence[str], options: ScanOptions | None = None) -> Report:
    """Scan datasets and return a full report.

    Args:
        paths: Paths, directories or globs to scan.
        options: Scan configuration. Defaults to a bare scan with no target CRS.

    Returns:
        A populated :class:`Report`. The report is returned even when every
        dataset failed to open; callers decide what constitutes failure.
    """
    options = options or ScanOptions()
    report = Report(mode="scan", target_crs=options.target_id)

    discovered = discover(paths)
    logger.info("Discovered %d dataset(s)", len(discovered))

    for path in discovered:
        dataset, findings = inspect_dataset(path)
        report.datasets.append(dataset)
        report.findings.extend(findings)

        if not dataset.readable:
            continue

        if dataset.crs is None:
            report.findings.append(_missing_crs_finding(dataset, options))

        effective = _effective_crs(dataset, options)
        if effective is not None:
            report.findings.extend(
                check_coordinate_plausibility(dataset.path, effective, dataset.bounds)
            )

    report.findings.extend(_mixed_crs_findings(report.datasets, options.target_id))

    _, transform_findings = resolve_transforms(report.datasets, options)
    report.findings.extend(transform_findings)

    return report
