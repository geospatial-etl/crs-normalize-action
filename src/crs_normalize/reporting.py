"""Rendering reports as terminal tables, JSON, GitHub annotations and summaries.

The GitHub formatter emits `workflow commands`_ that GitHub parses out of the
job log and turns into inline annotations. Their syntax is strict: properties
are comma-separated ``key=value`` pairs, and both the properties and the
message body require their own escaping rules, implemented in
:func:`_escape_property` and :func:`_escape_data`.

.. _workflow commands:
   https://docs.github.com/actions/using-workflows/workflow-commands-for-github-actions
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from rich.console import Console
from rich.table import Table

from .codes import Severity
from .models import Finding, Report

__all__ = [
    "render_terminal",
    "render_json",
    "render_github",
    "render_markdown_summary",
    "format_annotation",
]

logger = logging.getLogger(__name__)

#: Maps our severities onto the three annotation levels GitHub understands.
_ANNOTATION_LEVELS: dict[Severity, str] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.NOTICE: "notice",
}


def _escape_data(value: str) -> str:
    """Escape a workflow-command message body.

    GitHub requires ``%``, carriage return and newline to be percent-encoded so
    that a multi-line message stays on one log line.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a workflow-command property value.

    In addition to the message-body rules, ``:`` and ``,`` must be encoded
    because they delimit the property list itself.
    """
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def format_annotation(finding: Finding) -> str:
    """Render one finding as a GitHub Actions workflow command.

    The produced string has the shape::

        ::error file=data/a.gpkg,line=1,title=CRS001_MISSING_CRS::<message>

    ``line=1`` is always emitted: spatial datasets are binary and have no
    meaningful line number, but GitHub only renders a file-level annotation
    when a line is present.

    Args:
        finding: The finding to render.

    Returns:
        A single-line workflow command, without a trailing newline.
    """
    level = _ANNOTATION_LEVELS[finding.severity]
    properties = ",".join(
        (
            f"file={_escape_property(str(finding.path))}",
            "line=1",
            f"title={_escape_property(finding.code.value)}",
        )
    )
    return f"::{level} {properties}::{_escape_data(finding.rendered_message())}"


def render_github(report: Report) -> str:
    """Render every finding in ``report`` as workflow commands.

    Args:
        report: The report to render.

    Returns:
        Newline-separated workflow commands, one per finding.
    """
    return "\n".join(format_annotation(f) for f in report.findings)


def render_json(report: Report) -> str:
    """Render ``report`` as an indented JSON document.

    The document includes the derived aggregates the Action needs as outputs
    (``files_scanned``, ``files_changed``, ``crs_histogram``) alongside the raw
    datasets, findings and changes.

    Args:
        report: The report to render.

    Returns:
        A JSON string.
    """
    payload = {
        "mode": report.mode,
        "target_crs": report.target_crs,
        "files_scanned": report.files_scanned,
        "files_changed": report.files_changed,
        "crs_histogram": report.crs_histogram(),
        "datasets": [d.model_dump(mode="json") for d in report.datasets],
        "findings": [f.model_dump(mode="json") for f in report.findings],
        "changes": [c.model_dump(mode="json") for c in report.changes],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _severity_style(severity: Severity) -> str:
    """Return the rich style to use for a severity."""
    return {
        Severity.ERROR: "bold red",
        Severity.WARNING: "yellow",
        Severity.NOTICE: "cyan",
    }[severity]


def render_terminal(report: Report, console: Console | None = None) -> None:
    """Print ``report`` as rich tables.

    Args:
        report: The report to render.
        console: Console to print to. A new one is created when omitted.
    """
    console = console or Console()

    histogram = report.crs_histogram()
    if histogram:
        table = Table(title="CRS distribution", header_style="bold")
        table.add_column("CRS")
        table.add_column("Datasets", justify="right")
        for crs, count in histogram.items():
            table.add_row(crs, str(count))
        console.print(table)

    if report.datasets:
        table = Table(title="Datasets", header_style="bold")
        table.add_column("Path", overflow="fold")
        table.add_column("Kind")
        table.add_column("CRS")
        table.add_column("Features", justify="right")
        for dataset in report.datasets:
            table.add_row(
                str(dataset.path),
                dataset.kind.value,
                dataset.crs_label if dataset.readable else "[red]unreadable[/red]",
                "" if dataset.feature_count is None else str(dataset.feature_count),
            )
        console.print(table)

    if report.changes:
        table = Table(title="Reprojections", header_style="bold")
        table.add_column("Path", overflow="fold")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Transformation", overflow="fold")
        table.add_column("Accuracy", justify="right")
        for change in report.changes:
            table.add_row(
                str(change.output_path),
                change.source_crs or "<none>",
                change.target_crs,
                (change.transform.name if change.transform else "") or "",
                change.transform.accuracy_label if change.transform else "",
            )
        console.print(table)

    if report.findings:
        table = Table(title="Findings", header_style="bold")
        table.add_column("Severity")
        table.add_column("Code")
        table.add_column("Path", overflow="fold")
        table.add_column("Message", overflow="fold")
        for finding in report.findings:
            table.add_row(
                f"[{_severity_style(finding.severity)}]{finding.severity.value}[/]",
                finding.code.value,
                str(finding.path),
                finding.rendered_message(),
            )
        console.print(table)
    else:
        console.print("[green]No CRS problems found.[/green]")


def _markdown_escape(value: str) -> str:
    """Escape pipe characters so a value cannot break out of a table cell."""
    return value.replace("|", "\\|")


def _summary_rows(report: Report) -> Iterable[str]:
    """Yield the per-dataset rows of the markdown summary table."""
    changes_by_path = {c.path: c for c in report.changes}
    for dataset in report.datasets:
        change = changes_by_path.get(dataset.path)
        if change is not None and change.transform is not None:
            target = change.target_crs
            transform_name = change.transform.name or "-"
            accuracy = change.transform.accuracy_label
        else:
            target = report.target_crs or "-"
            transform_name = "-"
            accuracy = "-"
        source = dataset.crs_label if dataset.readable else "unreadable"
        yield (
            f"| `{_markdown_escape(str(dataset.path))}` | {dataset.kind.value} "
            f"| {_markdown_escape(source)} | {_markdown_escape(target)} "
            f"| {_markdown_escape(transform_name)} | {accuracy} |"
        )


def render_markdown_summary(report: Report) -> str:
    """Render ``report`` as a GitHub job summary in markdown.

    Includes a headline verdict, the CRS histogram, a per-dataset table showing
    the transformation used and its accuracy, and the full list of findings
    with their remediation hints.

    Args:
        report: The report to render.

    Returns:
        A markdown document.
    """
    errors = report.errors
    warnings = report.warnings
    lines: list[str] = ["## CRS normalization"]

    if errors:
        lines.append(
            f"**{len(errors)} unresolved CRS problem(s)** across {report.files_scanned} dataset(s)."
        )
    elif warnings:
        lines.append(
            f"{report.files_scanned} dataset(s) scanned, {len(warnings)} warning(s), no blocking problems."
        )
    else:
        lines.append(f"{report.files_scanned} dataset(s) scanned. No CRS problems found.")

    if report.files_changed:
        lines.append(f"{report.files_changed} dataset(s) reprojected to `{report.target_crs}`.")

    histogram = report.crs_histogram()
    if histogram:
        lines += ["", "### CRS distribution", "", "| CRS | Datasets |", "| --- | ---: |"]
        lines += [f"| `{_markdown_escape(crs)}` | {count} |" for crs, count in histogram.items()]

    if report.datasets:
        lines += [
            "",
            "### Datasets",
            "",
            "| File | Kind | Source CRS | Target CRS | Transformation | Accuracy |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
        lines += list(_summary_rows(report))

    if report.findings:
        lines += ["", "### Findings", ""]
        for finding in report.findings:
            marker = "❌" if finding.severity is Severity.ERROR else "⚠️"
            lines.append(
                f"- {marker} **{finding.code.value}** `{_markdown_escape(str(finding.path))}` — "
                f"{_markdown_escape(finding.rendered_message())}"
            )

    lines.append("")
    return "\n".join(lines)
