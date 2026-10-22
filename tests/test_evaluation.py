"""Evaluation harness: sampling, label validation, mapping scores, mutants."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assay import cli, evaluation, runner
from assay.demo_api import MUTANTS, create_app
from assay.mapping import (
    baseline_tests_from_spec,
    load_criteria,
    map_requirements,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "specs" / "openapi.json"
CRITERIA = ROOT / "criteria.yaml"

# Labels are only valid for these exact generated tests. If this fails, the case
# IDs changed: version the change and relabel rather than editing this value.
FROZEN_CASE_ID_FINGERPRINT = (
    "6b044adc502881259b729313410abd702b43603890fad93ed891afdabf879422"
)

# The labelled sheet's pairs, in sheet order, with their dev/test assignment. Moving
# a pair between halves, adding, removing or reordering pairs fails the integrity
# test. Pinning this is deliberate: the split is what keeps the threshold choice
# (dev) apart from the honest estimate (test). It is a frozen record of the sheet
# as drawn, so it does not follow later changes to the mapper or the sampler.
FROZEN_PAIR_SPLITS = [
    ("REQ-017|BASE-63501DB1C1E5", "test"),
    ("REQ-007|BASE-24F2A691793C", "test"),
    ("REQ-001|BASE-ACCE3364DDBF", "dev"),
    ("REQ-008|BASE-C45605A49051", "test"),
    ("REQ-008|BASE-BD70624B4DB6", "dev"),
    ("REQ-010|BASE-25A83939B42E", "dev"),
    ("REQ-003|BASE-BABD40AD7654", "dev"),
    ("REQ-003|BASE-BD70624B4DB6", "test"),
    ("REQ-001|BASE-B7511B99FBAF", "dev"),
    ("REQ-009|BASE-FFE9E6656AED", "test"),
    ("REQ-015|BASE-C45605A49051", "test"),
    ("REQ-006|BASE-9FE18F7BA1D4", "test"),
    ("REQ-006|BASE-C1EB78F90681", "dev"),
    ("REQ-005|BASE-BD70624B4DB6", "test"),
    ("REQ-008|BASE-D9059F5116BA", "dev"),
    ("REQ-007|BASE-EA9F63A8D176", "test"),
    ("REQ-010|BASE-B7053FFDC7A3", "dev"),
    ("REQ-009|BASE-F57DEC78BF68", "test"),
    ("REQ-012|BASE-ADA325A9C3A0", "dev"),
    ("REQ-005|BASE-E205982C270D", "test"),
    ("REQ-007|BASE-DC36D6CBBDD1", "dev"),
    ("REQ-004|BASE-61572B0F8204", "test"),
    ("REQ-002|BASE-ACCE3364DDBF", "dev"),
    ("REQ-002|BASE-C5963F15308C", "test"),
    ("REQ-013|BASE-C45605A49051", "test"),
    ("REQ-003|BASE-25A83939B42E", "dev"),
    ("REQ-011|BASE-0AE43E8BB6A2", "test"),
    ("REQ-005|BASE-24F2A691793C", "dev"),
    ("REQ-006|BASE-A8E377FB5739", "dev"),
    ("REQ-004|BASE-AD0DB6BE77C6", "test"),
    ("REQ-014|BASE-BABD40AD7654", "dev"),
    ("REQ-006|BASE-C0F225F6EED3", "test"),
    ("REQ-007|BASE-7E53008D1DAE", "dev"),
    ("REQ-009|BASE-BD70624B4DB6", "test"),
    ("REQ-004|BASE-77E133B408A1", "dev"),
    ("REQ-004|BASE-24F2A691793C", "dev"),
    ("REQ-013|BASE-69E516887E11", "test"),
    ("REQ-003|BASE-C5963F15308C", "test"),
    ("REQ-005|BASE-7D96379F7C04", "dev"),
    ("REQ-016|BASE-DC36D6CBBDD1", "dev"),
]


@pytest.fixture(scope="module")
def tests_and_criteria():
    spec = runner.load_spec(SPEC)
    return baseline_tests_from_spec(spec), load_criteria(CRITERIA)


# --- the freeze ----------------------------------------------------------------


def test_case_ids_are_frozen(tests_and_criteria):
    tests, _ = tests_and_criteria
    assert evaluation.case_id_fingerprint(tests) == FROZEN_CASE_ID_FINGERPRINT


def test_fingerprint_changes_when_an_id_changes(tests_and_criteria):
    tests, _ = tests_and_criteria
    altered = [tests[0].model_copy(update={"case_id": "BASE-000000000000"})]
    altered += tests[1:]
    assert evaluation.case_id_fingerprint(altered) != FROZEN_CASE_ID_FINGERPRINT


def _read_committed_sheet(path=None):
    """The committed sheet's rows. A BOM, as a spreadsheet may add, is tolerated."""
    sheet = path or ROOT / "data" / "labelled_pairs.csv"
    with Path(sheet).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _assert_sheet_intact(rows, tests, criteria):
    """Check a label sheet against the frozen record and the current data.

    Labels and notes are the human's and are not compared. Every other column must
    equal what the current criteria and tests say, and the (pair, split) sequence
    must equal the frozen pins. Nothing here uses the sampler or the mapper, so a
    mapper improvement cannot break it.
    """
    criterion_by_id = {item.id: item for item in criteria}
    test_by_id = {item.case_id: item for item in tests}

    assert [(r["pair_id"], r["split"]) for r in rows] == FROZEN_PAIR_SPLITS

    compared = set()
    for row in rows:
        criterion = criterion_by_id[row["requirement_id"]]
        test = test_by_id[row["case_id"]]
        expected = {
            "pair_id": f"{criterion.id}|{test.case_id}",
            "requirement_id": criterion.id,
            "criterion_description": criterion.description,
            "criterion_field": criterion.field,
            "criterion_constraint": criterion.constraint,
            "criterion_value": json.dumps(criterion.value, sort_keys=True),
            "criterion_expected_status": str(criterion.expected_status),
            "case_id": test.case_id,
            "test_name": test.name,
            "test_kind": test.kind,
            "test_field": test.field,
            "test_expected_status": str(test.expected_status),
            "test_checks": "+".join(test.checks),
        }
        compared |= set(expected)
        for column, value in expected.items():
            assert row[column] == value, (row["pair_id"], column, row[column], value)
        assert row["label"] in ("", "0", "1"), (row["pair_id"], row["label"])
    # A column added to the sheet must be compared here or declared human-only.
    assert compared | {"split", "label", "notes"} == set(evaluation.LABEL_COLUMNS)


