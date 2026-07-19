"""Command-line interface for ``crs-normalize``.

Two commands are exposed:

``scan``
    Read-only inspection of a set of datasets.
``normalize``
    Reprojection onto a target CRS, with ``--check`` for a dry run.

Both share the output plumbing: ``--format`` selects the report rendering,
``--summary-file`` writes a GitHub job summary, and the exit code communicates
the verdict to CI. See :class:`crs_normalize.models.ExitCode`.
"""

from __future__ import annotations

import logging
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from .codes import Code
from .github import upsert_comment
from .models import ExitCode, Report
from .normalizer import RESAMPLING_METHODS, NormalizeOptions, normalize
from .reporting import (
    render_github,
    render_json,
    render_markdown_summary,
    render_terminal,
)
from .scanner import ScanOptions, scan

__all__ = ["app", "main"]

logger = logging.getLogger("crs_normalize")

app = typer.Typer(
    name="crs-normalize",
    help="Detect and fix mixed or missing coordinate reference systems in spatial datasets.",
    no_args_is_help=True,
    add_completion=False,
)


class OutputFormat(StrEnum):
    """Available report renderings."""

    TABLE = "table"
    JSON = "json"
    GITHUB = "github"


class FailOn(StrEnum):
    """Policy deciding which findings fail the run.

    Attributes:
        MIXED: Fail on anything, including datasets merely disagreeing on CRS.
        UNRESOLVABLE: Fail only on problems this tool cannot fix on its own.
        NEVER: Always exit successfully; report only.
    """

    MIXED = "mixed"
    UNRESOLVABLE = "unresolvable"
    NEVER = "never"


#: Codes that represent something the tool genuinely cannot resolve for you,
#: as opposed to a difference it could reconcile by reprojecting.
_UNRESOLVABLE_CODES: frozenset[Code] = frozenset(
    {
        Code.MISSING_CRS,
        Code.NO_TRANSFORM,
        Code.GRID_UNAVAILABLE,
        Code.ROTATED_RASTER,
        Code.IMPLAUSIBLE_COORDINATES,
        Code.SIDECAR_MISMATCH,
        Code.TRANSFORM_ACCURACY,
        Code.UNREADABLE,
        Code.WRITE_FAILED,
    }
)


