"""Tests for the offline requirement mapper (milestone 4)."""

import json
import socket
from pathlib import Path

import pytest

from assay.cli import main as cli_main
from assay.mapping import (
    DEFAULT_THRESHOLD,
    Criterion,
    DisabledLLMMapper,
    LLMDisabledError,
    MappableTest,
    MappingError,
    TfidfMapper,
    _test_bound,
    baseline_tests_from_spec,
    case_text,
    criterion_text,
    get_mapper,
    load_criteria,
    map_requirements,
    tokenize,
    write_reports,
)
from assay.runner import export_tests, load_mapping, load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "specs" / "openapi.json"
CRITERIA = ROOT / "criteria.yaml"


def criterion(**overrides) -> Criterion:
    values = {
        "id": "REQ-100",
        "method": "POST",
        "path": "/items",
        "field": "request.name",
        "constraint": "min_length",
        "value": 3,
        "expected_status": 422,
        "description": "Item name shorter than three characters is rejected.",
    }
    return Criterion(**{**values, **overrides})


def case(**overrides) -> MappableTest:
    values = {
        "case_id": "BASE-A",
        "name": "POST /items — body.name — just below minimum length 3",
        "method": "POST",
        "path": "/items",
        "field": "body.name",
        "kind": "boundary",
        "expected_valid": False,
        "expected_status": 422,
        "checks": ("status",),
    }
    return MappableTest(**{**values, **overrides})


@pytest.fixture(scope="module")
def real():
    criteria = load_criteria(CRITERIA)
    tests = baseline_tests_from_spec(load_spec(SPEC))
    return {
        "criteria": criteria,
        "tests": tests,
        "result": map_requirements(criteria, tests),
    }


def row_for(result, requirement_id: str):
    return next(row for row in result.rows if row.requirement_id == requirement_id)


# --- text preparation --------------------------------------------------------


def test_tokenize_normalizes_number_words_floats_and_stop_words() -> None:
    assert tokenize("Shorter than three characters") == ["shorter", "3", "characters"]
    assert tokenize("exclusive minimum 0.0 and 10000.0") == [
        "exclusive", "minimum", "0", "10000",
    ]
    assert tokenize("price 0.01") == ["price", "0.01"]
    assert tokenize("product_id") == ["product", "id"]


def test_criterion_and_test_texts_share_vocabulary_for_a_true_pair() -> None:
    text_a = set(criterion_text(criterion()).split())
    text_b = set(case_text(case()).split())

    assert {"name", "minimum", "length", "3", "invalid"} <= text_a & text_b


def test_texts_leave_out_method_path_and_status() -> None:
    text = criterion_text(criterion()) + " " + case_text(case())

    for word in ("post", "items", "422"):
        assert word not in text.split()


# --- loading criteria --------------------------------------------------------