def test_committed_sheet_is_frozen_and_consistent_with_the_current_data(
    tests_and_criteria,
):
    """The labelled sheet is a frozen record, not something to redraw."""
    tests, criteria = tests_and_criteria
    committed = _read_committed_sheet()

    assert len(committed) == len(FROZEN_PAIR_SPLITS)
    assert len(committed) == sum(evaluation.DEFAULT_COUNTS.values())
    _assert_sheet_intact(committed, tests, criteria)

    meta = json.loads((ROOT / "data" / "labelled_pairs.meta.json").read_text())
    assert meta["case_id_fingerprint"] == FROZEN_CASE_ID_FINGERPRINT
    assert meta["pairs"] == len(committed)
    counts = {
        split: sum(1 for _, name in FROZEN_PAIR_SPLITS if name == split)
        for split in evaluation.SPLITS
    }
    assert meta["splits"] == counts


def _tampered(edit):
    rows = [dict(row) for row in _read_committed_sheet()]
    edit(rows)
    return rows


def _flip_split(rows):
    rows[0]["split"] = "dev" if rows[0]["split"] == "test" else "test"


def _swap_rows(rows):
    rows[0], rows[1] = rows[1], rows[0]


@pytest.mark.parametrize(
    "edit",
    [
        _flip_split,  # moving one pair between dev and test
        _swap_rows,  # reordering
        lambda rows: rows.pop(),  # removing a pair
        lambda rows: rows.append(dict(rows[0])),  # duplicating a pair
        lambda rows: rows[3].update(label="maybe"),  # a label that is not 1 or 0
    ],
    ids=["flip-split", "reorder", "remove", "duplicate", "bad-label"],
)
def test_the_sheet_integrity_check_rejects_structural_tampering(
    tests_and_criteria, edit
):
    tests, criteria = tests_and_criteria

    with pytest.raises(AssertionError):
        _assert_sheet_intact(_tampered(edit), tests, criteria)


@pytest.mark.parametrize(
    "column",
    [name for name in evaluation.LABEL_COLUMNS if name not in ("label", "notes")],
)
def test_changing_any_non_label_column_is_rejected(tests_and_criteria, column):
    """Appending to a value always changes it, so no edit can be a silent no-op."""
    tests, criteria = tests_and_criteria

    def edit(rows):
        rows[3][column] += "!"

    with pytest.raises((AssertionError, KeyError)):
        _assert_sheet_intact(_tampered(edit), tests, criteria)


def test_labels_and_notes_may_change_without_failing_the_integrity_check(
    tests_and_criteria,
):
    tests, criteria = tests_and_criteria

    def fill(rows):
        for index, row in enumerate(rows):
            row["label"] = "10"[index % 2]
            row["notes"] = "unsure" if index % 7 == 0 else ""

    _assert_sheet_intact(_tampered(fill), tests, criteria)


def test_the_integrity_check_does_not_depend_on_the_mapper_or_the_sampler(
    tests_and_criteria, monkeypatch
):
    """A mapper that returned nothing, and a sampler that raised, change nothing."""
    tests, criteria = tests_and_criteria

    def fail(*args, **kwargs):
        raise AssertionError("integrity check must not call the mapper or sampler")

    monkeypatch.setattr(evaluation, "map_requirements", fail)
    monkeypatch.setattr(evaluation, "sample_pairs", fail)

    _assert_sheet_intact(_read_committed_sheet(), tests, criteria)


# --- sampling ------------------------------------------------------------------


def test_sample_is_deterministic_and_seed_dependent(tests_and_criteria):
    tests, criteria = tests_and_criteria
    first = evaluation.sample_pairs(criteria, tests, seed=1)
    assert first == evaluation.sample_pairs(criteria, tests, seed=1)
    other = evaluation.sample_pairs(criteria, tests, seed=2)
    assert [r["pair_id"] for r in first] != [r["pair_id"] for r in other]


def test_sample_size_strata_and_splits(tests_and_criteria):
    tests, criteria = tests_and_criteria
    rows = evaluation.sample_pairs(criteria, tests)
    assert len(rows) == sum(evaluation.DEFAULT_COUNTS.values()) == 40
    assert len({r["pair_id"] for r in rows}) == 40
    result = map_requirements(criteria, tests)
    strata = evaluation.classify_pairs(criteria, tests, result)
    counted: dict[str, int] = {}
    for row in rows:
        name = strata[(row["requirement_id"], row["case_id"])]
        counted[name] = counted.get(name, 0) + 1
    assert counted == evaluation.DEFAULT_COUNTS
    splits = [r["split"] for r in rows]
    assert abs(splits.count("dev") - splits.count("test")) <= 3
    for stratum in evaluation.DEFAULT_COUNTS:
        halves = {
            split: sum(
                1
                for r in rows
                if r["split"] == split
                and strata[(r["requirement_id"], r["case_id"])] == stratum
            )
            for split in evaluation.SPLITS
        }
        assert abs(halves["dev"] - halves["test"]) <= 1


def test_sample_spreads_across_criteria(tests_and_criteria):
    tests, criteria = tests_and_criteria
    rows = evaluation.sample_pairs(criteria, tests)
    assert len({r["requirement_id"] for r in rows}) >= 15


