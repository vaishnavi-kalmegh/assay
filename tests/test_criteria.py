"""Structural validation for machine-readable acceptance criteria."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = {
    "id",
    "method",
    "path",
    "field_rule",
    "expected_status",
    "description",
}


def test_criteria_have_unique_ids_and_required_structure() -> None:
    document = yaml.safe_load((ROOT / "criteria.yaml").read_text(encoding="utf-8"))
    criteria = document["criteria"]

    assert len(criteria) == 18
    assert len({item["id"] for item in criteria}) == len(criteria)
    for item in criteria:
        assert REQUIRED_FIELDS <= item.keys()
        assert item["method"] in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        assert item["path"].startswith("/")
        assert isinstance(item["expected_status"], int)
        assert item["description"].strip()
        assert item["field_rule"].strip()
