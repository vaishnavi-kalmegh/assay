"""Generate executable pytest checks from structured acceptance criteria.

The schema-only baseline remains criteria-agnostic. This module translates
supported criterion constraints into explicit request and response assertions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from assay.mapping import Criterion, MappingError, load_criteria
from assay.runner import load_spec

_REQ_ID = re.compile(r"^REQ-\d{3,}$")
_MARKER_FILE = ".assay-generated"
_PYTEST_INI = """\
[pytest]
addopts = -p no:cacheprovider
markers =
    req(requirement_id): acceptance requirement covered by a test
"""
_VALID_PRODUCT = {"name": "Fixture product", "price": 9.99, "category": "book"}


def _write(path: Path, text: str) -> None:
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


def _validate_criterion(criterion: Criterion, spec: dict[str, Any]) -> None:
    """Reject criteria that do not target an operation or rule we implement."""
    method = criterion.method.lower()
    if criterion.path not in spec.get("paths", {}):
        raise MappingError(f"{criterion.id}: unknown OpenAPI path {criterion.path}")
    operation = spec["paths"][criterion.path].get(method)
    if not isinstance(operation, dict):
        raise MappingError(
            f"{criterion.id}: operation {criterion.method} {criterion.path} "
            "is absent from the OpenAPI spec"
        )
    supported = {
        ("GET", "/health", "response.status", "equals"),
        ("GET", "/health", "response.body.status", "equals"),
        ("POST", "/products", "request.name", "min_length"),
        ("POST", "/products", "request.name", "max_length"),
        ("POST", "/products", "request.price", "greater_than"),
        ("POST", "/products", "request.price", "less_than_or_equal"),
        ("POST", "/products", "request.category", "enum"),
        ("POST", "/products", "request.name", "required"),
        (
            "GET",
            "/products/{product_id}",
            "response.body.id",
            "equals_path_parameter",
        ),
        ("GET", "/products/{product_id}", "path.product_id", "not_found"),
        (
            "PATCH",
            "/products/{product_id}/stock",
            "request.quantity",
            "greater_than_or_equal",
        ),
        (
            "PATCH",
            "/products/{product_id}/stock",
            "request.quantity",
            "less_than_or_equal",
        ),
        ("POST", "/orders", "request.quantity", "less_than_or_equal"),
        ("POST", "/orders", "request.quantity", "greater_than_or_equal"),
        ("POST", "/orders", "request.product_id", "not_found"),
    }
    key = (
        criterion.method.upper(),
        criterion.path,
        criterion.field,
        criterion.constraint,
    )
    if key not in supported:
        raise MappingError(
            f"{criterion.id}: unsupported acceptance rule "
            f"{criterion.field!r} / {criterion.constraint!r}"
        )
    if criterion.expected_status not in {200, 201, 404, 422}:
        raise MappingError(
            f"{criterion.id}: unsupported expected status {criterion.expected_status}"
        )


def _product_setup() -> list[str]:
    return [
        '    created = client.post("/products", json=_VALID_PRODUCT)',
        "    assert created.status_code == 201, created.text",
        '    product_id = created.json()["id"]',
    ]


def _quantity_value(criterion: Criterion) -> Any:
    """Choose a quantity that exercises the criterion's expected boundary."""
    value = criterion.value
    if criterion.expected_status >= 400:
        if criterion.constraint == "greater_than_or_equal":
            return value - 1
        if criterion.constraint == "less_than_or_equal":
            return value + 1
    return value


def _product_body(criterion: Criterion) -> dict[str, Any]:
    """Create a valid product body with the criterion's target value changed."""
    body = dict(_VALID_PRODUCT)
    value = criterion.value
    invalid = criterion.expected_status >= 400
    field = criterion.field.removeprefix("request.")

    if field == "name" and criterion.constraint == "min_length":
        length = max(0, int(value) - 1) if invalid else int(value)
        body["name"] = "x" * length
    elif field == "name" and criterion.constraint == "max_length":
        length = int(value) + 1 if invalid else int(value)
        body["name"] = "x" * length
    elif field == "price" and criterion.constraint == "greater_than":
        body["price"] = value if invalid else float(value) + 0.01
    elif field == "price" and criterion.constraint == "less_than_or_equal":
        body["price"] = value + 1 if invalid else value
    elif field == "category" and criterion.constraint == "enum":
        body["category"] = "__unsupported__" if invalid else value[0]
    elif field == "name" and criterion.constraint == "required":
        body.pop("name", None)
    return body


def _render_test(criterion: Criterion) -> str:
    """Render one explicit pytest test for a validated criterion."""
    req_id = criterion.id
    function = "test_" + req_id.lower().replace("-", "_")
    status = criterion.expected_status
    method = criterion.method.upper()
    path = criterion.path
    lines = [f"@pytest.mark.req({req_id!r})", f"def {function}(client):"]

    if criterion.field == "response.status":
        lines.append(f"    response = client.get({path!r})")
    elif criterion.field == "response.body.status":
        lines += [
            f"    response = client.get({path!r})",
            f"    assert response.status_code == {status}",
            f"    assert response.json()['status'] == {criterion.value!r}",
        ]
        return "\n".join(lines)
    elif criterion.constraint == "equals_path_parameter":
        lines += _product_setup()
        lines += [
            '    response = client.get(f"/products/{product_id}")',
            f"    assert response.status_code == {status}",
            '    assert response.json()["id"] == product_id',
        ]
        return "\n".join(lines)
    elif criterion.constraint == "not_found":
        if method == "GET":
            target = path.replace("{product_id}", str(criterion.value))
            lines.append(f"    response = client.get({target!r})")
        else:
            lines.append(
                "    response = client.post("
                f'"/orders", json={{"product_id": {criterion.value!r}, "quantity": 1}})'
            )
    elif method == "PATCH":
        lines += _product_setup()
        quantity = _quantity_value(criterion)
        lines.append(
            '    response = client.patch('
            'f"/products/{product_id}/stock", '
            f'json={{"quantity": {quantity!r}}})'
        )
    elif method == "POST" and path == "/orders":
        lines += _product_setup()
        quantity = _quantity_value(criterion)
        lines.append(
            '    response = client.post("/orders", '
            f'json={{"product_id": product_id, "quantity": {quantity!r}}})'
        )
    else:
        body = _product_body(criterion)
        lines.append(
            f"    response = client.{method.lower()}({path!r}, json={body!r})"
        )

    lines.append(f"    assert response.status_code == {status}, response.text")
    return "\n".join(lines)