def test_sheet_is_blind_to_the_mapper(tests_and_criteria):
    """No score, rank, eligibility or stratum column may reach the labeller."""
    for name in evaluation.LABEL_COLUMNS:
        for banned in ("score", "rank", "eligible", "stratum", "mapped", "predicted"):
            assert banned not in name
    tests, criteria = tests_and_criteria
    for row in evaluation.sample_pairs(criteria, tests):
        assert row["label"] == "" and row["notes"] == ""


def test_sample_rejects_unknown_stratum(tests_and_criteria):
    tests, criteria = tests_and_criteria
    with pytest.raises(evaluation.EvaluationError):
        evaluation.sample_pairs(criteria, tests, counts={"nonsense": 3})


def test_classify_pairs_on_real_data(tests_and_criteria):
    tests, criteria = tests_and_criteria
    result = map_requirements(criteria, tests)
    strata = evaluation.classify_pairs(criteria, tests, result)
    assert len(strata) == len(tests) * len(criteria)
    health = next(t for t in tests if t.path == "/health")
    assert strata[("REQ-001", health.case_id)] == "eligible"
    assert strata[("REQ-003", health.case_id)] == "other_operation"
    # REQ-002 has same-operation tests, but the rules ruled them all out.
    assert strata[("REQ-002", health.case_id)] == "same_operation"


# --- label sheets --------------------------------------------------------------


def _filled_sheet(tmp_path, tests, criteria, label_for):
    rows = evaluation.sample_pairs(criteria, tests)
    for row in rows:
        row["label"] = label_for(row)
    sheet, meta = tmp_path / "labels.csv", tmp_path / "meta.json"
    evaluation.write_label_sheet(rows, sheet, meta, tests=tests, seed=1)
    return sheet, meta, rows


