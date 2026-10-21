"""Tests for criteria-driven acceptance test generation."""

import json
from pathlib import Path

import pytest

from assay.acceptance import export_acceptance_tests, run_acceptance
from assay.mapping import MappingError

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "specs" / "openapi.json"
CRITERIA = ROOT / "criteria.yaml"


def _tree(path: Path) -> dict[str, bytes]:
    return {
        item.name: item.read_bytes()
        for item in sorted(path.iterdir())
        if item.is_file() and item.name != "junit.xml"
    }


def test_export_creates_one_explicit_test_for_each_requirement(tmp_path: Path) -> None:
    output = tmp_path / "generated"
    manifest = export_acceptance_tests(SPEC, CRITERIA, output)
    source = (output / "test_acceptance.py").read_text(encoding="utf-8")

    assert len(manifest) == 18
    assert [item["requirement_id"] for item in manifest] == [
        f"REQ-{number:03d}" for number in range(1, 19)
    ]
    assert source.count("@pytest.mark.req(") == 18
    assert 'assert response.json()["status"] == "ok"' in source
    assert 'assert response.json()["id"] == product_id' in source
    assert "assert response.status_code == 404" in source
    assert (output / "manifest.json").exists()
    assert (output / "conftest.py").exists()


def test_generated_acceptance_suite_passes_all_criteria(tmp_path: Path) -> None:
    report = run_acceptance(
        SPEC, CRITERIA, tmp_path / "generated", tmp_path / "results.json"
    )

    assert report["summary"] == {"total": 18, "passed": 18}
    assert {item["requirement_id"] for item in report["results"]} == {
        f"REQ-{number:03d}" for number in range(1, 19)
    }
    assert all(item["outcome"] == "passed" for item in report["results"])
    assert json.loads((tmp_path / "results.json").read_text("utf-8")) == report


def test_export_is_deterministic(tmp_path: Path) -> None:
    export_acceptance_tests(SPEC, CRITERIA, tmp_path / "first")
    export_acceptance_tests(SPEC, CRITERIA, tmp_path / "second")

    assert _tree(tmp_path / "first") == _tree(tmp_path / "second")


def test_export_refuses_to_overwrite_unowned_directory(tmp_path: Path) -> None:
    output = tmp_path / "keep"
    output.mkdir()
    note = output / "notes.txt"
    note.write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="not created by assay"):
        export_acceptance_tests(SPEC, CRITERIA, output)

    assert note.read_text(encoding="utf-8") == "keep me"


def test_export_rejects_criteria_for_an_unknown_operation(tmp_path: Path) -> None:
    source = CRITERIA.read_text(encoding="utf-8").replace(
        "path: /health", "path: /missing", 1
    )
    criteria = tmp_path / "criteria.yaml"
    criteria.write_text(source, encoding="utf-8")

    with pytest.raises(MappingError, match="unknown OpenAPI path"):
        export_acceptance_tests(SPEC, criteria, tmp_path / "generated")


def test_export_rejects_duplicate_requirement_ids(tmp_path: Path) -> None:
    source = CRITERIA.read_text(encoding="utf-8")
    duplicate = source.replace("- id: REQ-002", "- id: REQ-001", 1)
    criteria = tmp_path / "criteria.yaml"
    criteria.write_text(duplicate, encoding="utf-8")

    with pytest.raises(MappingError, match="duplicate requirement ID"):
        export_acceptance_tests(SPEC, criteria, tmp_path / "generated")