def export_acceptance_tests(
    spec_path: Path, criteria_path: Path, output_dir: Path
) -> list[dict[str, str]]:
    """Generate one directly asserted pytest test per criterion."""
    spec = load_spec(spec_path)
    criteria = load_criteria(criteria_path)
    seen: set[str] = set()
    for criterion in criteria:
        if not _REQ_ID.fullmatch(criterion.id):
            raise MappingError(f"invalid requirement ID: {criterion.id!r}")
        if criterion.id in seen:
            raise MappingError(f"duplicate requirement ID: {criterion.id}")
        seen.add(criterion.id)
        _validate_criterion(criterion, spec)

    output_dir = Path(output_dir)
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError(f"{output_dir} exists and is not a directory")
        if any(output_dir.iterdir()) and not (output_dir / _MARKER_FILE).exists():
            raise ValueError(
                f"{output_dir} is not empty and was not created by assay; "
                "choose an empty or new directory"
            )
        patterns = (
            "test_acceptance*.py",
            "conftest.py",
            "pytest.ini",
            "manifest.json",
            "junit.xml",
        )
        for pattern in patterns:
            for stale in output_dir.glob(pattern):
                stale.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / _MARKER_FILE).write_text(
        "generated by assay\n", encoding="utf-8"
    )

    header = [
        '"""Generated acceptance tests; regenerate with assay acceptance."""',
        "",
        "import pytest",
        "",
        f"_VALID_PRODUCT = {_VALID_PRODUCT!r}",
        "",
    ]
    tests = "\n\n".join(_render_test(criterion) for criterion in criteria)
    _write(output_dir / "test_acceptance.py", "\n".join(header) + tests)
    _write(
        output_dir / "conftest.py",
        '"""Fixtures for generated acceptance tests."""\n\n'
        "import pytest\n"
        "from fastapi.testclient import TestClient\n"
        "from assay.demo_api import create_app\n\n"
        "@pytest.fixture\n"
        "def client():\n"
        "    with TestClient(create_app(seeded=False)) as test_client:\n"
        "        yield test_client\n",
    )
    _write(output_dir / "pytest.ini", _PYTEST_INI)
    manifest = [
        {
            "requirement_id": criterion.id,
            "description": criterion.description,
            "method": criterion.method.upper(),
            "path": criterion.path,
            "expected_status": criterion.expected_status,
            "test": (
                "test_acceptance.py::test_"
                + criterion.id.lower().replace("-", "_")
            ),
        }
        for criterion in criteria
    ]
    _write(
        output_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True),
    )
    return manifest


def run_acceptance(
    spec_path: Path,
    criteria_path: Path,
    output_dir: Path,
    results_path: Path,
) -> dict[str, Any]:
    """Generate and execute acceptance tests, then write deterministic JSON results."""
    manifest = export_acceptance_tests(spec_path, criteria_path, output_dir)
    junit_path = Path(output_dir) / "junit.xml"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(output_dir),
            "-q",
            f"--junitxml={junit_path}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if not junit_path.exists():
        raise RuntimeError(
            "pytest did not produce a JUnit report:\n"
            + completed.stdout
            + completed.stderr
        )
    observed: dict[str, str] = {}
    for node in ET.parse(junit_path).getroot().iter("testcase"):
        outcome = "passed"
        if node.find("failure") is not None:
            outcome = "failed"
        elif node.find("error") is not None:
            outcome = "error"
        elif node.find("skipped") is not None:
            outcome = "skipped"
        observed[node.get("name", "")] = outcome
    results = [
        {
            **entry,
            "outcome": observed.get(entry["test"].split("::")[-1], "not_run"),
        }
        for entry in manifest
    ]
    counts: dict[str, int] = {}
    for result in results:
        outcome = result["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    report = {
        "summary": {
            "total": len(results),
            **dict(sorted(counts.items())),
        },
        "results": results,
    }
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    _write(results_path, json.dumps(report, indent=2, sort_keys=True))
    return report


def register_commands(commands: Any) -> None:
    command = commands.add_parser(
        "acceptance", help="generate and run tests from criteria.yaml"
    )
    command.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
    command.add_argument("--criteria", type=Path, default=Path("criteria.yaml"))
    command.add_argument(
        "--output", type=Path, default=Path("generated_acceptance")
    )
    command.add_argument(
        "--results",
        type=Path,
        default=Path("reports/acceptance-results.json"),
    )


def handle(arguments: argparse.Namespace) -> int:
    try:
        report = run_acceptance(
            arguments.spec,
            arguments.criteria,
            arguments.output,
            arguments.results,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    summary = report["summary"]
    outcomes = ", ".join(
        f"{key} {value}" for key, value in summary.items() if key != "total"
    )
    print(f"Ran {summary['total']} acceptance tests: {outcomes}")
    print(f"Results written to {arguments.results}")
    return 0 if summary.get("passed", 0) == summary["total"] else 1
