"""Tests for the offline OpenAPI schema baseline."""

import builtins
import copy
import json
from pathlib import Path

import pytest

from assay.baseline import (
    UnsupportedSchemaError,
    generate_cases,
    normalize_cases,
)

ROOT = Path(__file__).resolve().parents[1]


def load_spec() -> dict:
    return json.loads((ROOT / "specs" / "openapi.json").read_text(encoding="utf-8"))


def test_real_demo_spec_generates_cases_for_all_five_endpoints() -> None:
    cases = generate_cases(load_spec())
    endpoints = {(case.method, case.path) for case in cases}

    assert endpoints == {
        ("GET", "/health"),
        ("POST", "/products"),
        ("GET", "/products/{product_id}"),
        ("PATCH", "/products/{product_id}/stock"),
        ("POST", "/orders"),
    }


def test_benign_schema_metadata_is_ignored() -> None:
    document = copy.deepcopy(load_spec())
    name_schema = document["components"]["schemas"]["ProductCreate"][
        "properties"
    ]["name"]
    name_schema.update(
        {
            "default": "sample",
            "example": "sample name",
            "examples": ["sample", "another sample"],
            "description": "Metadata should not change input generation.",
        }
    )
    category_ref = document["components"]["schemas"]["ProductCreate"][
        "properties"
    ]["category"]
    category_ref.update({"title": "Category", "description": "Ignored ref metadata"})
    document["paths"]["/products"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"] = {"sample": {"value": {"name": "sample"}}}

    assert generate_cases(document)


def test_two_runs_produce_identical_normalized_cases() -> None:
    document = load_spec()
    first = normalize_cases(generate_cases(document))
    second = normalize_cases(generate_cases(document))

    assert first == second
    assert first == sorted(
        first,
        key=lambda case: (
            case["method"],
            case["path"],
            case["field"],
            case["kind"],
            case["name"],
            case["case_id"],
        ),
    )
    assert len({case["case_id"] for case in first}) == len(first)


def test_cases_cover_required_generation_categories() -> None:
    cases = normalize_cases(generate_cases(load_spec()))
    kinds = {case["kind"] for case in cases}

    assert {
        "valid",
        "boundary",
        "invalid_type",
        "missing_required",
        "invalid_enum",
    } <= kinds
    assert any(
        case["kind"] == "invalid_enum"
        and case["method"] == "POST"
        and case["path"] == "/products"
        and case["field"] == "body.category"
        for case in cases
    )
    assert any(
        case["kind"] == "missing_required"
        and case["field"] == "body.name"
        and "missing required field" in case["name"]
        for case in cases
    )


def test_case_names_include_method_path_field_and_test_description() -> None:
    for case in normalize_cases(generate_cases(load_spec())):
        assert case["method"] in case["name"]
        assert case["path"] in case["name"]
        assert case["field"] in case["name"]
        assert case["kind"] in {
            "valid",
            "boundary",
            "invalid_type",
            "missing_required",
            "invalid_enum",
        }
        assert case["name"].split("—")[-1].strip()


def test_string_boundaries_include_min_max_and_outside_values() -> None:
    cases = normalize_cases(generate_cases(load_spec()))
    product_names = [
        case for case in cases
        if case["path"] == "/products"
        and case["field"] == "body.name"
        and case["kind"] == "boundary"
    ]

    assert any(
        case["body"]["name"] == "xxx" and case["expected_valid"]
        for case in product_names
    )
    assert any(
        case["body"]["name"] == "xx" and not case["expected_valid"]
        for case in product_names
    )
    assert any(
        case["body"]["name"] == "x" * 40 and case["expected_valid"]
        for case in product_names
    )
    assert any(
        case["body"]["name"] == "x" * 41 and not case["expected_valid"]
        for case in product_names
    )


def test_numeric_boundaries_include_bounds_and_just_outside() -> None:
    cases = normalize_cases(generate_cases(load_spec()))
    prices = [
        case for case in cases
        if case["path"] == "/products"
        and case["field"] == "body.price"
        and case["kind"] == "boundary"
    ]

    assert any(
        case["body"]["price"] == 0 and not case["expected_valid"]
        for case in prices
    )
    assert any(
        case["body"]["price"] == -0.01 and not case["expected_valid"]
        for case in prices
    )
    assert any(
        case["body"]["price"] == 10000 and case["expected_valid"]
        for case in prices
    )
    assert any(
        case["body"]["price"] == 10000.01 and not case["expected_valid"]
        for case in prices
    )


def test_numeric_boundary_semantics_and_integer_step() -> None:
    document = {
        "openapi": "3.1.0",
        "info": {"title": "bounds test", "version": "1"},
        "paths": {
            "/bounds": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "exclusive_min",
                                        "inclusive_min",
                                        "inclusive_max",
                                        "integer_min",
                                        "exclusive_max",
                                    ],
                                    "properties": {
                                        "exclusive_min": {
                                            "type": "number",
                                            "exclusiveMinimum": 0,
                                        },
                                        "inclusive_min": {
                                            "type": "number",
                                            "minimum": 0,
                                        },
                                        "inclusive_max": {
                                            "type": "number",
                                            "maximum": 5,
                                        },
                                        "integer_min": {
                                            "type": "integer",
                                            "minimum": 2,
                                        },
                                        "exclusive_max": {
                                            "type": "number",
                                            "exclusiveMaximum": 5,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {},
                }
            }
        },
    }
    cases = normalize_cases(generate_cases(document))
    boundaries = {
        case_field: [
            case for case in cases
            if case["field"] == case_field and case["kind"] == "boundary"
        ]
        for case_field in (
            "body.exclusive_min",
            "body.inclusive_min",
            "body.inclusive_max",
            "body.integer_min",
            "body.exclusive_max",
        )
    }

    exclusive_min = boundaries["body.exclusive_min"]
    assert any(
        case["body"]["exclusive_min"] == 0 and not case["expected_valid"]
        for case in exclusive_min
    )
    assert any(
        case["body"]["exclusive_min"] == 0.01 and case["expected_valid"]
        and "just inside" in case["name"]
        for case in exclusive_min
    )
    assert any(
        case["body"]["exclusive_min"] == -0.01 and not case["expected_valid"]
        for case in exclusive_min
    )

    inclusive_min = boundaries["body.inclusive_min"]
    assert any(
        case["body"]["inclusive_min"] == 0 and case["expected_valid"]
        for case in inclusive_min
    )
    assert any(
        case["body"]["inclusive_min"] == -0.01 and not case["expected_valid"]
        for case in inclusive_min
    )

    inclusive_max = boundaries["body.inclusive_max"]
    assert any(
        case["body"]["inclusive_max"] == 5 and case["expected_valid"]
        for case in inclusive_max
    )
    assert any(
        case["body"]["inclusive_max"] == 5.01 and not case["expected_valid"]
        for case in inclusive_max
    )

    integer_min = boundaries["body.integer_min"]
    assert any(
        case["body"]["integer_min"] == 2 and case["expected_valid"]
        for case in integer_min
    )
    assert any(
        case["body"]["integer_min"] == 1 and not case["expected_valid"]
        for case in integer_min
    )

    exclusive_max = boundaries["body.exclusive_max"]
    assert any(
        case["body"]["exclusive_max"] == 5 and not case["expected_valid"]
        for case in exclusive_max
    )
    assert any(
        case["body"]["exclusive_max"] == 4.99 and case["expected_valid"]
        and "just inside" in case["name"]
        for case in exclusive_max
    )
    assert any(
        case["body"]["exclusive_max"] == 5.01 and not case["expected_valid"]
        for case in exclusive_max
    )


