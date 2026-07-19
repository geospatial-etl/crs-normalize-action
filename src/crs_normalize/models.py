"""Data model for scan and normalize results.

All models are :mod:`pydantic` models so that reports round-trip losslessly to
JSON (``--format json``) and so that the shapes consumed by the GitHub Action
are validated rather than assumed.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .codes import CODE_REMEDIES, DOC_LINKS, Code, Severity

__all__ = [
    "DatasetKind",
    "Finding",
    "TransformReport",
    "DatasetInfo",
    "FileChange",
    "Report",
    "ExitCode",
]


class ExitCode(IntEnum):
    """Process exit codes.

    An :class:`~enum.IntEnum` so members can be passed straight to
    :func:`sys.exit` and compared against raw integers in tests and shell
    scripts without conversion.

    Attributes:
        CLEAN: No problems found and nothing needed changing.
        FAILED: At least one unresolvable problem, per the ``fail_on`` policy.
        CHANGED: The run completed but datasets were (or would be) modified.
        USAGE: The invocation itself was invalid.
    """

    CLEAN = 0
    FAILED = 1
    CHANGED = 2
    USAGE = 3


class DatasetKind(StrEnum):
    """Which reader handled a dataset."""

    VECTOR = "vector"
    RASTER = "raster"
    UNKNOWN = "unknown"


class Finding(BaseModel):
    """A single diagnostic attached to a dataset.

    Attributes:
        code: Stable machine-readable code.
        severity: Whether this can fail the run.
        path: Dataset the finding relates to.
        message: Instructive, self-contained explanation of the problem.
        detail: Optional extra context (values, CRS names) for JSON consumers.
    """

    model_config = ConfigDict(frozen=True)

    code: Code
    severity: Severity
    path: Path
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remedy(self) -> str:
        """Return the remediation hint registered for :attr:`code`."""
        return CODE_REMEDIES.get(self.code, "")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def help_url(self) -> str | None:
        """Return a documentation link for this code, when one genuinely helps."""
        return DOC_LINKS.get(self.code)

    def rendered_message(self) -> str:
        """Return the message with its remediation hint and link appended.

        This is what humans see in annotations and in the terminal. It is
        deliberately verbose: a CRS failure in CI is useless unless the reader
        can tell what was wrong and what to do next.
        """
        parts = [self.message.strip()]
        if self.remedy:
            parts.append(self.remedy.strip())
        if self.help_url:
            parts.append(f"See {self.help_url}")
        return " ".join(parts)


class TransformReport(BaseModel):
    """The coordinate operation actually selected for one source CRS.

    Attributes:
        source_crs: Source CRS as a compact identifier or WKT fallback.
        target_crs: Target CRS as a compact identifier.
        name: PROJ's description of the chosen operation.
        accuracy_m: Declared accuracy in metres, or ``None`` when PROJ reports
            the accuracy as unknown.
        available: Whether a usable operation exists at all.
        best_available: Whether PROJ could select its preferred operation, or
            had to fall back because a grid was missing.
        missing_grids: Short names of grids that would improve accuracy.
    """

    source_crs: str
    target_crs: str
    name: str | None = None
    accuracy_m: float | None = None
    available: bool = True
    best_available: bool = True
    missing_grids: list[str] = Field(default_factory=list)

    @property
    def accuracy_label(self) -> str:
        """Return a human-readable accuracy string for tables and summaries."""
        if not self.available:
            return "n/a"
        if self.accuracy_m is None:
            return "unknown"
        if self.accuracy_m == 0:
            return "exact"
        return f"{self.accuracy_m:g} m"


class DatasetInfo(BaseModel):
    """Everything the scanner learned about one dataset."""

    path: Path
    kind: DatasetKind
    crs: str | None = None
    crs_name: str | None = None
    #: Full WKT of the declared CRS. Retained because :attr:`crs` is a lossy
    #: display label: a CRS with no authority code cannot be reconstructed from
    #: it, and reprojection needs the exact definition.
    crs_wkt: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    feature_count: int | None = None
    driver: str | None = None
    readable: bool = True

    @property
    def crs_label(self) -> str:
        """Return the CRS identifier for display, or a missing-CRS marker."""
        return self.crs or "<none>"


class FileChange(BaseModel):
    """A dataset that was rewritten (or would be, in check mode)."""

    path: Path
    output_path: Path
    source_crs: str | None
    target_crs: str
    transform: TransformReport | None = None
    written: bool = False


class Report(BaseModel):
    """Aggregate result of a scan or normalize run.

    Attributes:
        datasets: Every dataset that was discovered, readable or not.
        findings: All diagnostics raised across those datasets.
        changes: Datasets rewritten or scheduled for rewrite.
        target_crs: The requested target CRS, when normalizing.
        mode: ``"scan"``, ``"check"`` or ``"fix"``.
    """

    datasets: list[DatasetInfo] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    changes: list[FileChange] = Field(default_factory=list)
    target_crs: str | None = None
    mode: str = "scan"

    @property
    def errors(self) -> list[Finding]:
        """Return only the findings with ``ERROR`` severity."""
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        """Return only the findings with ``WARNING`` severity."""
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def files_scanned(self) -> int:
        """Return the number of datasets discovered."""
        return len(self.datasets)

    @property
    def files_changed(self) -> int:
        """Return the number of datasets actually rewritten."""
        return sum(1 for c in self.changes if c.written)

    def crs_histogram(self) -> dict[str, int]:
        """Return a mapping of CRS identifier to dataset count.

        Datasets with no CRS are counted under the key ``"<none>"`` so that the
        histogram always accounts for every scanned file.
        """
        histogram: dict[str, int] = {}
        for dataset in self.datasets:
            if not dataset.readable:
                continue
            histogram[dataset.crs_label] = histogram.get(dataset.crs_label, 0) + 1
        return dict(sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])))

    def has_code(self, code: Code) -> bool:
        """Return whether any finding carries ``code``."""
        return any(f.code is code for f in self.findings)

    def merge(self, other: Self) -> None:
        """Fold another report's datasets, findings and changes into this one."""
        self.datasets.extend(other.datasets)
        self.findings.extend(other.findings)
        self.changes.extend(other.changes)
