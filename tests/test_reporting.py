"""Tests for the JSON, GitHub annotation and markdown summary formatters."""

from __future__ import annotations

import json
from pathlib import Path

from crs_normalize import (
    Code,
    DatasetInfo,
    DatasetKind,
    Finding,
    Report,
    Severity,
    format_annotation,
    render_github,
    render_json,
    render_markdown_summary,
)
from crs_normalize.models import FileChange, TransformReport


def make_finding(**overrides) -> Finding:
    """Build a finding with simple defaults, overridable per test."""
    defaults = {
        "code": Code.MISSING_CRS,
        "severity": Severity.ERROR,
        "path": Path("data/a.gpkg"),
        "message": "No CRS.",
    }
    return Finding(**{**defaults, **overrides})


class TestAnnotations:
    """The exact wire format of GitHub Actions workflow commands."""

    def test_error_annotation_shape(self) -> None:
        finding = make_finding(message="No CRS here.")
        rendered = format_annotation(finding)
        assert rendered.startswith("::error file=data/a.gpkg,line=1,title=CRS001_MISSING_CRS::")
        assert "No CRS here." in rendered

    def test_warning_annotation_uses_the_warning_level(self) -> None:
        finding = make_finding(severity=Severity.WARNING, code=Code.AXIS_ORDER)
        rendered = format_annotation(finding)
        assert rendered.startswith("::warning file=data/a.gpkg,line=1,title=CRS010_AXIS_ORDER::")

    def test_notice_annotation_uses_the_notice_level(self) -> None:
        rendered = format_annotation(make_finding(severity=Severity.NOTICE))
        assert rendered.startswith("::notice ")

    def test_annotation_is_a_single_line(self) -> None:
        rendered = format_annotation(make_finding(message="line one\nline two"))
        assert "\n" not in rendered
        assert "%0A" in rendered

    def test_message_escapes_percent_and_newlines(self) -> None:
        rendered = format_annotation(make_finding(message="50% done\r\nnext"))
        body = rendered.split("::", 2)[2]
        assert "50%25 done%0D%0Anext" in body

    def test_property_values_escape_colons_and_commas(self) -> None:
        finding = make_finding(path=Path("data/odd:name,with.gpkg"))
        rendered = format_annotation(finding)
        assert "file=data/odd%3Aname%2Cwith.gpkg," in rendered
        # The escaped separators must not leak into the property list.
        properties = rendered.split("::", 2)[1]
        assert properties.count(",") == 2

    def test_annotation_carries_the_remedy_and_doc_link(self) -> None:
        rendered = format_annotation(make_finding())
        assert "--assume-crs" in rendered
        assert (
            "https%3A//www.geospatial-etl.com" in rendered
            or "https://www.geospatial-etl.com" in rendered
        )

    def test_codes_without_a_doc_link_get_no_see_clause(self) -> None:
        rendered = format_annotation(make_finding(code=Code.ROTATED_RASTER))
        assert "See http" not in rendered

    def test_render_github_emits_one_line_per_finding(self) -> None:
        report = Report(findings=[make_finding(), make_finding(code=Code.MIXED_CRS)])
        lines = render_github(report).splitlines()
        assert len(lines) == 2
        assert all(line.startswith("::error ") for line in lines)

    def test_render_github_of_a_clean_report_is_empty(self) -> None:
        assert render_github(Report()) == ""