def test_path_parameters_are_included_and_type_checked() -> None:
    cases = normalize_cases(generate_cases(load_spec()))
    valid = next(
        case for case in cases
        if case["method"] == "GET"
        and case["path"] == "/products/{product_id}"
        and case["kind"] == "valid"
    )
    invalid = next(
        case for case in cases
        if case["method"] == "GET"
        and case["path"] == "/products/{product_id}"
        and case["kind"] == "invalid_type"
        and case["field"] == "path.product_id"
    )

    assert valid["path_parameters"] == {"product_id": 1}
    assert invalid["path_parameters"] == {"product_id": "not-an-integer"}
    assert invalid["expected_valid"] is False


def test_generator_never_opens_criteria_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    document = load_spec()
    original_open = builtins.open
    original_path_open = Path.open

    def guard(file, *args, **kwargs):
        if "criteria.yaml" in str(file).replace("\\", "/"):
            raise AssertionError("baseline generator attempted to load criteria.yaml")
        return original_open(file, *args, **kwargs)

    def guard_path_open(self, *args, **kwargs):
        if self.name == "criteria.yaml":
            raise AssertionError("baseline generator attempted to load criteria.yaml")
        return original_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(Path, "open", guard_path_open)
    assert generate_cases(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda spec: spec["paths"]["/products"]["post"].setdefault(
                "parameters", []
            ).append(
                {
                    "name": "q", "in": "query", "required": False,
                    "schema": {"type": "string"},
                }
            ),
            "only path parameters",
        ),
        (
            lambda spec: spec["components"]["schemas"]["ProductCreate"]["properties"][
                "name"
            ].update(
                {"pattern": "^[A-Z]+$"}
            ),
            "pattern",
        ),
        (
            lambda spec: spec["paths"]["/products"]["post"]["requestBody"][
                "content"
            ].update(
                {"text/plain": {"schema": {"type": "string"}}}
            ),
            "application/json",
        ),
        (
            lambda spec: spec["components"]["schemas"]["ProductCreate"]["properties"][
                "name"
            ].update({"type": "array", "items": {"type": "string"}}),
            "items",
        ),
        (
            lambda spec: spec["components"]["schemas"]["ProductCreate"][
                "properties"
            ].__setitem__(
                "name", {"$ref": "#/components/schemas/MissingSchema"}
            ),
            "unresolved schema reference",
        ),
    ],
)
def test_unsupported_schema_constructs_fail_clearly(mutate, message: str) -> None:
    document = load_spec()
    mutate(document)

    with pytest.raises(UnsupportedSchemaError, match=message):
        generate_cases(document)


def test_integer_fields_get_int_values_when_bounds_are_written_as_floats() -> None:
    # FastAPI writes the integer bound 1 as 1.0 in its OpenAPI output.
    document = load_spec()
    order = document["components"]["schemas"]["OrderCreate"]["properties"]
    assert isinstance(order["product_id"]["exclusiveMinimum"], float)
    assert isinstance(order["quantity"]["maximum"], float)

    cases = [
        case
        for case in generate_cases(document)
        if (case.method, case.path) == ("POST", "/orders")
    ]
    values = [
        case.body[field]
        for case in cases
        if case.body is not None and case.kind != "invalid_type"
        for field in ("product_id", "quantity")
        if field in case.body
    ]
    assert values
    assert all(type(value) is int for value in values), values
    assert not any("0.0" in case.name for case in cases)


def test_fractional_bound_on_integer_field_is_rejected() -> None:
    document = load_spec()
    document["components"]["schemas"]["OrderCreate"]["properties"]["quantity"][
        "maximum"
    ] = 10.5

    with pytest.raises(UnsupportedSchemaError, match="whole number"):
        generate_cases(document)
