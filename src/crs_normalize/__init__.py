"""Detect and fix mixed or missing coordinate reference systems in spatial data.

``crs_normalize`` scans vector and raster datasets, reports what CRS each one
declares, identifies the cases that cannot be safely resolved automatically,
and reprojects the rest onto a single target CRS.

The public API mirrors the CLI:

>>> from crs_normalize import ScanOptions, scan
>>> report = scan(["data/"], ScanOptions(target_crs="EPSG:3857"))
>>> sorted(report.crs_histogram())  # doctest: +SKIP
['EPSG:27700', 'EPSG:4326']
"""

from __future__ import annotations

import logging

from .codes import CODE_REMEDIES, CODE_TITLES, Code, Severity
from .crs_utils import assess_transform, check_coordinate_plausibility, parse_crs
from .discovery import discover
from .inspector import inspect_dataset
from .models import (
    DatasetInfo,
    DatasetKind,
    ExitCode,
    FileChange,
    Finding,
    Report,
    TransformReport,
)
from .normalizer import RESAMPLING_METHODS, NormalizeOptions, normalize
from .reporting import (
    format_annotation,
    render_github,
    render_json,
    render_markdown_summary,
    render_terminal,
)
from .scanner import ScanOptions, scan

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "CODE_REMEDIES",
    "CODE_TITLES",
    "Code",
    "Severity",
    "DatasetInfo",
    "DatasetKind",
    "ExitCode",
    "FileChange",
    "Finding",
    "Report",
    "TransformReport",
    "ScanOptions",
    "NormalizeOptions",
    "RESAMPLING_METHODS",
    "scan",
    "normalize",
    "discover",
    "inspect_dataset",
    "parse_crs",
    "assess_transform",
    "check_coordinate_plausibility",
    "format_annotation",
    "render_github",
    "render_json",
    "render_markdown_summary",
    "render_terminal",
]

# Library code must never configure logging for its consumers; attaching a
# null handler keeps it silent unless the application opts in.
logging.getLogger(__name__).addHandler(logging.NullHandler())