class TestJson:
    """The JSON report consumed by the Action's outputs."""

    def test_json_report_carries_aggregates_and_records(self) -> None:
        report = Report(
            mode="fix",
            target_crs="EPSG:3857",
            datasets=[
                DatasetInfo(path=Path("a.gpkg"), kind=DatasetKind.VECTOR, crs="EPSG:4326"),
                DatasetInfo(path=Path("b.tif"), kind=DatasetKind.RASTER, crs="EPSG:4326"),
                DatasetInfo(path=Path("c.gpkg"), kind=DatasetKind.VECTOR, crs=None),
            ],
            findings=[make_finding()],
        )
        payload = json.loads(render_json(report))

        assert payload["mode"] == "fix"
        assert payload["target_crs"] == "EPSG:3857"
        assert payload["files_scanned"] == 3
        assert payload["files_changed"] == 0
        assert payload["crs_histogram"] == {"EPSG:4326": 2, "<none>": 1}
        assert len(payload["datasets"]) == 3
        assert payload["findings"][0]["code"] == "CRS001_MISSING_CRS"

    def test_findings_serialise_their_remedy_and_link(self) -> None:
        payload = json.loads(render_json(Report(findings=[make_finding()])))
        finding = payload["findings"][0]
        assert finding["remedy"]
        assert finding["help_url"].startswith("https://www.geospatial-etl.com/")

    def test_json_is_stable_and_parseable_for_an_empty_report(self) -> None:
        payload = json.loads(render_json(Report()))
        assert payload["files_scanned"] == 0
        assert payload["crs_histogram"] == {}


class TestMarkdownSummary:
    """The job summary written to GITHUB_STEP_SUMMARY."""

    def test_clean_summary_states_no_problems(self) -> None:
        report = Report(datasets=[DatasetInfo(path=Path("a.gpkg"), kind=DatasetKind.VECTOR, crs="EPSG:4326")])
        markdown = render_markdown_summary(report)
        assert "## CRS normalization" in markdown
        assert "No CRS problems found" in markdown

    def test_summary_counts_errors(self) -> None:
        report = Report(
            datasets=[DatasetInfo(path=Path("a.gpkg"), kind=DatasetKind.VECTOR)],
            findings=[make_finding()],
        )
        assert "**1 unresolved CRS problem(s)**" in render_markdown_summary(report)

    def test_summary_tabulates_transform_and_accuracy(self) -> None:
        report = Report(
            mode="fix",
            target_crs="EPSG:3857",
            datasets=[DatasetInfo(path=Path("a.gpkg"), kind=DatasetKind.VECTOR, crs="EPSG:4326")],
            changes=[
                FileChange(
                    path=Path("a.gpkg"),
                    output_path=Path("out/a.gpkg"),
                    source_crs="EPSG:4326",
                    target_crs="EPSG:3857",
                    transform=TransformReport(
                        source_crs="EPSG:4326",
                        target_crs="EPSG:3857",
                        name="Popular Visualisation Pseudo-Mercator",
                        accuracy_m=0.0,
                    ),
                    written=True,
                )
            ],
        )
        markdown = render_markdown_summary(report)
        assert "| File | Kind | Source CRS | Target CRS | Transformation | Accuracy |" in markdown
        assert "Popular Visualisation Pseudo-Mercator" in markdown
        assert "exact" in markdown
        assert "1 dataset(s) reprojected to `EPSG:3857`" in markdown

    def test_summary_lists_findings_with_remedies(self) -> None:
        markdown = render_markdown_summary(Report(findings=[make_finding()]))
        assert "CRS001_MISSING_CRS" in markdown
        assert "--assume-crs" in markdown

    def test_pipes_in_values_cannot_break_the_table(self) -> None:
        report = Report(datasets=[DatasetInfo(path=Path("we|ird.gpkg"), kind=DatasetKind.VECTOR)])
        assert "we\\|ird.gpkg" in render_markdown_summary(report)


class TestAccuracyLabels:
    """Human-readable accuracy rendering."""

    def test_unknown_accuracy(self) -> None:
        report = TransformReport(source_crs="a", target_crs="b", accuracy_m=None)
        assert report.accuracy_label == "unknown"

    def test_zero_accuracy_is_exact(self) -> None:
        report = TransformReport(source_crs="a", target_crs="b", accuracy_m=0.0)
        assert report.accuracy_label == "exact"

    def test_metre_accuracy_is_formatted(self) -> None:
        report = TransformReport(source_crs="a", target_crs="b", accuracy_m=2.5)
        assert report.accuracy_label == "2.5 m"

    def test_unavailable_transform_reports_not_applicable(self) -> None:
        report = TransformReport(source_crs="a", target_crs="b", available=False)
        assert report.accuracy_label == "n/a"
