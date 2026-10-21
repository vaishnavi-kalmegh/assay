"""Tests for the pytest exporter and runner (milestone 3)."""

import builtins
import json
import subprocess
import sys
from pathlib import Path

import pytest

from assay import runner
from assay.baseline import generate_cases
from assay.runner import (
    ManifestEntry,
    expected_status,
    export_tests,
    load_mapping,
    main,
    run,
    run_tests,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "specs" / "openapi.json"


def spec_document() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def read_tree(directory: Path, skip: tuple[str, ...] = ("junit.xml",)) -> dict:
    """Map every generated file name to its bytes, ignoring pytest's own output."""
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name not in skip
    }


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Export and run the whole suite once; several tests inspect the result."""
    base = tmp_path_factory.mktemp("suite")
    report = run(SPEC, base / "generated", base / "results.json")
    return {"dir": base / "generated", "file": base / "results.json", **report}


# --- expected status ---------------------------------------------------------


def test_valid_cases_expect_the_lowest_documented_2xx_status() -> None:
    document = spec_document()
    expected = {
        ("GET", "/health"): 200,
        ("POST", "/products"): 201,
        ("GET", "/products/{product_id}"): 200,
        ("PATCH", "/products/{product_id}/stock"): 200,
        ("POST", "/orders"): 201,
    }
    seen = set()
    for case in generate_cases(document):
        if case.expected_valid:
            assert expected_status(document, case) == expected[
                (case.method, case.path)
            ]
            seen.add((case.method, case.path))
    assert seen == set(expected)


def test_invalid_cases_expect_422() -> None:
    document = spec_document()
    invalid = [case for case in generate_cases(document) if not case.expected_valid]
    assert invalid
    assert {expected_status(document, case) for case in invalid} == {422}


def test_lowest_2xx_is_used_when_several_are_documented() -> None:
    document = spec_document()
    document["paths"]["/orders"]["post"]["responses"]["200"] = {"description": "ok"}
    valid = next(
        case
        for case in generate_cases(document)
        if case.expected_valid and case.path == "/orders"
    )
    assert expected_status(document, valid) == 200


def test_valid_case_without_a_documented_2xx_is_an_error() -> None:
    document = spec_document()
    document["paths"]["/health"]["get"]["responses"] = {"500": {"description": "x"}}
    valid = next(
        case
        for case in generate_cases(document)
        if case.expected_valid and case.path == "/health"
    )
    with pytest.raises(ValueError, match="No documented 2xx"):
        expected_status(document, valid)


# --- export ------------------------------------------------------------------


def test_export_writes_one_test_per_baseline_case(tmp_path: Path) -> None:
    entries = export_tests(SPEC, tmp_path / "out")
    cases = generate_cases(spec_document())

    assert [entry.case_id for entry in entries] == [case.case_id for case in cases]
    assert len({entry.function for entry in entries}) == len(entries)

    out = tmp_path / "out"
    names = {path.name for path in out.iterdir()}
    assert {"conftest.py", "pytest.ini", "manifest.json"} <= names
    assert {entry.file for entry in entries} <= names

    sources = "".join(
        (out / name).read_text(encoding="utf-8")
        for name in sorted(names)
        if name.startswith("test_")
    )
    for entry in entries:
        assert f"def {entry.function}(" in sources
    assert "pytest.mark.req" not in sources  # no mapping given, no markers


def test_manifest_matches_the_exported_entries(tmp_path: Path) -> None:
    entries = export_tests(SPEC, tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text("utf-8"))
    assert manifest == [entry.model_dump(mode="json") for entry in entries]
    assert all(ManifestEntry.model_validate(item) for item in manifest)


def test_export_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    export_tests(SPEC, tmp_path / "first")
    export_tests(SPEC, tmp_path / "second")
    assert read_tree(tmp_path / "first") == read_tree(tmp_path / "second")


def test_re_exporting_removes_stale_generated_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    export_tests(SPEC, out)
    stale = out / "test_stale.py"
    stale.write_text("def test_stale():\n    assert False\n", encoding="utf-8")

    export_tests(SPEC, out)

    assert not stale.exists()


def test_export_refuses_to_overwrite_a_directory_it_did_not_create(
    tmp_path: Path,
) -> None:
    out = tmp_path / "precious"
    out.mkdir()
    keep = out / "notes.txt"
    keep.write_text("do not delete", encoding="utf-8")

    with pytest.raises(ValueError, match="not created by assay"):
        export_tests(SPEC, out)

    assert keep.read_text(encoding="utf-8") == "do not delete"


def test_export_never_opens_criteria_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = builtins.open
    original_read_text = Path.read_text

    def guard_open(file, *args, **kwargs):
        if "criteria.yaml" in str(file).replace("\\", "/"):
            raise AssertionError("exporter attempted to load criteria.yaml")
        return original_open(file, *args, **kwargs)

    def guard_read_text(self, *args, **kwargs):
        if self.name == "criteria.yaml":
            raise AssertionError("exporter attempted to load criteria.yaml")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(Path, "read_text", guard_read_text)

    assert export_tests(SPEC, tmp_path / "out")


# --- requirement markers -----------------------------------------------------


def test_markers_appear_only_for_mapped_cases(tmp_path: Path) -> None:
    cases = generate_cases(spec_document())
    mapped = cases[0]
    other = cases[1]
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(
        json.dumps({mapped.case_id: ["REQ-004", "REQ-001"]}), encoding="utf-8"
    )

    entries = export_tests(SPEC, tmp_path / "out", mapping_file)

    by_id = {entry.case_id: entry for entry in entries}
    assert by_id[mapped.case_id].requirements == ["REQ-001", "REQ-004"]
    assert by_id[other.case_id].requirements == []
    source = (tmp_path / "out" / by_id[mapped.case_id].file).read_text("utf-8")
    function_at = source.index(f"def {by_id[mapped.case_id].function}(")
    before = source[:function_at].rstrip().splitlines()
    assert before[-2:] == [
        "@pytest.mark.req('REQ-001')",
        "@pytest.mark.req('REQ-004')",
    ]
    assert source.count("pytest.mark.req") == 2


def test_yaml_mapping_files_are_supported(tmp_path: Path) -> None:
    case = generate_cases(spec_document())[0]
    mapping_file = tmp_path / "mapping.yaml"
    mapping_file.write_text(f"{case.case_id}: REQ-010\n", encoding="utf-8")

    mapping = load_mapping(mapping_file, {case.case_id})

    assert mapping == {case.case_id: ["REQ-010"]}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ({"BASE-DOESNOTEXIST": ["REQ-001"]}, "unknown case ID"),
        ({"__first__": ["not-a-requirement"]}, "requirement IDs like REQ-001"),
        ({"__first__": [1]}, "requirement IDs like REQ-001"),
        (["REQ-001"], "object of case ID"),
    ],
)
def test_invalid_mapping_files_are_rejected(
    tmp_path: Path, content, message: str
) -> None:
    first = generate_cases(spec_document())[0].case_id
    if isinstance(content, dict):
        content = {(first if k == "__first__" else k): v for k, v in content.items()}
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        export_tests(SPEC, tmp_path / "out", mapping_file)


def test_mapped_requirements_flow_into_run_results(tmp_path: Path) -> None:
    case = next(
        case for case in generate_cases(spec_document()) if case.path == "/health"
    )
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({case.case_id: ["REQ-001"]}), encoding="utf-8")

    report = run(SPEC, tmp_path / "out", tmp_path / "results.json", mapping_file)

    by_id = {result["case_id"]: result for result in report["results"]}
    assert by_id[case.case_id]["requirements"] == ["REQ-001"]
    assert by_id[case.case_id]["outcome"] == "passed"


# --- running -----------------------------------------------------------------


def test_every_generated_test_passes_against_the_correct_demo_api(suite: dict) -> None:
    summary = suite["summary"]
    failing = [r for r in suite["results"] if r["outcome"] != "passed"]
    assert not failing, failing
    total = len(generate_cases(spec_document()))
    assert summary == {"total": total, "passed": total}


def test_every_result_traces_to_a_baseline_case(suite: dict) -> None:
    cases = {case.case_id: case for case in generate_cases(spec_document())}
    results = suite["results"]

    assert [r["case_id"] for r in results] == list(cases)
    for result in results:
        case = cases[result["case_id"]]
        assert result["name"] == case.name
        assert result["kind"] == case.kind
        assert result["expected_valid"] is case.expected_valid
        assert result["actual_status"] == result["expected_status"]
        assert result["test"].endswith(
            "::test_" + case.case_id.lower().replace("-", "_")
        )


def test_results_file_has_the_documented_shape(suite: dict) -> None:
    written = json.loads(suite["file"].read_text(encoding="utf-8"))

    assert set(written) == {"summary", "results"}
    assert written["summary"]["total"] == len(written["results"])
    assert set(written["results"][0]) == {
        "case_id",
        "name",
        "kind",
        "method",
        "path",
        "field",
        "expected_valid",
        "expected_status",
        "actual_status",
        "outcome",
        "requirements",
        "test",
    }


def test_results_are_identical_across_runs(tmp_path: Path, suite: dict) -> None:
    run(SPEC, tmp_path / "again", tmp_path / "results.json")

    assert (tmp_path / "results.json").read_bytes() == suite["file"].read_bytes()


def test_a_wrong_api_response_is_reported_as_a_failed_case(tmp_path: Path) -> None:
    """If the target misbehaves the runner must report it, not hide it."""
    entries = export_tests(SPEC, tmp_path / "out")
    target = next(e for e in entries if e.method == "POST" and e.kind == "valid")
    test_file = tmp_path / "out" / target.file
    source = test_file.read_text(encoding="utf-8")
    start = source.index(f"def {target.function}(")
    end = source.index("\n\n\n", start) if "\n\n\n" in source[start:] else len(source)
    block = source[start:end]
    broken = block.replace(
        f"assert response.status_code == {target.expected_status}",
        "assert response.status_code == 599",
    )
    assert broken != block
    test_file.write_text(source.replace(block, broken), encoding="utf-8")

    report = run_tests(tmp_path / "out", entries, tmp_path / "results.json")

    by_id = {result["case_id"]: result for result in report["results"]}
    assert by_id[target.case_id]["outcome"] == "failed"
    assert by_id[target.case_id]["actual_status"] == target.expected_status
    assert report["summary"]["failed"] == 1
    assert report["summary"]["passed"] == len(entries) - 1


def test_generated_tests_do_not_depend_on_execution_order(suite: dict) -> None:
    """Every test gets a fresh app, so running them in reverse must still pass."""
    directory = suite["dir"]
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", str(directory), "--collect-only", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = [line for line in collected.stdout.splitlines() if "::" in line]
    assert len(node_ids) == suite["summary"]["total"]

    reversed_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        + list(reversed(node_ids)),
        capture_output=True,
        text=True,
        check=False,
        cwd=directory,
    )

    assert reversed_run.returncode == 0, reversed_run.stdout + reversed_run.stderr


# --- command line ------------------------------------------------------------


def test_cli_run_returns_zero_when_everything_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "run",
            "--spec",
            str(SPEC),
            "--output",
            str(tmp_path / "out"),
            "--results",
            str(tmp_path / "results.json"),
        ]
    )

    assert code == 0
    total = len(generate_cases(spec_document()))
    assert f"Ran {total} generated tests: passed {total}" in capsys.readouterr().out
    assert (tmp_path / "results.json").exists()


def test_cli_export_writes_files_without_running_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["export", "--spec", str(SPEC), "--output", str(tmp_path / "out")])

    assert code == 0
    total = len(generate_cases(spec_document()))
    assert f"Exported {total} tests" in capsys.readouterr().out
    assert not (tmp_path / "out" / "junit.xml").exists()


def test_cli_reports_a_missing_spec_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["export", "--spec", str(tmp_path / "nope.json"), "--output", str(tmp_path)]
    )

    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_reports_an_unsupported_spec_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = spec_document()
    document["paths"]["/health"]["get"]["parameters"] = [
        {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}}
    ]
    bad_spec = tmp_path / "bad.json"
    bad_spec.write_text(json.dumps(document), encoding="utf-8")

    code = main(
        ["export", "--spec", str(bad_spec), "--output", str(tmp_path / "out")]
    )

    assert code == 2
    assert "only path parameters" in capsys.readouterr().err



# --- external target mode ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "https://staging.example.test",
        "http://127.0.0.1:8080/",
    ],
)
def test_external_base_url_accepts_absolute_http_urls(url: str) -> None:
    runner._validate_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "localhost:8000",
        "ftp://example.test",
        "https:///missing-host",
        "http://user:secret@example.test",
        "http://localhost:8000/api",
        "http://localhost:8000?debug=1",
    ],
)
def test_external_base_url_rejects_invalid_or_embedded_credentials(url: str) -> None:
    with pytest.raises(ValueError, match="--base-url"):
        runner._validate_base_url(url)


def test_exported_client_fixture_supports_external_http_target(tmp_path: Path) -> None:
    export_tests(SPEC, tmp_path / "out")
    fixture = (tmp_path / "out" / "conftest.py").read_text(encoding="utf-8")

    assert 'os.environ.get("ASSAY_BASE_URL")' in fixture
    assert "httpx.Client(base_url=" in fixture
    assert "yield TestClient(" in fixture
    assert "timeout=10.0" in fixture



def test_run_tests_passes_only_explicit_external_target_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "generated"
    out.mkdir()
    captured_environments: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        captured_environments.append(kwargs["env"])
        junit_arg = next(arg for arg in command if arg.startswith("--junitxml="))
        junit_path = Path(junit_arg.split("=", 1)[1])
        junit_path.write_text(
            '<testsuites><testsuite><testcase name="test_base">'
            '<properties><property name="actual_status" value="200" />'
            '</properties></testcase></testsuite></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "1 passed", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setenv("ASSAY_BASE_URL", "http://ambient.example.test")

    entry = ManifestEntry(
        case_id="BASE-TEST",
        name="GET /health",
        kind="valid",
        method="GET",
        path="/health",
        field="",
        expected_valid=True,
        expected_status=200,
        file="test_health.py",
        function="test_base",
        requirements=[],
    )
    runner.run_tests(
        out, [entry], tmp_path / "external.json", base_url="http://127.0.0.1:8000"
    )
    runner.run_tests(out, [entry], tmp_path / "local.json")

    assert captured_environments[0]["ASSAY_BASE_URL"] == "http://127.0.0.1:8000"
    assert "ASSAY_BASE_URL" not in captured_environments[1]
