"""Tests for CLI wiring: exit codes, output formats and side outputs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pytest

from crs_normalize.cli import main
from crs_normalize.models import ExitCode

from .conftest import write_vector


def run(*args: str) -> int:
    """Invoke the CLI with ``args`` and return its exit code."""
    return main(list(args))


class TestExitCodes:
    """Exit codes are the contract CI depends on."""

    def test_clean_scan_exits_zero(self, vector_4326: Path) -> None:
        assert run("scan", str(vector_4326)) == ExitCode.CLEAN

    def test_missing_crs_exits_one(self, vector_no_crs: Path) -> None:
        assert run("scan", str(vector_no_crs)) == ExitCode.FAILED

    def test_fail_on_never_exits_zero_despite_errors(self, vector_no_crs: Path) -> None:
        assert run("scan", str(vector_no_crs), "--fail-on", "never") == ExitCode.CLEAN

    def test_mixed_crs_passes_under_the_unresolvable_policy(
        self, vector_4326: Path, vector_27700: Path
    ) -> None:
        # Mixed CRS is resolvable by reprojecting, so the default policy allows it.
        assert run("scan", str(vector_4326.parent)) == ExitCode.CLEAN

    def test_mixed_crs_fails_under_the_mixed_policy(
        self, vector_4326: Path, vector_27700: Path
    ) -> None:
        assert run("scan", str(vector_4326.parent), "--fail-on", "mixed") == ExitCode.FAILED

    def test_normalize_exits_two_when_it_changes_files(self, vector_4326: Path, tmp_path: Path) -> None:
        code = run(
            "normalize", str(vector_4326), "--target", "EPSG:3857", "--output-dir", str(tmp_path / "out")
        )
        assert code == ExitCode.CHANGED

    def test_normalize_exits_zero_when_nothing_needs_changing(self, vector_4326: Path) -> None:
        assert run("normalize", str(vector_4326), "--target", "EPSG:4326") == ExitCode.CLEAN

    def test_check_mode_exits_two_when_changes_are_pending(self, vector_4326: Path) -> None:
        code = run("normalize", str(vector_4326), "--target", "EPSG:3857", "--check")
        assert code == ExitCode.CHANGED
        # Nothing may have been written.
        assert gpd.read_file(vector_4326).crs.to_epsg() == 4326

    def test_unresolvable_beats_changed(self, tmp_path: Path) -> None:
        write_vector(tmp_path / "d" / "ok.gpkg", "EPSG:4326")
        write_vector(tmp_path / "d" / "bad.gpkg", None)
        code = run("normalize", str(tmp_path / "d"), "--target", "EPSG:3857")
        assert code == ExitCode.FAILED

    def test_bad_crs_is_a_usage_error(self, vector_4326: Path) -> None:
        assert run("scan", str(vector_4326), "--target", "EPSG:nonsense") == ExitCode.USAGE

    def test_bad_resampling_method_is_a_usage_error(self, vector_4326: Path) -> None:
        code = run(
            "normalize", str(vector_4326), "--target", "EPSG:3857", "--resampling", "telepathy"
        )
        assert code == ExitCode.USAGE

    def test_negative_transform_error_is_a_usage_error(self, vector_4326: Path) -> None:
        code = run("scan", str(vector_4326), "--max-transform-error", "-1")
        assert code == ExitCode.USAGE


class TestOutputFormats:
    """Rendering selected by ``--format``."""

    def test_json_format_is_valid_json(self, vector_4326: Path, capsys: pytest.CaptureFixture) -> None:
        run("scan", str(vector_4326), "--format", "json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["files_scanned"] == 1
        assert payload["crs_histogram"] == {"EPSG:4326": 1}

    def test_github_format_emits_workflow_commands(
        self, vector_no_crs: Path, capsys: pytest.CaptureFixture
    ) -> None:
        run("scan", str(vector_no_crs), "--format", "github")
        out = capsys.readouterr().out
        assert out.startswith("::error file=")
        assert ",line=1,title=CRS001_MISSING_CRS::" in out

    def test_github_format_is_silent_for_a_clean_scan(
        self, vector_4326: Path, capsys: pytest.CaptureFixture
    ) -> None:
        run("scan", str(vector_4326), "--format", "github")
        assert capsys.readouterr().out == ""

    def test_table_format_prints_a_histogram(
        self, vector_4326: Path, capsys: pytest.CaptureFixture
    ) -> None:
        run("scan", str(vector_4326))
        out = capsys.readouterr().out
        assert "CRS distribution" in out
        assert "EPSG:4326" in out


class TestSideOutputs:
    """Summary and report files written for the Action to consume."""

    def test_summary_file_is_written(self, vector_4326: Path, tmp_path: Path) -> None:
        summary = tmp_path / "summary.md"
        run("scan", str(vector_4326), "--summary-file", str(summary))
        assert "## CRS normalization" in summary.read_text(encoding="utf-8")

    def test_summary_file_is_appended_not_truncated(self, vector_4326: Path, tmp_path: Path) -> None:
        # GITHUB_STEP_SUMMARY accumulates across steps, so the tool must append.
        summary = tmp_path / "summary.md"
        summary.write_text("earlier step\n", encoding="utf-8")
        run("scan", str(vector_4326), "--summary-file", str(summary))
        assert summary.read_text(encoding="utf-8").startswith("earlier step\n")

    def test_report_file_contains_the_json_report(self, vector_4326: Path, tmp_path: Path) -> None:
        report_file = tmp_path / "nested" / "report.json"
        run("scan", str(vector_4326), "--report-file", str(report_file))
        payload = json.loads(report_file.read_text(encoding="utf-8"))
        assert payload["files_scanned"] == 1

    def test_normalize_report_file_records_changes(self, vector_4326: Path, tmp_path: Path) -> None:
        report_file = tmp_path / "report.json"
        run(
            "normalize",
            str(vector_4326),
            "--target",
            "EPSG:3857",
            "--output-dir",
            str(tmp_path / "out"),
            "--report-file",
            str(report_file),
        )
        payload = json.loads(report_file.read_text(encoding="utf-8"))
        assert payload["files_changed"] == 1
        assert payload["changes"][0]["target_crs"] == "EPSG:3857"


def test_module_entry_point_runs_the_cli(vector_4326: Path) -> None:
    """``python -m crs_normalize`` must behave exactly like the console script."""
    completed = subprocess.run(
        [sys.executable, "-m", "crs_normalize", "scan", str(vector_4326), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == ExitCode.CLEAN
    assert json.loads(completed.stdout)["crs_histogram"] == {"EPSG:4326": 1}


def test_module_entry_point_propagates_failure_exit_codes(vector_no_crs: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "crs_normalize", "scan", str(vector_no_crs)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == ExitCode.FAILED
