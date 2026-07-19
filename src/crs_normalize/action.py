"""Bridge between the CLI's JSON report and GitHub Action outputs.

The Docker entrypoint runs the CLI with ``--report-file`` and then invokes this
module to translate that report into the ``$GITHUB_OUTPUT`` key/value pairs
declared in ``action.yml``. Keeping the translation in Python rather than shell
means the JSON is parsed by a real parser and multi-line values are written
with correct heredoc delimiters.

Run as::

    python -m crs_normalize.action --report crs-report.json --exit-code 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from .models import ExitCode

__all__ = ["status_for", "write_outputs", "main"]

logger = logging.getLogger(__name__)


def status_for(exit_code: int) -> str:
    """Map a CLI exit code onto the Action's ``status`` output.

    Args:
        exit_code: Exit code returned by the CLI.

    Returns:
        One of ``"clean"``, ``"failed"``, ``"changed"`` or ``"error"``.
    """
    return {
        int(ExitCode.CLEAN): "clean",
        int(ExitCode.FAILED): "failed",
        int(ExitCode.CHANGED): "changed",
        int(ExitCode.USAGE): "error",
    }.get(exit_code, "error")


def _format_output(key: str, value: str) -> str:
    """Render one ``$GITHUB_OUTPUT`` entry.

    Single-line values use ``key=value``. Multi-line values use the heredoc
    form with a random delimiter, which is what GitHub requires and what stops
    report content from being able to forge additional outputs.
    """
    if "\n" not in value:
        return f"{key}={value}\n"
    delimiter = f"ghadelim_{secrets.token_hex(8)}"
    return f"{key}<<{delimiter}\n{value}\n{delimiter}\n"


def write_outputs(report: dict[str, Any], exit_code: int, output_file: Path | None) -> dict[str, str]:
    """Write the Action outputs derived from ``report``.

    Args:
        report: Parsed JSON report produced by the CLI.
        exit_code: Exit code the CLI returned.
        output_file: Path of ``$GITHUB_OUTPUT``. When ``None``, outputs are
            computed and returned but not written, which is what happens when
            the CLI is run outside Actions.

    Returns:
        The mapping of output names to values.
    """
    outputs = {
        "status": status_for(exit_code),
        "files-scanned": str(report.get("files_scanned", 0)),
        "files-changed": str(report.get("files_changed", 0)),
        "crs-histogram": json.dumps(report.get("crs_histogram", {}), sort_keys=True),
    }

    if output_file is not None:
        with output_file.open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(_format_output(key, value))
    else:
        logger.debug("No GITHUB_OUTPUT set; computed outputs but wrote nothing.")

    return outputs


def main(argv: list[str] | None = None) -> int:
    """Read a JSON report and emit Action outputs.

    Args:
        argv: Argument vector, defaulting to :data:`sys.argv`.

    Returns:
        ``0`` on success. Failure to read the report is not fatal: placeholder
        outputs are emitted so that downstream steps referencing them do not
        break on an unrelated error.
    """
    parser = argparse.ArgumentParser(description="Emit GitHub Action outputs from a CRS report.")
    parser.add_argument("--report", required=True, type=Path, help="Path to the JSON report.")
    parser.add_argument("--exit-code", required=True, type=int, help="Exit code the CLI returned.")
    parser.add_argument(
        "--report-output-name",
        default="report-path",
        help="Name of the output carrying the report path.",
    )
    args = parser.parse_args(argv)

    raw_output = os.environ.get("GITHUB_OUTPUT")
    output_file = Path(raw_output) if raw_output else None

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read report %s: %s", args.report, exc)
        report = {}

    write_outputs(report, args.exit_code, output_file)

    if output_file is not None:
        with output_file.open("a", encoding="utf-8") as handle:
            handle.write(_format_output(args.report_output_name, str(args.report)))

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