def test_real_criteria_file_loads() -> None:
    criteria = load_criteria(CRITERIA)

    assert len(criteria) == 18
    assert criteria[0].id == "REQ-001"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("criteria: not-a-list\n", "top-level 'criteria' list"),
        ("- just\n- a list\n", "top-level 'criteria' list"),
        ("criteria: [unclosed\n", "not valid YAML"),
        (
            "criteria:\n- {id: REQ-1, method: GET, path: /x, field: f, constraint: c,"
            " value: 1, expected_status: 200, description: d, field_rule: old}\n",
            "criterion #1",
        ),
        (
            "criteria:\n- {id: REQ-1, method: GET, path: /x, field: f, constraint: c,"
            " value: 1, expected_status: ok, description: d}\n",
            "criterion #1",
        ),
    ],
)
def test_invalid_criteria_files_are_rejected(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "criteria.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(MappingError, match=message):
        load_criteria(path)


# --- eligibility and gap reasons ---------------------------------------------


def test_matching_test_is_mapped_with_a_score() -> None:
    result = map_requirements([criterion()], [case()], threshold=0.1)

    row = result.rows[0]
    assert [match.case_id for match in row.matches] == ["BASE-A"]
    assert 0.1 <= row.matches[0].score <= 1.0
    assert row.gap_reason is None


def test_operation_must_match() -> None:
    other = case(path="/other")

    row = map_requirements([criterion()], [other], threshold=0.0).rows[0]

    assert row.gap_reason == "no_test_for_operation"
    assert "POST /items" in (row.gap_detail or "")
    assert row.candidates == []


def test_method_comparison_ignores_case() -> None:
    row = map_requirements(
        [criterion(method="post")], [case(method="POST")], threshold=0.0
    ).rows[0]

    assert row.matches


def test_expected_status_must_match_and_the_gap_lists_available_statuses() -> None:
    tests = [case(expected_status=201), case(case_id="BASE-B", expected_status=200)]

    row = map_requirements([criterion()], tests, threshold=0.0).rows[0]

    assert row.gap_reason == "no_test_with_expected_status"
    assert "expects status 422" in (row.gap_detail or "")
    assert "200, 201" in (row.gap_detail or "")


def test_response_body_criteria_need_a_test_that_checks_the_body() -> None:
    body_criterion = criterion(
        field="response.body.status", constraint="equals", value="ok",
        expected_status=200,
    )
    status_only = case(
        field="request", name="GET /items — request — valid request",
        expected_status=200, expected_valid=True, kind="valid",
    )
    checks_body = status_only.model_copy(
        update={"case_id": "BASE-B", "checks": ("status", "body")}
    )

    blocked = map_requirements([body_criterion], [status_only], threshold=0.0)
    allowed = map_requirements(
        [body_criterion], [status_only, checks_body], threshold=0.0
    )

    assert blocked.rows[0].gap_reason == "needs_response_body_check"
    assert [c.case_id for c in allowed.rows[0].candidates] == ["BASE-B"]


def test_a_test_about_another_field_is_not_eligible() -> None:
    other_field = case(field="body.price", name="POST /items — body.price — x")

    row = map_requirements([criterion()], [other_field], threshold=0.0).rows[0]

    assert row.gap_reason == "no_test_for_field"
    assert "body.price" in (row.gap_detail or "")


def test_a_whole_request_test_is_eligible_for_any_field() -> None:
    whole = case(
        field="request body", name="POST /items — request body — valid input",
        expected_status=201, expected_valid=True, kind="valid",
    )

    row = map_requirements(
        [criterion(expected_status=201, constraint="unspecified")],
        [whole],
        threshold=0.0
    ).rows[0]

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_low_scoring_eligible_tests_are_reported_as_below_threshold() -> None:
    weak = case(
        name="POST /items — body.name — just below minimum length 3 unrelated words"
    )

    row = map_requirements([criterion()], [weak], threshold=0.99).rows[0]

    assert row.gap_reason == "below_threshold"
    assert row.matches == []
    assert [c.case_id for c in row.candidates] == ["BASE-A"]
    assert "below the threshold 0.99" in (row.gap_detail or "")


# --- ranking, threshold, top-k ----------------------------------------------


def test_candidates_are_ranked_best_first_and_matches_obey_the_threshold() -> None:
    best = case(case_id="BASE-B")
    worse = case(
        case_id="BASE-A", name="POST /items — body.name — invalid field type"
    )

    row = map_requirements(
        [criterion(constraint="unspecified")], [worse, best], threshold=0.0
    ).rows[0]

    assert [c.case_id for c in row.candidates] == ["BASE-B", "BASE-A"]
    assert [c.rank for c in row.candidates] == [1, 2]
    assert row.candidates[0].score > row.candidates[1].score
    cutoff = row.candidates[0].score
    strict = map_requirements(
        [criterion(constraint="unspecified")], [worse, best], threshold=cutoff
    ).rows[0]
    assert [m.case_id for m in strict.matches] == ["BASE-B"]


def test_equal_scores_are_ordered_by_case_id() -> None:
    twins = [case(case_id="BASE-Z"), case(case_id="BASE-M")]

    row = map_requirements(
        [criterion(constraint="unspecified")], twins, threshold=0.0
    ).rows[0]

    assert [c.case_id for c in row.candidates] == ["BASE-M", "BASE-Z"]


def test_top_k_limits_the_mapped_tests() -> None:
    tests = [case(case_id=f"BASE-{letter}") for letter in "ABCDE"]

    row = map_requirements([criterion()], tests, threshold=0.0, top_k=2).rows[0]

    assert len(row.matches) == 2
    assert len(row.candidates) == 5


@pytest.mark.parametrize(
    ("threshold", "top_k"), [(-0.1, 3), (1.1, 3), (0.5, 0), (0.5, -1)]
)
def test_invalid_settings_are_rejected(threshold: float, top_k: int) -> None:
    with pytest.raises(MappingError):
        map_requirements([criterion()], [case()], threshold=threshold, top_k=top_k)


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(MappingError, match="criteria IDs"):
        map_requirements([criterion(), criterion()], [case()])
    with pytest.raises(MappingError, match="case IDs"):
        map_requirements([criterion()], [case(), case()])


def test_empty_inputs_do_not_crash() -> None:
    assert map_requirements([], [case()]).rows == []
    row = map_requirements([criterion()], []).rows[0]
    assert row.gap_reason == "no_test_for_operation"


# --- the real demo data ------------------------------------------------------


def test_every_criterion_has_a_row_and_each_row_is_mapped_or_explained(
    real: dict,
) -> None:
    result = real["result"]

    assert [row.requirement_id for row in result.rows] == [
        c.id for c in real["criteria"]
    ]
    for row in result.rows:
        assert bool(row.matches) != bool(row.gap_reason)
        if row.gap_reason:
            assert row.gap_detail


def test_logic_derived_gaps_on_the_demo_api(real: dict) -> None:
    """These gaps follow from what the baseline tests assert and generate."""
    result = real["result"]

    assert row_for(result, "REQ-002").gap_reason == "needs_response_body_check"
    assert row_for(result, "REQ-011").gap_reason == "needs_response_body_check"
    assert row_for(result, "REQ-012").gap_reason == "no_test_with_expected_status"
    assert row_for(result, "REQ-018").gap_reason == "no_test_with_expected_status"


def test_matches_never_cross_operations_or_statuses(real: dict) -> None:
    tests = {test.case_id: test for test in real["tests"]}
    for row in real["result"].rows:
        for match in row.matches:
            test = tests[match.case_id]
            assert (test.method, test.path) == (row.method, row.path)
            assert test.expected_status == row.expected_status


def test_clear_positive_examples_rank_first(real: dict) -> None:
    result = real["result"]
    expected_top = {
        "REQ-003": "minimum length 3",
        "REQ-004": "just below minimum length 3",
        "REQ-005": "just above maximum length 40",
        "REQ-007": "just outside maximum 10000.0",
        "REQ-009": "invalid enum value",
        "REQ-010": "body.name — missing required field",
        "REQ-014": "maximum boundary 1000",
        "REQ-016": "maximum boundary 10",
        "REQ-017": "just outside minimum 1",
    }
    for requirement, fragment in expected_top.items():
        assert fragment in row_for(result, requirement).candidates[0].name


def test_negative_example_other_fields_do_not_map_to_a_field_criterion(
    real: dict,
) -> None:
    row = row_for(real["result"], "REQ-010")

    assert row.candidates
    assert all("body.price" not in c.name for c in row.candidates)
    assert all("body.category" not in c.name for c in row.candidates)


def test_mapping_is_identical_across_runs(real: dict) -> None:
    again = map_requirements(real["criteria"], real["tests"])

    assert again.model_dump() == real["result"].model_dump()


def test_default_threshold_is_the_documented_provisional_value() -> None:
    assert DEFAULT_THRESHOLD == 0.4
    assert TfidfMapper().threshold == DEFAULT_THRESHOLD


# --- outputs -----------------------------------------------------------------


def test_reports_are_written_deterministically(tmp_path: Path, real: dict) -> None:
    first = write_reports(real["result"], tmp_path / "a")
    second = write_reports(real["result"], tmp_path / "b")

    assert set(first) == {"mapping.json", "traceability.json", "report.md"}
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes()
        assert first[name].read_bytes().endswith(b"\n")


def test_traceability_json_has_the_documented_shape(
    tmp_path: Path, real: dict
) -> None:
    paths = write_reports(real["result"], tmp_path)
    document = json.loads(paths["traceability.json"].read_text(encoding="utf-8"))

    assert set(document) == {"mapper", "threshold", "top_k", "summary", "requirements"}
    assert document["summary"] == {
        "criteria": 18,
        "tests": 43,
        "mapped": len(real["result"].mapped),
        "gaps": len(real["result"].gaps),
    }
    assert document["summary"]["mapped"] + document["summary"]["gaps"] == 18
    assert len(document["requirements"]) == 18


def test_report_lists_every_gap_with_its_reason(tmp_path: Path, real: dict) -> None:
    paths = write_reports(real["result"], tmp_path)
    report = paths["report.md"].read_text(encoding="utf-8")

    for row in real["result"].gaps:
        assert row.requirement_id in report.split("## Gaps")[1]
        assert row.gap_reason in report
    assert "not proof" in report


def test_mapping_file_is_accepted_by_the_runner_and_tags_tests(
    tmp_path: Path, real: dict
) -> None:
    paths = write_reports(real["result"], tmp_path / "reports")
    known = {test.case_id for test in real["tests"]}

    mapping = load_mapping(paths["mapping.json"], known)
    entries = export_tests(SPEC, tmp_path / "generated", paths["mapping.json"])

    assert mapping == real["result"].case_mapping()
    assert {entry.case_id for entry in entries if entry.requirements} == set(mapping)
    pairs = sum(len(requirements) for requirements in mapping.values())
    assert pairs == sum(len(row.matches) for row in real["result"].rows)
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "generated").glob("test_*.py")
    )
    assert sources.count("@pytest.mark.req(") == pairs


