"""Tests for dataset discovery and the pull request commenter."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crs_normalize.discovery import classify, discover
from crs_normalize.github import (
    COMMENT_MARKER,
    PullRequestCommenter,
    detect_pull_request_number,
    upsert_comment,
)
from crs_normalize.models import DatasetKind

from .conftest import write_raster, write_vector


class TestDiscovery:
    """Path, directory and glob expansion."""

    def test_classify_by_suffix(self) -> None:
        assert classify(Path("a.gpkg")) is DatasetKind.VECTOR
        assert classify(Path("a.SHP")) is DatasetKind.VECTOR
        assert classify(Path("a.tif")) is DatasetKind.RASTER
        assert classify(Path("a.txt")) is DatasetKind.UNKNOWN

    def test_directories_are_walked_recursively(self, tmp_path: Path) -> None:
        write_vector(tmp_path / "a" / "one.gpkg", "EPSG:4326")
        write_raster(tmp_path / "a" / "deep" / "two.tif", "EPSG:4326")
        found = discover([str(tmp_path)])
        assert {p.name for p in found} == {"one.gpkg", "two.tif"}

    def test_globs_are_expanded(self, tmp_path: Path) -> None:
        write_vector(tmp_path / "one.gpkg", "EPSG:4326")
        write_raster(tmp_path / "two.tif", "EPSG:4326")
        found = discover([str(tmp_path / "*.tif")])
        assert [p.name for p in found] == ["two.tif"]

    def test_recursive_globs_are_expanded(self, tmp_path: Path) -> None:
        write_vector(tmp_path / "a" / "b" / "deep.gpkg", "EPSG:4326")
        found = discover([str(tmp_path / "**" / "*.gpkg")])
        assert [p.name for p in found] == ["deep.gpkg"]

    def test_shapefile_sidecars_are_not_separate_datasets(self, tmp_path: Path) -> None:
        write_vector(tmp_path / "pts.shp", "EPSG:4326")
        found = discover([str(tmp_path)])
        assert [p.name for p in found] == ["pts.shp"]

    def test_unrecognised_files_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "data.csv").write_text("a,b", encoding="utf-8")
        assert discover([str(tmp_path)]) == []

    def test_results_are_deduplicated_across_overlapping_patterns(self, tmp_path: Path) -> None:
        target = write_vector(tmp_path / "one.gpkg", "EPSG:4326")
        found = discover([str(tmp_path), str(target), str(tmp_path / "*.gpkg")])
        assert found == [target]

    def test_missing_paths_yield_nothing(self, tmp_path: Path) -> None:
        assert discover([str(tmp_path / "absent")]) == []


class TestPullRequestDetection:
    """Reading the pull request number out of the event payload."""

    def test_pull_request_event(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")
        assert detect_pull_request_number(str(event)) == 42

    def test_issue_comment_event(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"issue": {"number": 7}}), encoding="utf-8")
        assert detect_pull_request_number(str(event)) == 7

    def test_push_event_has_no_pull_request(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text(json.dumps({"ref": "refs/heads/main"}), encoding="utf-8")
        assert detect_pull_request_number(str(event)) is None

    def test_absent_event_file(self, tmp_path: Path) -> None:
        assert detect_pull_request_number(str(tmp_path / "nope.json")) is None

    def test_malformed_event_file(self, tmp_path: Path) -> None:
        event = tmp_path / "event.json"
        event.write_text("{not json", encoding="utf-8")
        assert detect_pull_request_number(str(event)) is None


def make_client(handler) -> httpx.Client:
    """Return a client backed by a mock transport running ``handler``."""
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestPullRequestCommenter:
    """Creating and updating the sticky report comment."""

    def test_creates_a_comment_when_none_exists(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)["body"]
            return httpx.Response(201, json={"id": 1})

        commenter = PullRequestCommenter("tok", repository="o/r", client=make_client(handler))
        assert commenter.upsert(5, "hello") is True
        assert seen["method"] == "POST"
        assert str(seen["url"]).endswith("/repos/o/r/issues/5/comments")
        assert str(seen["body"]).startswith(COMMENT_MARKER)

    def test_updates_the_existing_comment(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 99, "body": f"{COMMENT_MARKER}\nold"}])
            seen["method"] = request.method
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"id": 99})

        commenter = PullRequestCommenter("tok", repository="o/r", client=make_client(handler))
        assert commenter.upsert(5, "new body") is True
        assert seen["method"] == "PATCH"
        assert str(seen["url"]).endswith("/repos/o/r/issues/comments/99")

    def test_unrelated_comments_are_ignored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 3, "body": "a human wrote this"}])
            return httpx.Response(201, json={"id": 4})

        commenter = PullRequestCommenter("tok", repository="o/r", client=make_client(handler))
        assert commenter.upsert(5, "body") is True

    def test_permission_failure_degrades_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(403, json={"message": "Resource not accessible"})

        commenter = PullRequestCommenter("tok", repository="o/r", client=make_client(handler))
        assert commenter.upsert(5, "body") is False

    def test_network_failure_degrades_gracefully(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        commenter = PullRequestCommenter("tok", repository="o/r", client=make_client(handler))
        assert commenter.upsert(5, "body") is False

    def test_missing_repository_slug_is_a_no_op(self) -> None:
        commenter = PullRequestCommenter("tok", repository="")
        assert commenter.upsert(5, "body") is False

    def test_upsert_comment_without_a_token_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert upsert_comment("body") is False

    def test_upsert_comment_outside_a_pull_request_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
        assert upsert_comment("body") is False
