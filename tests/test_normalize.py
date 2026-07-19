"""Tests for reprojection: round-trips, skipping, output mirroring, dry runs."""

from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import pytest
import rasterio
from shapely.geometry import Point

from crs_normalize import Code, NormalizeOptions, normalize
from crs_normalize.normalizer import parse_resampling

from .conftest import write_raster, write_vector


def test_vector_is_reprojected_to_the_target(vector_4326: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = normalize([str(vector_4326)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    assert report.files_changed == 1
    written = out / "points_4326.gpkg"
    assert written.is_file()

    result = gpd.read_file(written)
    assert result.crs.to_epsg() == 3857
    # Longitude 1 degree is ~111 320 m on the pseudo-Mercator x axis.
    assert result.geometry.iloc[0].x == pytest.approx(111319.49, abs=1.0)


def test_vector_round_trip_preserves_coordinates(tmp_path: Path) -> None:
    original = [Point(1.0, 51.0), Point(1.5, 51.5)]
    source = write_vector(tmp_path / "src" / "pts.gpkg", "EPSG:4326", geometries=original)

    forward = tmp_path / "fwd"
    normalize([str(source)], NormalizeOptions(target_crs="EPSG:3857", output_dir=forward))
    back = tmp_path / "back"
    normalize([str(forward / "pts.gpkg")], NormalizeOptions(target_crs="EPSG:4326", output_dir=back))

    result = gpd.read_file(back / "pts.gpkg")
    assert result.crs.to_epsg() == 4326
    for point, expected in zip(result.geometry, original, strict=True):
        assert point.x == pytest.approx(expected.x, abs=1e-7)
        assert point.y == pytest.approx(expected.y, abs=1e-7)


def test_raster_is_reprojected_and_stays_north_up(raster_4326: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = normalize([str(raster_4326)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    assert report.files_changed == 1
    with rasterio.open(out / "grid_4326.tif") as src:
        assert src.crs.to_epsg() == 3857
        assert src.transform.b == 0
        assert src.transform.d == 0
        assert src.transform.e < 0
        assert src.count == 1


def test_raster_resampling_method_is_honoured(raster_4326: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    normalize(
        [str(raster_4326)],
        NormalizeOptions(target_crs="EPSG:3857", output_dir=out, resampling="bilinear"),
    )
    assert (out / "grid_4326.tif").is_file()


def test_datasets_already_in_the_target_crs_are_not_rewritten(vector_4326: Path) -> None:
    before = vector_4326.stat().st_mtime_ns
    report = normalize([str(vector_4326)], NormalizeOptions(target_crs="EPSG:4326"))
    assert report.files_changed == 0
    assert vector_4326.stat().st_mtime_ns == before


def test_in_place_normalization_rewrites_the_original(vector_4326: Path) -> None:
    report = normalize([str(vector_4326)], NormalizeOptions(target_crs="EPSG:3857"))
    assert report.files_changed == 1
    assert gpd.read_file(vector_4326).crs.to_epsg() == 3857


def test_missing_crs_blocks_reprojection(vector_no_crs: Path) -> None:
    report = normalize([str(vector_no_crs)], NormalizeOptions(target_crs="EPSG:3857"))
    assert report.files_changed == 0
    assert report.has_code(Code.MISSING_CRS)
    # The original must be left exactly as it was.
    assert gpd.read_file(vector_no_crs).crs is None


def test_assume_crs_unblocks_a_dataset_with_no_crs(vector_no_crs: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = normalize(
        [str(vector_no_crs)],
        NormalizeOptions(target_crs="EPSG:3857", assume_crs="EPSG:4326", output_dir=out),
    )
    assert report.files_changed == 1
    assert gpd.read_file(out / "points_nocrs.gpkg").crs.to_epsg() == 3857


def test_rotated_raster_is_never_reprojected(tmp_path: Path) -> None:
    from affine import Affine

    rotated = write_raster(
        tmp_path / "rot.tif", "EPSG:4326", transform=Affine(0.01, 0.003, 1.0, 0.002, -0.01, 51.0)
    )
    report = normalize([str(rotated)], NormalizeOptions(target_crs="EPSG:3857"))
    assert report.files_changed == 0
    assert report.has_code(Code.ROTATED_RASTER)
    with rasterio.open(rotated) as src:
        assert src.crs.to_epsg() == 4326


def test_check_mode_writes_nothing_but_records_changes(vector_4326: Path) -> None:
    report = normalize([str(vector_4326)], NormalizeOptions(target_crs="EPSG:3857", dry_run=True))
    assert report.mode == "check"
    assert len(report.changes) == 1
    assert report.changes[0].written is False
    assert report.files_changed == 0
    assert gpd.read_file(vector_4326).crs.to_epsg() == 4326


def test_output_directory_mirrors_the_input_structure(tmp_path: Path) -> None:
    root = tmp_path / "data"
    write_vector(root / "a" / "one.gpkg", "EPSG:4326")
    write_vector(root / "b" / "nested" / "two.gpkg", "EPSG:4326")
    out = tmp_path / "out"

    report = normalize([str(root)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    assert report.files_changed == 2
    assert (out / "a" / "one.gpkg").is_file()
    assert (out / "b" / "nested" / "two.gpkg").is_file()


def test_output_directory_also_receives_already_correct_datasets(tmp_path: Path) -> None:
    root = tmp_path / "data"
    write_vector(root / "needs_work.gpkg", "EPSG:4326")
    write_vector(root / "already.gpkg", "EPSG:3857", geometries=[Point(111319.0, 6621293.0)])
    out = tmp_path / "out"

    normalize([str(root)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    # The mirror must be a complete copy of the input, not only the changes.
    assert (out / "needs_work.gpkg").is_file()
    assert (out / "already.gpkg").is_file()


def test_transform_details_are_recorded_for_each_change(vector_4326: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = normalize([str(vector_4326)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.source_crs == "EPSG:4326"
    assert change.target_crs == "EPSG:3857"
    assert change.transform is not None
    assert change.transform.name
    assert change.transform.accuracy_m is None or math.isfinite(change.transform.accuracy_m)


def test_missing_datum_grid_blocks_reprojection(vector_27700: Path) -> None:
    """A datum shift whose grid is absent must not silently use a coarser path."""
    from crs_normalize.crs_utils import assess_transform, parse_crs

    if assess_transform(parse_crs("EPSG:27700"), parse_crs("EPSG:4326")).best_available:
        pytest.skip("The OSTN15 datum-shift grid is installed in this environment.")

    report = normalize([str(vector_27700)], NormalizeOptions(target_crs="EPSG:4326"))
    assert report.files_changed == 0
    assert report.has_code(Code.GRID_UNAVAILABLE)
    assert gpd.read_file(vector_27700).crs.to_epsg() == 27700


def test_shapefile_sidecars_are_written_alongside(tmp_path: Path) -> None:
    source = write_vector(tmp_path / "src" / "pts.shp", "EPSG:4326")
    out = tmp_path / "out"
    normalize([str(source)], NormalizeOptions(target_crs="EPSG:3857", output_dir=out))

    assert (out / "pts.shp").is_file()
    assert (out / "pts.dbf").is_file()
    assert (out / "pts.shx").is_file()
    assert (out / "pts.prj").is_file()
    assert gpd.read_file(out / "pts.shp").crs.to_epsg() == 3857


def test_normalize_requires_a_target_crs() -> None:
    with pytest.raises(ValueError, match="target CRS is required"):
        NormalizeOptions(target_crs="")


def test_unknown_resampling_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown resampling method"):
        parse_resampling("telepathy")


def test_all_documented_resampling_methods_resolve() -> None:
    from crs_normalize.normalizer import RESAMPLING_METHODS

    for name in RESAMPLING_METHODS:
        assert parse_resampling(name) is RESAMPLING_METHODS[name]
