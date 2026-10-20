"""Export baseline cases as ordinary pytest files, run them, and report results.

Flow: OpenAPI spec -> baseline cases -> generated pytest files -> pytest run ->
JUnit XML -> a sorted, timestamp-free JSON results file.

The generated files are plain pytest. Every test builds a brand-new demo API
(``create_app(seeded=False)``) so nothing is shared between tests, and any test
that needs an existing product creates one through ``POST /products`` and uses
the ID the API returns. This module never reads acceptance criteria; requirement
markers appear only when the caller supplies an explicit case-to-requirement
mapping file (the mapper that produces it arrives in a later milestone).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict

from assay.baseline import BaselineCase, generate_cases

# Invalid inputs are expected to be rejected by request validation.
INVALID_STATUS = 422

# Fields that point at an existing resource. When a case uses the baseline's
# placeholder value for one of these, the generated test uses the ID returned by
# the API instead, via the pytest fixture named here.
REFERENCE_FIELDS = {"product_id": "product_id"}

MARKER_FILE = ".assay-generated"
_REQ_ID = re.compile(r"^REQ-\d{3,}$")

_PYTEST_INI = """\
[pytest]
addopts = -p no:cacheprovider
markers =
    req(requirement_id): acceptance requirement covered by a test
"""

_CONFTEST = '''\
"""Shared fixtures for assay-generated tests. Generated; do not edit."""

import os

import pytest
from fastapi.testclient import TestClient

from assay.demo_api import create_app


@pytest.fixture
def client():
    """A client for a brand-new, empty demo API. Nothing is shared between tests.

    ASSAY_MUTANT is set only by `assay evaluate mutants`; unset means the real API.
    """
    return TestClient(
        create_app(seeded=False, mutant=os.environ.get("ASSAY_MUTANT") or None)
    )


@pytest.fixture
def product_id(client):
    """Create one product through the API and return the ID the API assigned."""
    response = client.post(
        "/products",
        json={"name": "Fixture product", "price": 9.99, "category": "book"},
    )
    assert response.status_code == 201
    return response.json()["id"]
'''


class ManifestEntry(BaseModel):
    """One generated test and the baseline case it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    name: str
    kind: str
    method: str
    path: str
    field: str
    expected_valid: bool
    expected_status: int
    file: str
    function: str
    requirements: list[str]


@dataclass(frozen=True)
class _Ref:
    """A value that the generated test takes from a fixture instead of a literal."""

    fixture: str