def _rewrite(sheet, edit):
    with sheet.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        names = reader.fieldnames
    edit(rows)
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_filled_sheet_loads(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, rows = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    loaded = evaluation.load_labels(sheet, criteria, tests, meta)
    assert len(loaded) == len(rows)


@pytest.mark.parametrize(
    "edit,message",
    [
        (lambda rows: rows[0].update(label=""), "label must be 1 or 0"),
        (lambda rows: rows[0].update(label="yes"), "label must be 1 or 0"),
        (lambda rows: rows[0].update(split="train"), "split"),
        (lambda rows: rows[0].update(requirement_id="REQ-999"), "unknown requirement"),
        (lambda rows: rows[0].update(case_id="BASE-FFFFFFFFFFFF"), "unknown case"),
        (lambda rows: rows[0].update(pair_id="x|y"), "pair_id"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate"),
    ],
)
def test_bad_label_sheets_are_rejected(tmp_path, tests_and_criteria, edit, message):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    _rewrite(sheet, edit)
    with pytest.raises(evaluation.EvaluationError, match=message):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_sheet_with_missing_column_or_no_rows_is_rejected(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet = tmp_path / "bad.csv"
    sheet.write_text("pair_id,label\n", encoding="utf-8")
    with pytest.raises(evaluation.EvaluationError, match="missing columns"):
        evaluation.load_labels(sheet, criteria, tests)
    sheet.write_text(",".join(evaluation.LABEL_COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(evaluation.EvaluationError, match="no labelled pairs"):
        evaluation.load_labels(sheet, criteria, tests)


def test_labels_made_for_different_test_ids_are_rejected(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    data = json.loads(meta.read_text())
    data["case_id_fingerprint"] = "0" * 64
    meta.write_text(json.dumps(data))
    with pytest.raises(evaluation.EvaluationError, match="test IDs changed"):
        evaluation.load_labels(sheet, criteria, tests, meta)


# --- mapping evaluation --------------------------------------------------------


def _oracle(tests, criteria, threshold=0.4):
    result = map_requirements(criteria, tests, threshold=threshold)
    predicted = {(r.requirement_id, m.case_id) for r in result.rows for m in r.matches}
    return (
        lambda row: "1" if (row["requirement_id"], row["case_id"]) in predicted else "0"
    )


def test_a_perfect_labeller_gives_perfect_scores(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, _oracle(tests, criteria))
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    for split in evaluation.SPLITS:
        scores = report["configured_threshold"][split]
        assert scores["precision"] in (1.0, None)
        assert scores["recall"] in (1.0, None)
        assert scores["false_positives"] == scores["false_negatives"] == 0
    assert report["labelled_pairs"] == 40


def test_an_opposite_labeller_gives_zero_scores(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    oracle = _oracle(tests, criteria)
    sheet, meta, _ = _filled_sheet(
        tmp_path, tests, criteria, lambda r: "0" if oracle(r) == "1" else "1"
    )
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    for split in evaluation.SPLITS:
        scores = report["configured_threshold"][split]
        assert scores["true_positives"] == 0
        assert scores["precision"] in (0.0, None)


def test_sweep_uses_dev_labels_only(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    oracle = _oracle(tests, criteria)
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, oracle)
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    base = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    flipped = [
        {**r, "label": "1" if r["label"] == "0" else "0"} if r["split"] == "test" else r
        for r in rows
    ]
    again = evaluation.evaluate_mapping(
        flipped, criteria, tests, threshold=0.4, top_k=3
    )
    assert again["dev_sweep"] == base["dev_sweep"]
    assert again["dev_chosen_threshold"] == base["dev_chosen_threshold"]
    assert again["configured_threshold"]["test"] != base["configured_threshold"]["test"]


def test_report_shape_and_markdown(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, _oracle(tests, criteria))
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    assert [e["threshold"] for e in report["dev_sweep"]] == list(
        evaluation.SWEEP_THRESHOLDS
    )
    assert set(report["by_stratum"]) <= set(evaluation.DEFAULT_COUNTS)
    text = evaluation.mapping_markdown(report)
    assert "stratified sample" in text and "Dev sweep" in text


# --- mutants in the demo API ---------------------------------------------------

_PRODUCT = {"name": "Notebook", "price": 5.5, "category": "book"}

# mutant -> (method, path, json body, status on the real API, status on the mutant)
_MUTANT_PROBES = {
    "name_min_length_2": ("POST", "/products", {**_PRODUCT, "name": "ab"}, 422, 201),
    "name_max_length_41": (
        "POST",
        "/products",
        {**_PRODUCT, "name": "x" * 41},
        422,
        201,
    ),
    "price_allows_zero": ("POST", "/products", {**_PRODUCT, "price": 0}, 422, 201),
    "price_max_exclusive": (
        "POST",
        "/products",
        {**_PRODUCT, "price": 10000},
        201,
        422,
    ),
    "category_accepts_toy": (
        "POST",
        "/products",
        {**_PRODUCT, "category": "toy"},
        422,
        201,
    ),
    "stock_allows_negative_one": (
        "PATCH",
        "/products/1/stock",
        {"quantity": -1},
        422,
        200,
    ),
    "order_quantity_max_11": (
        "POST",
        "/orders",
        {"product_id": 1, "quantity": 11},
        422,
        201,
    ),
    "get_unknown_product_200": ("GET", "/products/99999", None, 404, 200),
    "order_unknown_product_201": (
        "POST",
        "/orders",
        {"product_id": 99999, "quantity": 1},
        404,
        201,
    ),
}


def _call(client, method, path, body):
    return client.request(method, path, json=body).status_code


def test_mutant_registry_size_and_coverage_of_probes():
    assert 5 <= len(MUTANTS) <= 10
    assert set(_MUTANT_PROBES) | {"health_body_wrong"} == set(MUTANTS)


@pytest.mark.parametrize("name", sorted(_MUTANT_PROBES))
def test_each_mutant_changes_behaviour_and_the_real_api_does_not(name):
    method, path, body, real, mutated = _MUTANT_PROBES[name]
    assert _call(TestClient(create_app()), method, path, body) == real
    assert _call(TestClient(create_app(mutant=name)), method, path, body) == mutated


def test_health_body_mutant():
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}
    mutated = TestClient(create_app(mutant="health_body_wrong"))
    assert mutated.get("/health").json() == {"status": "up"}


def test_mutants_are_off_by_default():
    client = TestClient(create_app())
    assert _call(client, "POST", "/products", {**_PRODUCT, "name": "ab"}) == 422
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/products/99999").status_code == 404


def test_unknown_mutant_is_rejected_with_the_known_names():
    with pytest.raises(ValueError, match="name_min_length_2"):
        create_app(mutant="nope")


def test_runner_clears_a_stray_mutant_variable(tmp_path, monkeypatch):
    """A leftover ASSAY_MUTANT in the shell must not turn a mutant on silently."""
    monkeypatch.setenv("ASSAY_MUTANT", "name_min_length_2")
    entries = runner.export_tests(SPEC, tmp_path / "suite")
    report = runner.run_tests(tmp_path / "suite", entries, tmp_path / "r.json")
    assert report["summary"]["passed"] == report["summary"]["total"]


# --- mutation evaluation -------------------------------------------------------

# Characterisation of today's baseline suite on these ten hand-written mutants.
# These pin the *current* generated tests, not a claim about test quality in general.
_KILLED_BY_BASELINE = {
    "name_max_length_41",
    "name_min_length_2",
    "order_quantity_max_11",
    "price_allows_zero",
    "price_max_exclusive",
    "stock_allows_negative_one",
}


@pytest.fixture(scope="module")
def full_mutation_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("mutation")
    return evaluation.evaluate_mutants(SPEC, base / "suite", base / "out")


def test_known_mutants_are_killed_and_known_survivors_survive(full_mutation_run):
    killed = {m["mutant"] for m in full_mutation_run["mutants"] if m["killed"]}
    assert killed == _KILLED_BY_BASELINE
    assert set(full_mutation_run["score"]["survived"]) == set(MUTANTS) - killed


def test_mutation_result_shape(full_mutation_run):
    assert (
        full_mutation_run["control"]["passed"] == full_mutation_run["control"]["total"]
    )
    score = full_mutation_run["score"]
    assert score["executed"] == len(MUTANTS)
    assert score["killed"] == len(_KILLED_BY_BASELINE)
    assert score["mutation_score"] == len(_KILLED_BY_BASELINE) / len(MUTANTS)
    names = [m["mutant"] for m in full_mutation_run["mutants"]]
    assert names == sorted(names)
    for record in full_mutation_run["mutants"]:
        assert record["killed"] == bool(record["detecting_cases"])
        assert record["detecting_cases"] == sorted(record["detecting_cases"])


def test_selecting_one_mutant_runs_only_that_one(tmp_path):
    result = evaluation.evaluate_mutants(
        SPEC, tmp_path / "suite", tmp_path / "out", selected=["price_allows_zero"]
    )
    assert [m["mutant"] for m in result["mutants"]] == ["price_allows_zero"]
    assert result["score"]["executed"] == 1
    assert not (tmp_path / "out" / "mutant-name_min_length_2.json").exists()


def test_mutation_output_is_byte_identical_across_runs(tmp_path):
    outputs = []
    for run in ("a", "b"):
        evaluation.evaluate_mutants(
            SPEC,
            tmp_path / run / "suite",
            tmp_path / run / "out",
            selected=["name_min_length_2"],
        )
        outputs.append((tmp_path / run / "out" / "mutation.json").read_bytes())
    assert outputs[0] == outputs[1]


def test_unknown_or_duplicate_mutants_are_rejected(tmp_path):
    with pytest.raises(evaluation.EvaluationError, match="unknown mutants"):
        evaluation.evaluate_mutants(
            SPEC, tmp_path / "s", tmp_path / "o", selected=["ghost"]
        )
    with pytest.raises(evaluation.EvaluationError, match="unique"):
        evaluation.evaluate_mutants(
            SPEC,
            tmp_path / "s",
            tmp_path / "o",
            selected=["price_allows_zero", "price_allows_zero"],
        )


def test_a_failing_control_run_invalidates_the_evaluation(tmp_path, monkeypatch):
    def broken(output_dir, entries, results_path, mutant=None):
        return {"summary": {"total": 43, "passed": 42, "failed": 1}, "results": []}

    monkeypatch.setattr(evaluation, "run_tests", broken)
    with pytest.raises(evaluation.EvaluationError, match="control run"):
        evaluation.evaluate_mutants(SPEC, tmp_path / "s", tmp_path / "o")


def test_mutation_markdown_lists_survivors(full_mutation_run):
    text = evaluation.mutation_markdown(full_mutation_run)
    assert "6 of 10" in text and "not exhaustive" in text
    assert text.count("| NO |") == len(MUTANTS) - len(_KILLED_BY_BASELINE)


# --- command line ---------------------------------------------------------------


def test_cli_sample_refuses_to_overwrite_labels(tmp_path, capsys):
    labels, meta = tmp_path / "l.csv", tmp_path / "m.json"
    base = ["evaluate", "sample", "--spec", str(SPEC), "--criteria", str(CRITERIA)]
    base += ["--labels", str(labels), "--meta", str(meta)]
    assert cli.main(base) == 0
    original = labels.read_text()
    assert cli.main(base) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert labels.read_text() == original
    assert cli.main([*base, "--force"]) == 0


def test_cli_mapping_rejects_blank_labels_and_accepts_filled(tmp_path, capsys):
    labels, meta = tmp_path / "l.csv", tmp_path / "m.json"
    common = ["--spec", str(SPEC), "--criteria", str(CRITERIA)]
    common += ["--labels", str(labels), "--meta", str(meta)]
    assert cli.main(["evaluate", "sample", *common]) == 0
    out = tmp_path / "reports"
    run = ["evaluate", "mapping", *common, "--output", str(out)]
    assert cli.main(run) == 2
    assert "label must be 1 or 0" in capsys.readouterr().err
    _rewrite(labels, lambda rows: [r.update(label="0") for r in rows])
    assert cli.main(run) == 0
    assert (out / "mapping.json").exists() and (out / "mapping.md").exists()


def test_cli_mapping_with_missing_labels_is_an_error(tmp_path, capsys):
    missing = tmp_path / "nope.csv"
    code = cli.main(
        ["evaluate", "mapping", "--spec", str(SPEC), "--criteria", str(CRITERIA)]
        + ["--labels", str(missing), "--output", str(tmp_path / "o")]
    )
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_mutants_end_to_end(tmp_path, capsys):
    code = cli.main(
        ["evaluate", "mutants", "--spec", str(SPEC)]
        + ["--work-dir", str(tmp_path / "suite"), "--output", str(tmp_path / "out")]
        + ["--mutant", "order_quantity_max_11"]
    )
    assert code == 0
    assert "Killed 1 of 1 mutants" in capsys.readouterr().out
    assert (tmp_path / "out" / "mutation.md").exists()
    code = cli.main(
        ["evaluate", "mutants", "--spec", str(SPEC), "--mutant", "ghost"]
        + ["--work-dir", str(tmp_path / "s2"), "--output", str(tmp_path / "o2")]
    )
    assert code == 2


def test_only_failed_tests_kill_a_mutant_not_setup_errors():
    rows = [
        {"case_id": "B", "outcome": "failed"},
        {"case_id": "A", "outcome": "failed"},
        {"case_id": "C", "outcome": "error"},
        {"case_id": "D", "outcome": "not_run"},
        {"case_id": "E", "outcome": "passed"},
    ]
    assert evaluation.detecting_cases(rows) == ["A", "B"]
    assert evaluation.detecting_cases([{"case_id": "C", "outcome": "error"}]) == []


# --- labelling workflow: encodings ---------------------------------------------


def test_a_sheet_saved_with_a_byte_order_mark_loads(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, rows = _filled_sheet(tmp_path, tests, criteria, lambda r: "1")
    sheet.write_bytes(b"\xef\xbb\xbf" + sheet.read_bytes())

    loaded = evaluation.load_labels(sheet, criteria, tests, meta)

    assert len(loaded) == len(rows)
    assert "pair_id" in loaded[0]  # not "\ufeffpair_id"


def test_the_committed_sheet_still_passes_integrity_with_a_byte_order_mark(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    copy = tmp_path / "with_bom.csv"
    copy.write_bytes(
        b"\xef\xbb\xbf" + (ROOT / "data" / "labelled_pairs.csv").read_bytes()
    )

    _assert_sheet_intact(_read_committed_sheet(copy), tests, criteria)


def test_a_sheet_saved_in_a_legacy_encoding_gives_a_clear_error(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "1")
    # Spreadsheet programs saving plain "CSV" on Windows write the dash as 0x97.
    sheet.write_bytes(sheet.read_text(encoding="utf-8").encode("cp1252", "replace"))
    assert b"\x97" in sheet.read_bytes()

    with pytest.raises(evaluation.EvaluationError, match="CSV UTF-8"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_the_cli_reports_a_legacy_encoding_as_an_error_not_a_traceback(
    tmp_path, tests_and_criteria, capsys
):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "1")
    sheet.write_bytes(sheet.read_text(encoding="utf-8").encode("cp1252", "replace"))

    code = cli.main(
        ["evaluate", "mapping", "--spec", str(SPEC), "--criteria", str(CRITERIA)]
        + ["--labels", str(sheet), "--meta", str(meta)]
        + ["--output", str(tmp_path / "out")]
    )

    assert code == 2
    assert "not valid UTF-8" in capsys.readouterr().err


# --- self-agreement: relabel sheet ---------------------------------------------


def _first_rows(tests, criteria, tmp_path, label_for=lambda r: "0"):
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, label_for)
    return evaluation.load_labels(sheet, criteria, tests, meta), sheet, meta


def test_relabel_sheet_is_a_blank_subset_of_the_first_sheet(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    first, _, _ = _first_rows(tests, criteria, tmp_path, lambda r: "1")

    rows = evaluation.make_relabel_rows(first, size=15)

    assert len(rows) == 15
    assert len({r["pair_id"] for r in rows}) == 15
    by_id = {r["pair_id"]: r for r in first}
    for row in rows:
        assert list(row) == evaluation.LABEL_COLUMNS
        assert row["label"] == "" and row["notes"] == ""
        original = by_id[row["pair_id"]]
        for column in evaluation.LABEL_COLUMNS:
            if column not in ("label", "notes"):
                assert row[column] == original[column]


def test_relabel_selection_cannot_depend_on_the_first_labels(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    all_no, _, _ = _first_rows(tests, criteria, tmp_path / "a", lambda r: "0")
    mixed, _, _ = _first_rows(
        tests, criteria, tmp_path / "b", lambda r: "1" if r["split"] == "dev" else "0"
    )

    assert evaluation.make_relabel_rows(
        all_no, size=12
    ) == evaluation.make_relabel_rows(mixed, size=12)


def test_relabel_is_deterministic_seed_dependent_and_reordered(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    first, _, _ = _first_rows(tests, criteria, tmp_path)

    again = evaluation.make_relabel_rows(first, size=20, seed=1)
    other = evaluation.make_relabel_rows(first, size=20, seed=2)

    assert again == evaluation.make_relabel_rows(first, size=20, seed=1)
    assert [r["pair_id"] for r in again] != [r["pair_id"] for r in other]
    first_order = [
        r["pair_id"] for r in first if r["pair_id"] in {x["pair_id"] for x in again}
    ]
    assert [r["pair_id"] for r in again] != first_order


@pytest.mark.parametrize("size", [0, -1, 41])
def test_relabel_rejects_an_impossible_size(tmp_path, tests_and_criteria, size):
    tests, criteria = tests_and_criteria
    first, _, _ = _first_rows(tests, criteria, tmp_path)

    with pytest.raises(evaluation.EvaluationError, match="size must be between"):
        evaluation.make_relabel_rows(first, size=size)


def test_relabel_rejects_a_first_sheet_with_duplicate_pairs(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    first, _, _ = _first_rows(tests, criteria, tmp_path)

    with pytest.raises(evaluation.EvaluationError, match="duplicate"):
        evaluation.make_relabel_rows([*first, first[0]], size=5)


def test_cli_relabel_writes_a_blank_subset_and_protects_existing_files(
    tmp_path, capsys
):
    committed = ROOT / "data" / "labelled_pairs.csv"
    before = committed.read_bytes()
    output = tmp_path / "relabel.csv"
    base = ["evaluate", "relabel", "--labels", str(committed)]

    assert cli.main([*base, "--output", str(output), "--size", "10"]) == 0
    assert "without looking at your first labels" in capsys.readouterr().out
    rows = _read_committed_sheet(output)
    assert len(rows) == 10 and all(r["label"] == "" for r in rows)

    assert cli.main([*base, "--output", str(output)]) == 2  # exists, no --force
    assert "refusing to overwrite" in capsys.readouterr().err
    assert cli.main([*base, "--output", str(committed)]) == 2  # never the first sheet
    assert "must not be the first sheet" in capsys.readouterr().err
    assert cli.main([*base, "--output", str(output), "--size", "5", "--force"]) == 0
    assert len(_read_committed_sheet(output)) == 5
    assert committed.read_bytes() == before  # the committed sheet was never touched


# --- self-agreement: report ----------------------------------------------------


def _labelled(rows, labels, notes=None):
    """Copies of rows with the given labels (and optional notes) filled in."""
    out = []
    for index, (row, label) in enumerate(zip(rows, labels, strict=True)):
        out.append({**row, "label": label, "notes": (notes or {}).get(index, "")})
    return out


def test_agreement_hand_worked_example(tmp_path, tests_and_criteria):
    # 10 pairs, five yes. The second pass flips one yes to no and one no to yes.
    # Observed agreement 8/10; each pass is 50% yes, so chance agreement is 0.5
    # and kappa = (0.8 - 0.5) / (1 - 0.5) = 0.6.
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:10]
    first = _labelled(rows, ["1"] * 5 + ["0"] * 5)
    second = _labelled(rows, ["0"] + ["1"] * 4 + ["0"] * 4 + ["1"])

    report = evaluation.evaluate_agreement(first, second)

    assert report["pairs_compared"] == 10
    assert report["agreements"] == 8
    assert report["observed_agreement"] == 0.8
    assert report["cohens_kappa"] == pytest.approx(0.6)
    assert [d["pair_id"] for d in report["disagreements"]] == sorted(
        [rows[0]["pair_id"], rows[9]["pair_id"]]
    )
    assert {
        d["pair_id"]: (d["first_label"], d["second_label"])
        for d in report["disagreements"]
    } == {
        rows[0]["pair_id"]: (1, 0),
        rows[9]["pair_id"]: (0, 1),
    }
    assert (report["positives_first"], report["positives_second"]) == (5, 5)


def test_only_the_relabelled_subset_is_compared(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    first = _labelled(base, ["1" if i % 2 else "0" for i in range(len(base))])
    second = _labelled(base[:6], [first[i]["label"] for i in range(6)])

    report = evaluation.evaluate_agreement(first, second)

    assert report["pairs_compared"] == 6
    assert report["first_sheet_pairs"] == 40
    assert report["observed_agreement"] == 1.0
    assert report["cohens_kappa"] == 1.0
    assert report["disagreements"] == []


def test_kappa_is_undefined_when_every_label_is_the_same(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    first = _labelled(base[:5], ["0"] * 5)

    report = evaluation.evaluate_agreement(first, first)

    assert report["observed_agreement"] == 1.0
    assert report["cohens_kappa"] is None
    assert "undefined" in evaluation.agreement_markdown(report)


def test_agreement_counts_rows_marked_unsure(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:4]
    first = _labelled(
        rows, ["1", "0", "1", "0"], {0: "Unsure", 2: "unsure about field"}
    )
    second = _labelled(rows, ["1", "0", "0", "0"], {2: "UNSURE"})

    report = evaluation.evaluate_agreement(first, second)

    assert (report["unsure_first"], report["unsure_second"]) == (2, 1)


def test_agreement_rejects_pairs_that_are_not_in_the_first_sheet(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    first = _labelled(base[:5], ["0"] * 5)
    stranger = _labelled(base[5:6], ["0"])

    with pytest.raises(evaluation.EvaluationError, match="not in the first sheet"):
        evaluation.evaluate_agreement(first, stranger)
    with pytest.raises(evaluation.EvaluationError, match="no pairs to compare"):
        evaluation.evaluate_agreement(first, [])


def test_agreement_markdown_is_honest_about_what_it_measures(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:10]
    report = evaluation.evaluate_agreement(
        _labelled(rows, ["1"] * 5 + ["0"] * 5),
        _labelled(rows, ["0"] + ["1"] * 4 + ["0"] * 4 + ["1"]),
    )

    text = evaluation.agreement_markdown(report)

    assert "not agreement between people" in text
    assert "one changed label moves observed agreement by 10.0 points" in text
    assert "0.800" in text and "0.600" in text
    assert text.count("\n| REQ-") == 2  # the two disagreements, listed


def test_agreement_markdown_with_no_disagreements(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:4]
    labels = _labelled(rows, ["1", "0", "1", "0"])

    text = evaluation.agreement_markdown(evaluation.evaluate_agreement(labels, labels))

    assert "None." in text


def test_agreement_output_is_deterministic(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    first = _labelled(base[:10], ["1", "0"] * 5)
    second = _labelled(base[:10], ["1", "1", "1", "0"] * 2 + ["1", "0"])

    one = evaluation.evaluate_agreement(first, second)

    assert one == evaluation.evaluate_agreement(first, second)
    assert json.dumps(one, sort_keys=True) == json.dumps(
        evaluation.evaluate_agreement(first, second), sort_keys=True
    )


def test_cli_relabel_then_agreement_end_to_end(tmp_path, tests_and_criteria, capsys):
    tests, criteria = tests_and_criteria
    first_sheet, meta, _ = _filled_sheet(
        tmp_path, tests, criteria, lambda r: "1" if r["split"] == "dev" else "0"
    )
    second_sheet = tmp_path / "second.csv"
    common = ["--spec", str(SPEC), "--criteria", str(CRITERIA), "--meta", str(meta)]

    assert (
        cli.main(
            ["evaluate", "relabel", "--labels", str(first_sheet)]
            + ["--output", str(second_sheet), "--size", "10"]
        )
        == 0
    )
    # A blank second sheet cannot be compared yet.
    run = ["evaluate", "agreement", *common, "--labels", str(first_sheet)]
    run += ["--relabels", str(second_sheet), "--output", str(tmp_path / "out")]
    assert cli.main(run) == 2
    assert "label must be 1 or 0" in capsys.readouterr().err

    # Label the same way as the first pass, except flip two.
    first_labels = {
        r["pair_id"]: r["label"] for r in _read_committed_sheet(first_sheet)
    }

    def fill(rows):
        for index, row in enumerate(rows):
            label = first_labels[row["pair_id"]]
            row["label"] = ("1" if label == "0" else "0") if index < 2 else label

    _rewrite(second_sheet, fill)
    assert cli.main(run) == 0

    report = json.loads((tmp_path / "out" / "agreement.json").read_text())
    assert report["pairs_compared"] == 10
    assert report["agreements"] == 8
    assert report["observed_agreement"] == 0.8
    assert len(report["disagreements"]) == 2
    assert (tmp_path / "out" / "agreement.md").exists()
    assert "Agreed on 8 of 10" in capsys.readouterr().out


# --- threshold tuning needs enough dev positives -------------------------------


def _report_with_dev_positives(tmp_path, tests_and_criteria, dev_positives):
    """Evaluate a sheet where exactly ``dev_positives`` dev rows are labelled 1."""
    tests, criteria = tests_and_criteria
    seen = {"dev": 0}

    def labeller(row):
        if row["split"] != "dev":
            return "0"
        seen["dev"] += 1
        return "1" if seen["dev"] <= dev_positives else "0"

    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, labeller)
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    return evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)


@pytest.mark.parametrize("dev_positives", [0, 1, evaluation.MIN_DEV_POSITIVES - 1])
def test_too_few_dev_positives_means_no_threshold_is_chosen(
    tmp_path, tests_and_criteria, dev_positives
):
    report = _report_with_dev_positives(tmp_path, tests_and_criteria, dev_positives)

    tuning = report["threshold_tuning"]
    assert tuning["tuned"] is False
    assert tuning["dev_positives"] == dev_positives
    assert tuning["kept_threshold"] == 0.4
    assert report["dev_chosen_threshold"] is None
    assert "dev_chosen_threshold_scores" not in report
    markdown = evaluation.mapping_markdown(report)
    assert "Threshold not tuned" in markdown
    assert "Dev-chosen threshold" not in markdown


@pytest.mark.parametrize("dev_positives", [evaluation.MIN_DEV_POSITIVES, 12])
def test_enough_dev_positives_lets_the_sweep_choose(
    tmp_path, tests_and_criteria, dev_positives
):
    report = _report_with_dev_positives(tmp_path, tests_and_criteria, dev_positives)

    assert report["threshold_tuning"]["tuned"] is True
    assert report["dev_chosen_threshold"] is not None
    assert "dev_chosen_threshold_scores" in report
    assert "Dev-chosen threshold:" in evaluation.mapping_markdown(report)


def test_enough_positives_but_no_defined_f1_is_not_reported_as_tuned(
    tmp_path, tests_and_criteria
):
    """Attempting the sweep is not the same as selecting a threshold."""
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    ever_predicted = evaluation._predicted_pairs(
        map_requirements(criteria, tests, threshold=0.0, top_k=1000)
    )
    unpredictable_dev = [
        {**row, "label": "1"}
        for row in rows
        if row["split"] == "dev"
        and (row["requirement_id"], row["case_id"]) not in ever_predicted
    ]
    assert len(unpredictable_dev) >= evaluation.MIN_DEV_POSITIVES  # precondition
    test_rows = [row for row in rows if row["split"] == "test"]

    report = evaluation.evaluate_mapping(
        unpredictable_dev + test_rows, criteria, tests, threshold=0.4, top_k=3
    )

    assert all(entry["f1"] is None for entry in report["dev_sweep"])
    tuning = report["threshold_tuning"]
    assert tuning["attempted"] is True
    assert tuning["tuned"] is False
    assert tuning["reason"] == "no_defined_f1"
    assert tuning["kept_threshold"] == 0.4
    assert report["dev_chosen_threshold"] is None
    markdown = evaluation.mapping_markdown(report)
    assert "F1 is undefined everywhere" in markdown
    assert "Dev-chosen threshold:" not in markdown


# --- the loader enforces the frozen sheet at runtime -------------------------------


def _frozen_sheet(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    return tests, criteria, sheet, meta


def test_moving_a_pair_between_halves_is_rejected_by_the_loader(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)

    def move_one(rows):
        rows[0]["split"] = "test" if rows[0]["split"] == "dev" else "dev"

    _rewrite(sheet, move_one)
    with pytest.raises(evaluation.EvaluationError, match="dev/test split"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_swapping_two_pairs_between_halves_keeps_counts_but_is_still_rejected(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)

    def swap(rows):
        dev = next(r for r in rows if r["split"] == "dev")
        test = next(r for r in rows if r["split"] == "test")
        dev["split"], test["split"] = "test", "dev"

    _rewrite(sheet, swap)
    with pytest.raises(evaluation.EvaluationError, match="dev/test split"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_editing_a_descriptive_column_is_rejected_by_the_loader(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    _rewrite(sheet, lambda rows: rows[3].update(test_name=rows[3]["test_name"] + "!"))
    with pytest.raises(evaluation.EvaluationError, match="frozen column"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_dropping_a_pair_is_rejected_unless_the_sheet_is_a_declared_subset(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    _rewrite(sheet, lambda rows: rows.pop())
    with pytest.raises(evaluation.EvaluationError, match="differ from the frozen set"):
        evaluation.load_labels(sheet, criteria, tests, meta)
    kept = evaluation.load_labels(sheet, criteria, tests, meta, subset=True)
    assert len(kept) == 39


def test_a_subset_may_not_smuggle_in_a_changed_split(tmp_path, tests_and_criteria):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)

    def flip(rows):
        rows[0]["split"] = "test" if rows[0]["split"] == "dev" else "dev"

    _rewrite(sheet, flip)
    with pytest.raises(evaluation.EvaluationError, match="dev/test split"):
        evaluation.load_labels(sheet, criteria, tests, meta, subset=True)


def test_missing_metadata_is_an_error_when_a_path_is_given(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    meta.unlink()
    with pytest.raises(evaluation.EvaluationError, match="is missing"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_metadata_without_pair_fingerprints_is_rejected(tmp_path, tests_and_criteria):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    data = json.loads(meta.read_text())
    del data["pair_fingerprints"]
    meta.write_text(json.dumps(data))
    with pytest.raises(evaluation.EvaluationError, match="pair_fingerprints"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_metadata_counts_must_match_the_sheet(tmp_path, tests_and_criteria):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    data = json.loads(meta.read_text())
    data["splits"] = {"dev": 19, "test": 21}
    meta.write_text(json.dumps(data))
    with pytest.raises(evaluation.EvaluationError, match="split counts"):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_labels_and_notes_may_change_without_breaking_the_freeze(
    tmp_path, tests_and_criteria
):
    tests, criteria, sheet, meta = _frozen_sheet(tmp_path, tests_and_criteria)
    _rewrite(sheet, lambda rows: rows[0].update(label="1", notes="unsure"))
    loaded = evaluation.load_labels(sheet, criteria, tests, meta)
    assert loaded[0]["label"] == "1"


def test_the_committed_sheet_loads_against_the_committed_metadata(tests_and_criteria):
    tests, criteria = tests_and_criteria
    root = Path(__file__).resolve().parent.parent
    rows = evaluation.load_labels(
        root / "data" / "labelled_pairs.csv",
        criteria,
        tests,
        root / "data" / "labelled_pairs.meta.json",
    )
    assert len(rows) == 40


def test_a_report_without_a_named_rater_is_a_self_agreement_report(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:4]
    report = evaluation.evaluate_agreement(
        _labelled(rows, ["1", "0", "0", "0"]), _labelled(rows, ["1", "0", "0", "0"])
    )

    text = evaluation.agreement_markdown(report)

    assert report["second_rater"] is None
    assert text.startswith("# Self-agreement")
    assert "one person's consistency with themselves" in text


def test_a_named_second_rater_is_not_presented_as_self_agreement(
    tmp_path, tests_and_criteria
):
    tests, criteria = tests_and_criteria
    base, _, _ = _first_rows(tests, criteria, tmp_path)
    rows = base[:4]

    report = evaluation.evaluate_agreement(
        _labelled(rows, ["1", "0", "0", "0"]),
        _labelled(rows, ["1", "1", "0", "0"]),
        second_rater="an AI model",
        note="not blind to the mapper",
    )
    text = evaluation.agreement_markdown(report)

    assert report["second_rater"] == "an AI model"
    assert text.startswith("# Agreement with a second rater")
    assert "The second rater is an AI model" in text
    assert "not self-consistency" in text
    assert "consistency with themselves" not in text
    assert "Note: not blind to the mapper" in text
    assert report["agreements"] == 3