def _configure_logging(verbose: bool) -> None:
    """Install a rich logging handler at the requested verbosity."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=Console(stderr=True), show_path=False, rich_tracebacks=True)],
        force=True,
    )


def _emit(report: Report, output_format: OutputFormat, console: Console) -> None:
    """Render ``report`` to stdout in the requested format."""
    if output_format is OutputFormat.JSON:
        console.print_json(render_json(report))
    elif output_format is OutputFormat.GITHUB:
        rendered = render_github(report)
        if rendered:
            # Workflow commands must reach the log verbatim, so bypass rich's
            # markup and wrapping entirely.
            sys.stdout.write(rendered + "\n")
            sys.stdout.flush()
    else:
        render_terminal(report, console)


def _write_side_outputs(
    report: Report,
    summary_file: Path | None,
    report_file: Path | None,
    comment_on_pr: bool,
) -> None:
    """Write the job summary, JSON report and pull request comment."""
    markdown = render_markdown_summary(report)

    if summary_file is not None:
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        with summary_file.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
        logger.debug("Wrote job summary to %s", summary_file)

    if report_file is not None:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(render_json(report), encoding="utf-8")
        logger.debug("Wrote JSON report to %s", report_file)

    if comment_on_pr:
        upsert_comment(markdown)


def _verdict(report: Report, fail_on: FailOn, changed: bool) -> int:
    """Map a report onto a process exit code.

    Args:
        report: The completed report.
        fail_on: Which findings should fail the run.
        changed: Whether any dataset was rewritten.

    Returns:
        One of the :class:`ExitCode` values.
    """
    errors = report.errors
    if fail_on is FailOn.NEVER:
        blocking: list = []
    elif fail_on is FailOn.UNRESOLVABLE:
        blocking = [f for f in errors if f.code in _UNRESOLVABLE_CODES]
    else:
        blocking = list(errors)

    if blocking:
        return ExitCode.FAILED
    if changed:
        return ExitCode.CHANGED
    return ExitCode.CLEAN


def _fail_usage(message: str) -> None:
    """Report an invalid invocation and exit with the usage code."""
    Console(stderr=True).print(f"[bold red]Usage error:[/bold red] {message}")
    raise typer.Exit(ExitCode.USAGE)


PathsArg = Annotated[
    list[str],
    typer.Argument(
        metavar="PATHS...",
        help="Files, directories or globs to inspect. Directories are walked recursively.",
    ),
]


@app.command()
def scan_command(
    paths: PathsArg,
    target: Annotated[
        str | None,
        typer.Option("--target", "-t", help="Target CRS to assess reachability and accuracy against."),
    ] = None,
    assume_crs: Annotated[
        str | None,
        typer.Option("--assume-crs", help="CRS to attribute to datasets that declare none."),
    ] = None,
    max_transform_error: Annotated[
        float | None,
        typer.Option(
            "--max-transform-error",
            help="Flag transformations less accurate than this, in metres.",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Report rendering.")
    ] = OutputFormat.TABLE,
    summary_file: Annotated[
        Path | None, typer.Option("--summary-file", help="Append a markdown job summary to this file.")
    ] = None,
    report_file: Annotated[
        Path | None, typer.Option("--report-file", help="Write the full JSON report to this file.")
    ] = None,
    fail_on: Annotated[
        FailOn, typer.Option("--fail-on", help="Which findings cause a non-zero exit.")
    ] = FailOn.UNRESOLVABLE,
    comment_on_pr: Annotated[
        bool, typer.Option("--comment-on-pr/--no-comment-on-pr", help="Post the summary to the pull request.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging.")] = False,
) -> None:
    """Inspect datasets and report their coordinate reference systems.

    Reads only metadata, never writes. Detects missing CRS, mixed CRS across the
    working set, coordinates implausible for the CRS they claim, sidecar
    disagreements and rotated rasters. With ``--target``, additionally reports
    whether each source CRS can reach that target and how accurate the
    transformation would be.
    """
    _configure_logging(verbose)
    console = Console()

    try:
        options = ScanOptions(
            target_crs=target,
            assume_crs=assume_crs,
            max_transform_error=max_transform_error,
        )
    except ValueError as exc:
        _fail_usage(str(exc))
        return

    if max_transform_error is not None and max_transform_error < 0:
        _fail_usage("--max-transform-error must not be negative.")

    report = scan(paths, options)
    _emit(report, output_format, console)
    _write_side_outputs(report, summary_file, report_file, comment_on_pr)
    raise typer.Exit(_verdict(report, fail_on, changed=False))


@app.command()
def normalize_command(
    paths: PathsArg,
    target: Annotated[
        str,
        typer.Option("--target", "-t", help="Target CRS, for example EPSG:3857."),
    ],
    assume_crs: Annotated[
        str | None,
        typer.Option("--assume-crs", help="CRS to attribute to datasets that declare none."),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="Write results here, mirroring the input structure."),
    ] = None,
    resampling: Annotated[
        str, typer.Option("--resampling", help="Raster resampling method.")
    ] = "nearest",
    max_transform_error: Annotated[
        float | None,
        typer.Option(
            "--max-transform-error",
            help="Refuse transformations less accurate than this, in metres.",
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report what would change without writing anything."),
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="Report rendering.")
    ] = OutputFormat.TABLE,
    summary_file: Annotated[
        Path | None, typer.Option("--summary-file", help="Append a markdown job summary to this file.")
    ] = None,
    report_file: Annotated[
        Path | None, typer.Option("--report-file", help="Write the full JSON report to this file.")
    ] = None,
    fail_on: Annotated[
        FailOn, typer.Option("--fail-on", help="Which findings cause a non-zero exit.")
    ] = FailOn.UNRESOLVABLE,
    comment_on_pr: Annotated[
        bool, typer.Option("--comment-on-pr/--no-comment-on-pr", help="Post the summary to the pull request.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging.")] = False,
) -> None:
    """Reproject datasets onto a single target CRS.

    Datasets already in the target CRS are left alone. Datasets carrying an
    unresolvable problem are never rewritten on a guess; they are reported and
    skipped. Exits 2 when anything changed, which makes ``--check`` usable as a
    drift gate in CI.
    """
    _configure_logging(verbose)
    console = Console()

    if resampling.lower() not in RESAMPLING_METHODS:
        _fail_usage(
            f"Unknown resampling method {resampling!r}. "
            f"Supported: {', '.join(sorted(RESAMPLING_METHODS))}."
        )

    try:
        options = NormalizeOptions(
            target_crs=target,
            assume_crs=assume_crs,
            max_transform_error=max_transform_error,
            output_dir=output_dir,
            resampling=resampling,
            dry_run=check,
        )
    except ValueError as exc:
        _fail_usage(str(exc))
        return

    report = normalize(paths, options)
    _emit(report, output_format, console)
    _write_side_outputs(report, summary_file, report_file, comment_on_pr)

    changed = bool(report.changes) if check else report.files_changed > 0
    raise typer.Exit(_verdict(report, fail_on, changed=changed))


# Typer derives command names from function names; register the intended names
# explicitly so the callables can keep unambiguous Python identifiers.
app.registered_commands[0].name = "scan"
app.registered_commands[1].name = "normalize"


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit code.

    Runs the Typer app with ``standalone_mode=False`` so that the process is
    never exited from underneath the caller. In that mode click *returns* the
    exit code carried by a :class:`typer.Exit` rather than raising it, and
    raises :class:`click.UsageError` for argument problems, so both paths are
    handled here.

    Args:
        argv: Argument vector, defaulting to :data:`sys.argv`.

    Returns:
        An :class:`ExitCode` value.
    """
    import click

    try:
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except click.UsageError as exc:
        Console(stderr=True).print(f"[bold red]Usage error:[/bold red] {exc.format_message()}")
        return int(ExitCode.USAGE)
    except click.Abort:
        return int(ExitCode.USAGE)

    return int(result) if isinstance(result, int) else int(ExitCode.CLEAN)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