def load_spec(spec_path: Path) -> dict[str, Any]:
    """Read an OpenAPI JSON document."""
    try:
        document = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{spec_path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{spec_path} must contain a JSON object")
    return document


def expected_status(spec: dict[str, Any], case: BaselineCase) -> int:
    """Return the status a correct API gives for this case.

    Valid cases expect the lowest documented 2xx response of the operation.
    Invalid cases expect 422 (request validation failure).
    """
    if not case.expected_valid:
        return INVALID_STATUS
    operation = spec["paths"][case.path][case.method.lower()]
    codes = sorted(
        int(code)
        for code in operation.get("responses", {})
        if str(code).isdigit() and 200 <= int(code) < 300
    )
    if not codes:
        raise ValueError(f"No documented 2xx response for {case.method} {case.path}")
    return codes[0]


def load_mapping(mapping_path: Path, known_case_ids: set[str]) -> dict[str, list[str]]:
    """Read a JSON or YAML file of ``{case_id: [requirement IDs]}``."""
    path = Path(mapping_path)
    text = path.read_text(encoding="utf-8")
    raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("mapping must be an object of case ID -> requirement IDs")
    mapping: dict[str, list[str]] = {}
    for case_id, requirements in raw.items():
        if case_id not in known_case_ids:
            raise ValueError(f"mapping refers to an unknown case ID: {case_id}")
        if isinstance(requirements, str):
            requirements = [requirements]
        if not isinstance(requirements, list) or not all(
            isinstance(item, str) and _REQ_ID.match(item) for item in requirements
        ):
            raise ValueError(
                f"mapping for {case_id} must be requirement IDs like REQ-001"
            )
        mapping[case_id] = sorted(set(requirements))
    return mapping


def _literal(value: Any) -> str:
    """Render a JSON-compatible value as deterministic Python source."""
    if isinstance(value, _Ref):
        return value.fixture
    if isinstance(value, dict):
        items = ", ".join(
            f"{key!r}: {_literal(item)}" for key, item in sorted(value.items())
        )
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    return repr(value)


def _placeholders(cases: list[BaselineCase]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per operation, the baseline's default valid value for each input field."""
    placeholders: dict[tuple[str, str], dict[str, Any]] = {}
    for case in cases:
        if case.kind == "valid":
            values = {**case.path_parameters, **(case.body or {})}
            placeholders[(case.method, case.path)] = values
    return placeholders


def _use_fixtures(
    values: dict[str, Any] | None, defaults: dict[str, Any]
) -> dict[str, Any] | None:
    """Swap placeholder IDs for fixtures so tests use IDs the API really issued."""
    if values is None:
        return None
    result = dict(values)
    for field, fixture in REFERENCE_FIELDS.items():
        if (
            field in result
            and field in defaults
            and type(result[field]) is type(defaults[field])
            and result[field] == defaults[field]
        ):
            result[field] = _Ref(fixture)
    return result


def _url_source(path: str, parameters: dict[str, Any]) -> tuple[str, set[str]]:
    """Return Python source for the request URL and the fixtures it uses."""
    fixtures: set[str] = set()
    url = path
    for name, value in sorted(parameters.items()):
        if isinstance(value, _Ref):
            url = url.replace("{" + name + "}", "{" + value.fixture + "}")
            fixtures.add(value.fixture)
        else:
            url = url.replace("{" + name + "}", quote(str(value), safe=""))
    return (("f" if fixtures else "") + repr(url)), fixtures


def _docstring(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _test_source(
    case: BaselineCase,
    function: str,
    status: int,
    requirements: list[str],
    defaults: dict[str, Any],
) -> str:
    parameters = _use_fixtures(case.path_parameters, defaults) or {}
    body = _use_fixtures(case.body, defaults)
    url, fixtures = _url_source(case.path, parameters)
    if body is not None:
        fixtures |= {
            value.fixture for value in body.values() if isinstance(value, _Ref)
        }
    arguments = ", ".join(["client", *sorted(fixtures), "record_property"])
    request = f"client.request({case.method!r}, {url}"
    if body is not None:
        request += f", json={_literal(body)}"
    request += ")"
    lines = [f"@pytest.mark.req({req!r})" for req in requirements]
    lines += [
        f"def {function}({arguments}):",
        f'    """{_docstring(case.name)}"""',
        f"    record_property('case_id', {case.case_id!r})",
        f"    record_property('expected_status', {status})",
        f"    response = {request}",
        "    record_property('actual_status', response.status_code)",
        f"    assert response.status_code == {status}",
    ]
    return "\n".join(lines)


def _file_stem(case: BaselineCase) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", case.path.lower()).strip("_") or "root"
    return f"test_{case.method.lower()}_{slug}"


def _prepare_output_dir(output_dir: Path) -> None:
    """Create the output directory, refusing to touch a directory assay did not make."""
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError(f"{output_dir} exists and is not a directory")
        if any(output_dir.iterdir()) and not (output_dir / MARKER_FILE).exists():
            raise ValueError(
                f"{output_dir} is not empty and was not created by assay; "
                "choose an empty or new directory"
            )
        for pattern in ("test_*.py", "conftest.py", "manifest.json", "junit.xml"):
            for stale in output_dir.glob(pattern):
                stale.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / MARKER_FILE).write_text("generated by assay\n", encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


def export_tests(
    spec_path: Path, output_dir: Path, mapping_path: Path | None = None
) -> list[ManifestEntry]:
    """Write pytest files for every baseline case and return the manifest."""
    spec = load_spec(spec_path)
    cases = generate_cases(spec)
    mapping = (
        load_mapping(mapping_path, {case.case_id for case in cases})
        if mapping_path is not None
        else {}
    )
    defaults = _placeholders(cases)
    output_dir = Path(output_dir)
    _prepare_output_dir(output_dir)

    sources: dict[str, list[str]] = {}
    stems: dict[tuple[str, str], str] = {}
    entries: list[ManifestEntry] = []
    for case in cases:
        key = (case.method, case.path)
        if key not in stems:
            stem = _file_stem(case)
            taken = set(stems.values())
            suffix = 2
            unique = stem
            while unique in taken:
                unique = f"{stem}_{suffix}"
                suffix += 1
            stems[key] = unique
        file_name = f"{stems[key]}.py"
        function = "test_" + case.case_id.lower().replace("-", "_")
        status = expected_status(spec, case)
        requirements = mapping.get(case.case_id, [])
        header = f"# {case.method} {case.path}"
        sources.setdefault(
            file_name,
            [
                f'"""Generated by assay for {case.method} {case.path}. Do not edit."""',
                "",
                "import pytest  # noqa: F401",
                "",
                header,
            ],
        ).append(
            "\n\n" + _test_source(case, function, status, requirements, defaults[key])
        )
        entries.append(
            ManifestEntry(
                case_id=case.case_id,
                name=case.name,
                kind=case.kind,
                method=case.method,
                path=case.path,
                field=case.field,
                expected_valid=case.expected_valid,
                expected_status=status,
                file=file_name,
                function=function,
                requirements=requirements,
            )
        )

    for file_name, parts in sorted(sources.items()):
        _write(output_dir / file_name, "\n".join(parts))
    _write(output_dir / "conftest.py", _CONFTEST)
    _write(output_dir / "pytest.ini", _PYTEST_INI)
    manifest = [entry.model_dump(mode="json") for entry in entries]
    _write(
        output_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True),
    )
    return entries


def _read_junit(junit_path: Path) -> dict[str, dict[str, Any]]:
    """Map test function name -> outcome and recorded properties."""
    results: dict[str, dict[str, Any]] = {}
    for node in ET.parse(junit_path).getroot().iter("testcase"):
        outcome = "passed"
        if node.find("failure") is not None:
            outcome = "failed"
        elif node.find("error") is not None:
            outcome = "error"
        elif node.find("skipped") is not None:
            outcome = "skipped"
        properties = {
            item.get("name"): item.get("value") for item in node.iter("property")
        }
        results[node.get("name", "")] = {"outcome": outcome, "properties": properties}
    return results


def run_tests(
    output_dir: Path,
    entries: list[ManifestEntry],
    results_path: Path,
    mutant: str | None = None,
) -> dict[str, Any]:
    """Run the generated suite with pytest and write sorted JSON results.

    ``mutant`` names a demo API mutant to run against (None is the real API). The
    environment variable is always set or cleared here so a stray value in the
    caller's shell can never turn a mutant on silently.
    """
    output_dir = Path(output_dir)
    junit_path = output_dir / "junit.xml"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("ASSAY_MUTANT", None)
    if mutant is not None:
        environment["ASSAY_MUTANT"] = mutant
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
        env=environment,
        timeout=600,
    )
    if not junit_path.exists():
        raise RuntimeError(
            "pytest did not produce a JUnit report:\n"
            + completed.stdout
            + completed.stderr
        )
    observed = _read_junit(junit_path)

    results: list[dict[str, Any]] = []
    for entry in entries:
        found = observed.get(entry.function)
        actual = None
        outcome = "not_run"
        if found is not None:
            outcome = found["outcome"]
            raw_actual = found["properties"].get("actual_status")
            actual = int(raw_actual) if raw_actual is not None else None
        results.append(
            {
                "case_id": entry.case_id,
                "name": entry.name,
                "kind": entry.kind,
                "method": entry.method,
                "path": entry.path,
                "field": entry.field,
                "expected_valid": entry.expected_valid,
                "expected_status": entry.expected_status,
                "actual_status": actual,
                "outcome": outcome,
                "requirements": entry.requirements,
                "test": f"{entry.file}::{entry.function}",
            }
        )
    counts: dict[str, int] = {}
    for result in results:
        counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
    report = {
        "summary": {"total": len(results), **dict(sorted(counts.items()))},
        "results": results,
    }
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    _write(results_path, json.dumps(report, indent=2, sort_keys=True))
    return report


def run(
    spec_path: Path,
    output_dir: Path,
    results_path: Path,
    mapping_path: Path | None = None,
) -> dict[str, Any]:
    """Export the suite, run it, and write results."""
    entries = export_tests(spec_path, output_dir, mapping_path)
    return run_tests(output_dir, entries, results_path)


def register_commands(commands: Any) -> None:
    """Add the `export` and `run` subcommands to the assay CLI."""
    for name, help_text in (
        ("export", "write generated pytest files"),
        ("run", "write generated pytest files, run them, and write results"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
        command.add_argument("--output", type=Path, default=Path("generated_tests"))
        command.add_argument(
            "--mapping",
            type=Path,
            default=None,
            help="JSON/YAML file of case ID -> requirement IDs (adds req markers)",
        )
        if name == "run":
            command.add_argument(
                "--results",
                type=Path,
                default=Path("reports/baseline-results.json"),
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assay", description="Export and run baseline API tests offline."
    )
    register_commands(parser.add_subparsers(dest="command", required=True))
    return parser


def handle(arguments: argparse.Namespace) -> int:
    """Run `assay export` or `assay run`: 0 success, 1 failing tests, 2 error."""
    try:
        if arguments.command == "export":
            entries = export_tests(arguments.spec, arguments.output, arguments.mapping)
            print(f"Exported {len(entries)} tests to {arguments.output}")
            return 0
        report = run(
            arguments.spec, arguments.output, arguments.results, arguments.mapping
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    summary = report["summary"]
    outcomes = ", ".join(
        f"{key} {value}" for key, value in summary.items() if key != "total"
    )
    print(f"Ran {summary['total']} generated tests: {outcomes}")
    print(f"Results written to {arguments.results}")
    return 0 if summary.get("passed", 0) == summary["total"] else 1


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for export and run."""
    return handle(_build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