# --- the disabled LLM mapper -------------------------------------------------


def test_llm_mapper_is_disabled_and_makes_no_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    mapper = get_mapper("llm")

    assert isinstance(mapper, DisabledLLMMapper)
    with pytest.raises(LLMDisabledError, match="disabled"):
        mapper.map([criterion()], [case()])


def test_unknown_mapper_name_is_rejected() -> None:
    with pytest.raises(MappingError, match="unknown mapper"):
        get_mapper("magic")


# --- command line ------------------------------------------------------------


def test_cli_map_writes_reports_and_summarizes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(
        [
            "map", "--spec", str(SPEC), "--criteria", str(CRITERIA),
            "--output", str(tmp_path / "out"),
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "criteria" in output and "gaps" in output
    assert {path.name for path in (tmp_path / "out").iterdir()} == {
        "mapping.json", "traceability.json", "report.md",
    }


def test_cli_map_with_the_llm_mapper_fails_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(["map", "--mapper", "llm", "--output", str(tmp_path / "out")])

    assert code == 2
    assert "disabled" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("flag", "value"), [("--threshold", "2"), ("--top-k", "0")])
def test_cli_map_rejects_bad_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    code = cli_main(
        [
            "map", "--spec", str(SPEC), "--criteria", str(CRITERIA),
            "--output", str(tmp_path / "out"), flag, value,
        ]
    )

    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_map_reports_a_missing_criteria_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(
        [
            "map", "--spec", str(SPEC), "--criteria", str(tmp_path / "missing.yaml"),
            "--output", str(tmp_path / "out"),
        ]
    )

    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_map_then_run_with_the_mapping_reports_requirements(
    tmp_path: Path,
) -> None:
    assert cli_main(
        [
            "map", "--spec", str(SPEC), "--criteria", str(CRITERIA),
            "--output", str(tmp_path / "map"),
        ]
    ) == 0

    code = cli_main(
        [
            "run", "--spec", str(SPEC), "--output", str(tmp_path / "generated"),
            "--results", str(tmp_path / "results.json"),
            "--mapping", str(tmp_path / "map" / "mapping.json"),
        ]
    )

    assert code == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    tagged = [r for r in results["results"] if r["requirements"]]
    assert tagged
    assert all(r["outcome"] == "passed" for r in tagged)
    mapping = json.loads((tmp_path / "map" / "mapping.json").read_text("utf-8"))
    assert {r["case_id"] for r in tagged} == set(mapping)


def test_cli_help_lists_all_three_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli_main(["--help"])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("export", "run", "map"):
        assert command in help_text


# --- evaluation regressions: field scope, constraint direction, status ----------
#
# Found by comparing the mapper with hand labels on the development half. Each
# real-data test below finds its tests by structure (field, kind, name text), not
# by case ID, so it keeps working when IDs are regenerated.


def _candidate_ids(row) -> set[str]:
    return {candidate.case_id for candidate in row.candidates}


def _real_test(real: dict, **wanted) -> MappableTest:
    found = [
        test
        for test in real["tests"]
        if all(getattr(test, key) == value for key, value in wanted.items())
    ]
    assert len(found) == 1, f"expected one test for {wanted}, found {len(found)}"
    return found[0]


def test_explicit_status_criterion_maps_the_test_that_asserts_it(real: dict) -> None:
    health_ok = _real_test(real, path="/health", expected_status=200)
    row = row_for(real["result"], "REQ-001")

    assert [m.case_id for m in row.matches] == [health_ok.case_id]
    assert row.matches[0].basis == "explicit_status"
    assert row.matches[0].score == 1.0
    assert row.gap_reason is None


def test_a_missing_whole_body_test_does_not_verify_a_named_field(real: dict) -> None:
    missing_body = _real_test(
        real, method="POST", path="/products", field="body", kind="missing_required"
    )
    row = row_for(real["result"], "REQ-010")

    assert missing_body.case_id not in _candidate_ids(row)
    assert "body.name" in row.candidates[0].name  # the field-specific test remains
    assert "missing required field" in row.candidates[0].name


def test_opposite_bound_tests_never_compete_for_a_bound_criterion(real: dict) -> None:
    row = row_for(real["result"], "REQ-003")  # min_length 3, name accepted

    assert row.matches
    assert all("maximum" not in c.name for c in row.candidates)
    assert "minimum length 3" in row.matches[0].name


def test_no_real_criterion_is_mapped_to_a_test_of_the_opposite_bound(
    real: dict,
) -> None:
    lower = {"min_length", "greater_than", "greater_than_or_equal"}
    upper = {"max_length", "less_than", "less_than_or_equal"}
    for criterion_ in real["criteria"]:
        row = row_for(real["result"], criterion_.id)
        opposite = "maximum" if criterion_.constraint in lower else "minimum"
        if criterion_.constraint not in lower | upper:
            continue
        for candidate in row.candidates:
            assert opposite not in candidate.name.split(" — ")[-1], (
                criterion_.id,
                candidate.name,
            )


def test_the_remaining_real_gaps_are_unchanged(real: dict) -> None:
    gaps = {row.requirement_id: row.gap_reason for row in real["result"].gaps}

    assert gaps == {
        "REQ-002": "needs_response_body_check",
        "REQ-008": "below_threshold",
        "REQ-011": "needs_response_body_check",
        "REQ-012": "no_test_with_expected_status",
        "REQ-018": "no_test_with_expected_status",
    }


# status criteria


def _status_criterion(**overrides) -> Criterion:
    values = {
        "id": "REQ-200",
        "method": "GET",
        "path": "/ping",
        "field": "response.status",
        "constraint": "equals",
        "value": 200,
        "expected_status": 200,
        "description": "Ping returns a successful status.",
    }
    return Criterion(**{**values, **overrides})


def _ping_test(**overrides) -> MappableTest:
    values = {
        "case_id": "BASE-P",
        "name": "GET /ping — request — valid request",
        "method": "GET",
        "path": "/ping",
        "field": "request",
        "kind": "valid",
        "expected_valid": True,
        "expected_status": 200,
        "checks": ("status",),
    }
    return case(**{**values, **overrides})


def test_explicit_status_match_scores_one_even_with_no_shared_words() -> None:
    row = map_requirements([_status_criterion()], [_ping_test()]).rows[0]

    assert [m.case_id for m in row.matches] == ["BASE-P"]
    assert (row.matches[0].score, row.matches[0].basis) == (1.0, "explicit_status")


def test_status_criterion_needs_the_test_to_assert_status() -> None:
    no_status = _ping_test(checks=("header",))

    row = map_requirements([_status_criterion()], [no_status], threshold=0.99).rows[0]

    assert row.candidates[0].basis == "tfidf"
    assert row.gap_reason == "below_threshold"


def test_status_criterion_value_must_equal_the_status_the_test_asserts() -> None:
    inconsistent = _status_criterion(value=204)  # filter passes on 200, value differs

    row = map_requirements([inconsistent], [_ping_test()], threshold=0.99).rows[0]

    assert row.candidates[0].basis == "tfidf"


def test_status_criterion_ignores_the_tests_request_field() -> None:
    field_test = _ping_test(field="body.name", name="GET /ping — body.name — x")

    row = map_requirements([_status_criterion()], [field_test]).rows[0]

    assert row.matches[0].basis == "explicit_status"


def test_only_equals_on_response_status_is_an_explicit_status_check() -> None:
    other = _status_criterion(constraint="not_found", value=200)

    row = map_requirements([other], [_ping_test()], threshold=0.0).rows[0]

    assert all(c.basis == "tfidf" for c in row.candidates)


def test_status_criterion_still_obeys_operation_and_status_filters() -> None:
    other_operation = _ping_test(path="/pong")
    other_status = _ping_test(case_id="BASE-Q", expected_status=204)

    row = map_requirements([_status_criterion()], [other_operation, other_status]).rows[
        0
    ]

    assert row.matches == []
    assert row.gap_reason in {"no_test_for_operation", "no_test_with_expected_status"}


def test_explicit_status_ranks_first_and_top_k_still_applies() -> None:
    tests = [_ping_test(case_id=f"BASE-{letter}") for letter in "CBA"]

    row = map_requirements([_status_criterion()], tests, top_k=2).rows[0]

    assert [m.case_id for m in row.matches] == ["BASE-A", "BASE-B"]  # tie: case ID
    assert [c.case_id for c in row.candidates] == ["BASE-A", "BASE-B", "BASE-C"]


def test_tfidf_matches_are_labelled_as_such_in_the_reports(
    real: dict, tmp_path: Path
) -> None:
    paths = write_reports(real["result"], tmp_path)
    trace = json.loads(paths["traceability.json"].read_text(encoding="utf-8"))
    bases = {
        match["basis"] for row in trace["requirements"] for match in row["matches"]
    }

    assert bases == {"tfidf", "explicit_status"}


# field scope


def _field_gap(test_field: str, name: str, **criterion_overrides):
    test = case(field=test_field, name=name)
    return map_requirements(
        [criterion(**criterion_overrides)], [test], threshold=0.0
    ).rows[0]


def test_a_body_only_test_names_no_field_and_is_not_eligible() -> None:
    row = _field_gap("body", "POST /items — body — missing required JSON body")

    assert row.gap_reason == "no_test_for_field"
    assert "body" in (row.gap_detail or "")


def test_a_path_only_test_names_no_field_and_is_not_eligible() -> None:
    row = _field_gap("path", "POST /items — path — x")

    assert row.gap_reason == "no_test_for_field"


def test_a_whole_request_test_remains_eligible_for_a_named_field() -> None:
    row = _field_gap("request", "POST /items — request — valid request")

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_a_criterion_without_field_words_accepts_a_body_only_test() -> None:
    row = _field_gap(
        "body", "POST /items — body — missing required JSON body", field="response"
    )

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_the_same_named_field_is_eligible_and_a_different_one_is_not() -> None:
    same = case(case_id="BASE-S", field="body.name")
    other = case(
        case_id="BASE-O", field="body.price", name="POST /items — body.price — x"
    )

    row = map_requirements([criterion()], [same, other], threshold=0.0).rows[0]

    assert _candidate_ids(row) == {"BASE-S"}


# constraint direction


def _bound_row(constraint: str, test_name: str, **criterion_overrides):
    test = case(name=test_name)
    return map_requirements(
        [criterion(constraint=constraint, **criterion_overrides)], [test], threshold=0.0
    ).rows[0]


@pytest.mark.parametrize(
    "constraint,test_name",
    [
        ("min_length", "POST /items — body.name — maximum length 40"),
        ("min_length", "POST /items — body.name — just above maximum length 40"),
        ("max_length", "POST /items — body.name — just below minimum length 3"),
        ("greater_than", "POST /items — body.name — just outside maximum 10000.0"),
        ("greater_than_or_equal", "POST /items — body.name — maximum boundary 5"),
        (
            "less_than_or_equal",
            "POST /items — body.name — exclusive minimum boundary 0",
        ),
        ("less_than", "POST /items — body.name — just outside minimum 1"),
    ],
)
def test_opposing_bounds_are_not_eligible(constraint: str, test_name: str) -> None:
    row = _bound_row(constraint, test_name)

    assert row.gap_reason == "no_test_for_constraint"
    assert row.candidates == []
    assert test_name in (row.gap_detail or "")


@pytest.mark.parametrize(
    "constraint,test_name",
    [
        ("min_length", "POST /items — body.name — just below minimum length 3"),
        ("min_length", "POST /items — body.name — minimum length 3"),
        ("max_length", "POST /items — body.name — just above maximum length 40"),
        (
            "greater_than",
            "POST /items — body.name — just outside exclusive minimum 0.0",
        ),
        ("less_than_or_equal", "POST /items — body.name — maximum boundary 10"),
    ],
)
def test_matching_bounds_stay_eligible(constraint: str, test_name: str) -> None:
    row = _bound_row(constraint, test_name)

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


@pytest.mark.parametrize(
    "test_name",
    [
        "POST /items — body.name — invalid field type",
        "POST /items — body.name — missing required field",
        "POST /items — body.name — between minimum and maximum length",
    ],
)
def test_tests_with_no_single_bound_direction_are_not_filtered(
    test_name: str,
) -> None:
    row = _bound_row("min_length", test_name)

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_a_non_bound_criterion_is_not_filtered_by_a_tests_bound() -> None:
    row = _bound_row(
        "enum", "POST /items — body.name — maximum length 40", value=["a", "b"]
    )

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_only_the_case_description_is_read_for_the_bound_direction() -> None:
    # "minimum" appears in the path part of the name; the case itself is a maximum.
    test = case(name="POST /minimum-order — body.name — just above maximum length 40")
    row = map_requirements(
        [criterion(path="/items", constraint="max_length")], [test], threshold=0.0
    ).rows[0]

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


def test_a_better_matching_direction_wins_over_an_opposite_one() -> None:
    opposite = case(
        case_id="BASE-X", name="POST /items — body.name — maximum length 40"
    )
    right = case(case_id="BASE-Y")

    row = map_requirements([criterion()], [opposite, right], threshold=0.0).rows[0]

    assert [c.case_id for c in row.candidates] == ["BASE-Y"]


@pytest.mark.parametrize("constraint", ["min_length", "max_length"])
def test_a_name_mentioning_both_directions_is_not_filtered_either_way(
    constraint: str,
) -> None:
    row = _bound_row(
        constraint, "POST /items — body.name — between minimum and maximum length"
    )

    assert [c.case_id for c in row.candidates] == ["BASE-A"]


# --- report.md distinguishes rule-based matches from text similarity ------------


def _report_cells(report: str) -> dict[str, list[str]]:
    """Requirement ID -> the match entries in its traceability-table cell."""
    cells: dict[str, list[str]] = {}
    for line in report.split("## Gaps")[0].splitlines():
        if line.startswith("| REQ-"):
            requirement, cell = [part.strip() for part in line.strip("|").split("|", 1)]
            cells[requirement] = [] if cell == "—" else cell.split("<br>")
    return cells


def test_report_marks_explicit_status_matches_and_leaves_tfidf_ones_plain(
    real: dict, tmp_path: Path
) -> None:
    paths = write_reports(real["result"], tmp_path)
    cells = _report_cells(paths["report.md"].read_text(encoding="utf-8"))
    trace = json.loads(paths["traceability.json"].read_text(encoding="utf-8"))

    explicit_seen = 0
    for row in trace["requirements"]:
        entries = cells[row["requirement_id"]]
        assert len(entries) == len(row["matches"])
        for match, entry in zip(row["matches"], entries, strict=True):
            assert entry.startswith(match["case_id"])
            if match["basis"] == "explicit_status":
                explicit_seen += 1
                assert f"({match['score']:.4f}, explicit_status)" in entry
            else:
                assert f"({match['score']:.4f})" in entry
                assert "explicit_status" not in entry
    assert explicit_seen >= 1  # the demo's REQ-001


def test_report_and_traceability_json_agree_on_which_matches_are_explicit(
    real: dict, tmp_path: Path
) -> None:
    paths = write_reports(real["result"], tmp_path)
    report = paths["report.md"].read_text(encoding="utf-8")
    trace = json.loads(paths["traceability.json"].read_text(encoding="utf-8"))

    in_json = sorted(
        (row["requirement_id"], match["case_id"])
        for row in trace["requirements"]
        for match in row["matches"]
        if match["basis"] == "explicit_status"
    )
    in_report = sorted(
        (requirement, entry.split(" ")[0])
        for requirement, entries in _report_cells(report).items()
        for entry in entries
        if "explicit_status" in entry
    )
    assert in_report == in_json


def test_report_explains_the_marker_in_a_legend(real: dict, tmp_path: Path) -> None:
    report = write_reports(real["result"], tmp_path)["report.md"].read_text(
        encoding="utf-8"
    )
    legend = report.split("## Traceability")[1].split("| Requirement |")[0]

    assert "explicit_status" in legend
    assert "TF-IDF" in legend
    assert "traceability.json" in legend


def test_a_synthetic_explicit_match_is_marked_in_the_report(tmp_path: Path) -> None:
    result = map_requirements([_status_criterion()], [_ping_test()])

    report = write_reports(result, tmp_path)["report.md"].read_text(encoding="utf-8")

    assert _report_cells(report)["REQ-200"] == [
        "BASE-P (1.0000, explicit_status) GET /ping — request — valid request"
    ]


def test_a_report_without_explicit_matches_marks_nothing(tmp_path: Path) -> None:
    result = map_requirements([criterion()], [case()], threshold=0.0)

    report = write_reports(result, tmp_path)["report.md"].read_text(encoding="utf-8")
    cells = _report_cells(report)

    assert cells["REQ-100"]
    assert all("explicit_status" not in entry for entry in cells["REQ-100"])


def test_marking_the_report_does_not_change_scores_or_matching(real: dict) -> None:
    """The explicit rule's score is still 1.0 and still ranks first."""
    row = row_for(real["result"], "REQ-001")

    assert row.matches[0].score == 1.0
    assert row.matches[0].rank == 1


# --- constraint direction is read from test names: guard the naming convention ----

def test_every_baseline_boundary_test_has_a_recognised_direction(real: dict) -> None:
    """Direction filtering reads the generated names. If a rename breaks that, fail.

    A boundary test with no recognised direction would silently skip the opposing-
    bound filter, so this fails loudly instead of letting the rule quietly stop
    working.
    """
    boundary = [test for test in real["tests"] if test.kind == "boundary"]
    unrecognised = [t.name for t in boundary if _test_bound(t) is None]

    assert boundary, "the demo spec should generate boundary tests"
    assert not unrecognised, (
        "these boundary tests have no single minimum/maximum direction, so the "
        "opposing-bound filter would not apply to them (did the generator's "
        f"wording change?): {unrecognised}"
    )


def test_baseline_boundary_directions_match_their_wording(real: dict) -> None:
    seen = set()
    for test in real["tests"]:
        if test.kind != "boundary":
            continue
        tail = test.name.split(" — ")[-1].lower()
        direction = _test_bound(test)
        expected = "lower" if "minimum" in tail else "upper"
        assert direction == expected, (test.name, direction)
        seen.add(direction)
    assert seen == {"lower", "upper"}


def test_no_non_boundary_baseline_test_is_given_a_direction(real: dict) -> None:
    wrongly_directed = [
        t.name for t in real["tests"] if t.kind != "boundary" and _test_bound(t)
    ]

    assert not wrongly_directed, wrongly_directed


@pytest.mark.parametrize(
    "tail,expected",
    [
        ("just below minimum length 3", "lower"),
        ("minimum length 3", "lower"),
        ("just inside exclusive minimum 0.0", "lower"),
        ("minimum boundary 1", "lower"),
        ("just above maximum length 40", "upper"),
        ("maximum length 40", "upper"),
        ("just outside exclusive maximum 9", "upper"),
        ("MAXIMUM boundary 10", "upper"),
        # ambiguous or unknown wording has no direction (and is never filtered)
        ("between minimum and maximum length", None),
        ("minimum then maximum", None),
        ("lower bound exceeded", None),
        ("invalid field type", None),
        ("missing required field", None),
        ("minimums", None),
    ],
)
def test_direction_from_wording_including_ambiguous_names(
    tail: str, expected: str | None
) -> None:
    test = case(name=f"POST /items — body.name — {tail}")

    assert _test_bound(test) == expected


def test_only_the_case_part_of_a_name_sets_the_direction() -> None:
    assert _test_bound(case(name="POST /minimum — body.name — maximum length 40")) == (
        "upper"
    )
    assert _test_bound(case(name="POST /maximum — body.name — valid input")) is None
