"""Tests for the JSON report to GitHub Action output bridge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crs_normalize.action import main, status_for, write_outputs
from crs_normalize.models import ExitCode


def parse_outputs(text: str) -> dict[str, str]:
    """Parse a ``$GITHUB_OUTPUT`` file, honouring the heredoc form."""
    values: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            values[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
        index += 1
    return values


class TestStatus:
    """Exit code to status mapping."""

    @pytest.mark.parametrize(
        ("exit_code", "expected"),
        [
            (ExitCode.CLEAN, "clean"),
            (ExitCode.FAILED, "failed"),
            (ExitCode.CHANGED, "changed"),
            (ExitCode.USAGE, "error"),
            (99, "error"),
        ],
    )
    def test_mapping(self, exit_code: int, expected: str) -> None:
        assert status_for(int(exit_code)) == expected


class TestWriteOutputs:
    """Writing the declared outputs."""

    def test_outputs_are_written(self, tmp_path: Path) -> None:
        output_file = tmp_path / "gh_output"
        report = {
            "files_scanned": 4,
            "files_changed": 2,
            "crs_histogram": {"EPSG:4326": 3, "EPSG:27700": 1},
        }
        write_outputs(report, int(ExitCode.CHANGED), output_file)
        values = parse_outputs(output_file.read_text(encoding="utf-8"))

        assert values["status"] == "changed"
        assert values["files-scanned"] == "4"
        assert values["files-changed"] == "2"
        assert json.loads(values["crs-histogram"]) == {"EPSG:4326": 3, "EPSG:27700": 1}

    def test_missing_output_file_is_not_fatal(self) -> None:
        outputs = write_outputs({"files_scanned": 1}, int(ExitCode.CLEAN), None)
        assert outputs["files-scanned"] == "1"

    def test_empty_report_yields_zero_defaults(self, tmp_path: Path) -> None:
        output_file = tmp_path / "gh_output"
        write_outputs({}, int(ExitCode.CLEAN), output_file)
        values = parse_outputs(output_file.read_text(encoding="utf-8"))
        assert values["files-scanned"] == "0"
        assert values["crs-histogram"] == "{}"

    def test_outputs_are_appended_to_existing_content(self, tmp_path: Path) -> None:
        output_file = tmp_path / "gh_output"
        output_file.write_text("earlier=value\n", encoding="utf-8")
        write_outputs({}, int(ExitCode.CLEAN), output_file)
        values = parse_outputs(output_file.read_text(encoding="utf-8"))
        assert values["earlier"] == "value"
        assert values["status"] == "clean"


class TestMain:
    """The module's command-line behaviour, as the entrypoint invokes it."""

    def test_main_reads_a_report_and_writes_outputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = tmp_path / "report.json"
        report.write_text(
            json.dumps({"files_scanned": 2, "files_changed": 0, "crs_histogram": {"EPSG:4326": 2}}),
            encoding="utf-8",
        )
        output_file = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

        assert main(["--report", str(report), "--exit-code", "0"]) == 0
        values = parse_outputs(output_file.read_text(encoding="utf-8"))
        assert values["status"] == "clean"
        assert values["files-scanned"] == "2"
        assert values["report-path"] == str(report)

    def test_main_survives_an_unreadable_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output_file = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

        # A failed run may never have produced a report; outputs must still exist
        # so downstream steps referencing them do not break.
        assert main(["--report", str(tmp_path / "absent.json"), "--exit-code", "3"]) == 0
        values = parse_outputs(output_file.read_text(encoding="utf-8"))
        assert values["status"] == "error"
        assert values["files-scanned"] == "0"

    def test_main_without_github_output_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"files_scanned": 1}), encoding="utf-8")
        assert main(["--report", str(report), "--exit-code", "0"]) == 0
