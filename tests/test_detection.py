"""Tests for CRS detection: missing, mixed, implausible and sidecar problems."""

from __future__ import annotations

from pathlib import Path

import pytest
from affine import Affine
from shapely.geometry import Point

from crs_normalize import Code, ScanOptions, Severity, scan
from crs_normalize.crs_utils import (
    assess_transform,
    check_coordinate_plausibility,
    parse_crs,
    plausible_bounds,
)

from .conftest import write_raster, write_vector


def codes(report) -> set[Code]:
    """Return the set of codes present in a report."""
    return {f.code for f in report.findings}


def test_scan_reports_crs_histogram(vector_4326: Path, vector_27700: Path) -> None:
    report = scan([str(vector_4326.parent)])
    assert report.files_scanned == 2
    assert report.crs_histogram() == {"EPSG:27700": 1, "EPSG:4326": 1}


def test_missing_crs_is_an_error_without_assume_crs(vector_no_crs: Path) -> None:
    report = scan([str(vector_no_crs)])
    missing = [f for f in report.findings if f.code is Code.MISSING_CRS]
    assert len(missing) == 1
    assert missing[0].severity is Severity.ERROR
    assert "will not guess" in missing[0].message


def test_missing_crs_downgrades_to_warning_with_assume_crs(vector_no_crs: Path) -> None:
    report = scan([str(vector_no_crs)], ScanOptions(assume_crs="EPSG:4326"))
    missing = [f for f in report.findings if f.code is Code.MISSING_CRS]
    assert len(missing) == 1
    assert missing[0].severity is Severity.WARNING
    assert missing[0].detail["assumed_crs"] == "EPSG:4326"


def test_mixed_crs_flags_the_minority_datasets(
    vector_4326: Path, vector_27700: Path, polygon_4326: Path
) -> None:
    report = scan([str(vector_4326.parent)])
    mixed = [f for f in report.findings if f.code is Code.MIXED_CRS]
    # EPSG:4326 is dominant (2 datasets), so only the 27700 dataset is flagged.
    assert [f.path for f in mixed] == [vector_27700]
    assert mixed[0].detail["dominant_crs"] == "EPSG:4326"


def test_single_crs_produces_no_mixed_finding(vector_4326: Path, polygon_4326: Path) -> None:
    report = scan([str(vector_4326.parent)])
    assert Code.MIXED_CRS not in codes(report)


def test_target_crs_decides_which_datasets_are_outliers(
    vector_4326: Path, vector_27700: Path, polygon_4326: Path
) -> None:
    # With an explicit target, the majority CRS no longer wins: everything that
    # is not the target is an outlier.
    report = scan([str(vector_4326.parent)], ScanOptions(target_crs="EPSG:27700"))
    mixed = [f for f in report.findings if f.code is Code.MIXED_CRS]
    assert {f.path for f in mixed} == {vector_4326, polygon_4326}


def test_degree_coordinates_in_a_projected_crs_are_implausible(tmp_path: Path) -> None:
    # Lon/lat degrees mislabelled as British National Grid metres.
    path = write_vector(
        tmp_path / "bad.gpkg", "EPSG:27700", geometries=[Point(1.0, 51.0), Point(1.5, 51.5)]
    )
    report = scan([str(path)])
    findings = [f for f in report.findings if f.code is Code.IMPLAUSIBLE_COORDINATES]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].detail["reason"] == "degree_magnitude_in_projected_crs"


def test_metre_coordinates_in_epsg_4326_are_implausible(tmp_path: Path) -> None:
    path = write_vector(
        tmp_path / "bad4326.gpkg",
        "EPSG:4326",
        geometries=[Point(530000, 180000), Point(531000, 181000)],
    )
    report = scan([str(path)])
    findings = [f for f in report.findings if f.code is Code.IMPLAUSIBLE_COORDINATES]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "labelled as degrees" in findings[0].message


def test_valid_projected_coordinates_are_not_flagged(vector_27700: Path) -> None:
    report = scan([str(vector_27700)])
    assert Code.IMPLAUSIBLE_COORDINATES not in codes(report)


def test_plausibility_check_is_skipped_without_bounds() -> None:
    assert check_coordinate_plausibility(Path("x.gpkg"), parse_crs("EPSG:27700"), None) == []


def test_plausible_bounds_for_projected_crs_are_metre_scale() -> None:
    bounds = plausible_bounds(parse_crs("EPSG:27700"))
    assert bounds is not None
    # British National Grid eastings/northings run to hundreds of kilometres.
    assert bounds[2] > 100_000


def test_plausible_bounds_for_geographic_crs_is_the_degree_envelope() -> None:
    assert plausible_bounds(parse_crs("EPSG:4326")) == (-180.0, -90.0, 180.0, 90.0)


def test_rotated_raster_is_an_error(tmp_path: Path) -> None:
    rotated = Affine(0.01, 0.003, 1.0, 0.002, -0.01, 51.0)
    path = write_raster(tmp_path / "rot.tif", "EPSG:4326", transform=rotated)
    report = scan([str(path)])
    findings = [f for f in report.findings if f.code is Code.ROTATED_RASTER]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_south_up_raster_is_a_warning(tmp_path: Path) -> None:
    south_up = Affine(0.01, 0.0, 1.0, 0.0, 0.01, 51.0)
    path = write_raster(tmp_path / "southup.tif", "EPSG:4326", transform=south_up)
    report = scan([str(path)])
    findings = [f for f in report.findings if f.code is Code.ROTATED_RASTER]
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING


def test_north_up_raster_is_clean(raster_4326: Path) -> None:
    report = scan([str(raster_4326)])
    assert Code.ROTATED_RASTER not in codes(report)


def test_sidecar_disagreeing_with_embedded_crs_is_an_error(raster_4326: Path) -> None:
    raster_4326.with_suffix(".prj").write_text(parse_crs("EPSG:27700").to_wkt(), encoding="utf-8")
    report = scan([str(raster_4326)])
    findings = [f for f in report.findings if f.code is Code.SIDECAR_MISMATCH]
    assert len(findings) == 1
    assert findings[0].detail["sidecar_crs"] == "EPSG:27700"
    assert findings[0].detail["embedded_crs"] == "EPSG:4326"


def test_agreeing_sidecar_is_not_flagged(raster_4326: Path) -> None:
    raster_4326.with_suffix(".prj").write_text(parse_crs("EPSG:4326").to_wkt(), encoding="utf-8")
    report = scan([str(raster_4326)])
    assert Code.SIDECAR_MISMATCH not in codes(report)


def test_shapefile_prj_is_never_treated_as_a_conflicting_sidecar(tmp_path: Path) -> None:
    # A shapefile's .prj *is* its CRS, so it can never contradict itself.
    path = write_vector(tmp_path / "shp" / "pts.shp", "EPSG:4326")
    report = scan([str(path)])
    assert Code.SIDECAR_MISMATCH not in codes(report)


def test_unreadable_dataset_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.gpkg"
    broken.write_bytes(b"this is not a geopackage")
    report = scan([str(broken)])
    findings = [f for f in report.findings if f.code is Code.UNREADABLE]
    assert len(findings) == 1
    assert report.datasets[0].readable is False


def test_unreadable_dataset_is_excluded_from_the_histogram(tmp_path: Path, vector_4326: Path) -> None:
    broken = vector_4326.parent / "broken.gpkg"
    broken.write_bytes(b"nope")
    report = scan([str(vector_4326.parent)])
    assert report.crs_histogram() == {"EPSG:4326": 1}


class TestTransformAvailability:
    """Transform reachability and accuracy, straight from PROJ."""

    def test_identity_transform_is_exact(self) -> None:
        report = assess_transform(parse_crs("EPSG:4326"), parse_crs("EPSG:4326"))
        assert report.available is True
        assert report.accuracy_m == 0.0
        assert report.accuracy_label == "exact"

    def test_ordinary_transform_is_available(self) -> None:
        report = assess_transform(parse_crs("EPSG:4326"), parse_crs("EPSG:3857"))
        assert report.available is True
        assert report.best_available is True
        assert report.name

    def test_engineering_crs_has_no_transform(self) -> None:
        engineering = parse_crs(
            'ENGCRS["unreferenced",EDATUM["unknown"],CS[Cartesian,2],'
            'AXIS["x",east,ORDER[1],LENGTHUNIT["metre",1]],'
            'AXIS["y",north,ORDER[2],LENGTHUNIT["metre",1]]]'
        )
        report = assess_transform(engineering, parse_crs("EPSG:4326"))
        assert report.available is False
        assert report.accuracy_label == "n/a"

    def test_unreachable_target_raises_no_transform_finding(self, tmp_path: Path) -> None:
        engineering_wkt = (
            'ENGCRS["unreferenced",EDATUM["unknown"],CS[Cartesian,2],'
            'AXIS["x",east,ORDER[1],LENGTHUNIT["metre",1]],'
            'AXIS["y",north,ORDER[2],LENGTHUNIT["metre",1]]]'
        )
        path = write_vector(
            tmp_path / "eng.gpkg",
            engineering_wkt,
            geometries=[Point(10.0, 20.0), Point(11.0, 21.0)],
        )
        report = scan([str(path)], ScanOptions(target_crs="EPSG:4326"))
        assert Code.NO_TRANSFORM in codes(report)

    def test_accuracy_limit_flags_a_datum_shift(self, vector_27700: Path) -> None:
        # OSGB36 to WGS 84 without the OSTN15 grid is accurate to a few metres,
        # comfortably worse than a 0.001 m limit.
        report = scan(
            [str(vector_27700)],
            ScanOptions(target_crs="EPSG:4326", max_transform_error=0.001),
        )
        findings = [f for f in report.findings if f.code is Code.TRANSFORM_ACCURACY]
        assert findings, "expected the coarse datum shift to breach a 1 mm limit"
        assert findings[0].detail["accuracy_m"] > 0.001

    def test_generous_accuracy_limit_passes(self, vector_27700: Path) -> None:
        report = scan(
            [str(vector_27700)],
            ScanOptions(target_crs="EPSG:4326", max_transform_error=1000.0),
        )
        assert Code.TRANSFORM_ACCURACY not in codes(report)

    def test_missing_datum_grid_is_reported(self, tmp_path: Path) -> None:
        source = assess_transform(parse_crs("EPSG:4277"), parse_crs("EPSG:4326"))
        if source.best_available:
            pytest.skip("The OSTN15 datum-shift grid is installed in this environment.")
        path = write_vector(
            tmp_path / "osgb36.gpkg",
            "EPSG:4277",
            geometries=[Point(-1.0, 52.0), Point(-1.1, 52.1)],
        )
        report = scan([str(path)], ScanOptions(target_crs="EPSG:4326"))
        findings = [f for f in report.findings if f.code is Code.GRID_UNAVAILABLE]
        assert findings
        assert findings[0].detail["missing_grids"]


def test_parse_crs_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="not a recognisable CRS"):
        parse_crs("EPSG:not-a-code")
